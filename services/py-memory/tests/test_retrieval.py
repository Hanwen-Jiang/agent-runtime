"""Memory Service 主体：检索流水线（设计书 3.4.1 第 4 条、附录 I.5、I.6）。

查询构造（按时机）→ 结构化预过滤（ACL 由身份拼接、kind、任务族、有效、未被取代、active）→ 出错时先按错误签名精确匹配
→ 稠密 + 稀疏混合召回 → 业务打分（相关 × 信任 × 新鲜度 × 作用域权重）→ MMR 去冗余 → token 预算。
"""

from __future__ import annotations

from datetime import timedelta

from agentruntime_memory.service import Feedback, SearchQuery

from .memory_support import ALICE, CAROL, DAVE, Backend, item, verified, write


async def ids(svc, identity, **query) -> list[str]:
    return [h.item["id"] for h in await svc.search(identity, SearchQuery(**{"trigger": "planning", **query}))]


async def test_prefilter_drops_expired_items_and_other_kinds(dev: Backend) -> None:
    svc = dev.service
    live = item(kind="failure", title="redis 连接被拒绝", guidance="先启动 redis 容器")
    expired = item(kind="failure", title="redis 连接被拒绝", guidance="旧版做法：重启宿主机上的服务进程", valid_to="2026-09-15")
    pref = item(kind="preference", title="redis 相关回复用中文", guidance="用中文回复")
    await write(svc, ALICE, live, expired, pref)
    found = await ids(svc, ALICE, query="redis 连接被拒绝", kinds=("failure",))
    assert found == [live["id"]]


async def test_task_family_filters_structurally(dev: Backend) -> None:
    svc = dev.service
    ci = item(title="redis 测试依赖", task_family=("engineering.ci_fix",))
    other = item(title="redis 测试依赖", guidance="报表任务里先预热 redis 缓存", task_family=("analytics.report",))
    await write(svc, ALICE, ci, other)
    assert await ids(svc, ALICE, query="redis 测试依赖", task_family="engineering.ci_fix") == [ci["id"]]


async def test_error_trigger_matches_the_signature_exactly_first(dev: Backend) -> None:
    """错误签名不嵌入，归一化（去掉路径、行号、数字）后精确匹配优先，向量兜底（附录 I.6）。
    memory_item.schema.json 没有错误签名字段：failure 条目的标题写错误签名。"""
    svc = dev.service
    exact = item(kind="failure", title="ImportError: cannot import name 'TaxTable' (line 12)", guidance="TaxTable 在依赖包里改名了，先固定依赖版本")
    similar = item(kind="failure", title="ImportError TaxTable 依赖安装不完整", guidance="重新安装 TaxTable 所在的依赖")
    await write(svc, ALICE, similar, exact)
    hits = await svc.search(ALICE, SearchQuery(trigger="error", error_signature="ImportError: cannot import name 'TaxTable' (line 40)", query="cannot import name TaxTable"))
    assert hits[0].item["id"] == exact["id"]


async def test_trust_freshness_and_scope_shape_the_ranking(dev: Backend) -> None:
    svc = dev.service
    low = item(title="redis 先启动", guidance="启动 redis 再测试", trust=0.3)
    high = item(title="redis 先启动 容器", guidance="启动 redis 容器再测试", trust=0.9)
    await write(svc, ALICE, low, high)
    assert (await ids(svc, ALICE, query="redis 先启动"))[0] == high["id"]
    dev.clock.advance(timedelta(days=400))
    fresh = item(title="Redis 启动顺序", guidance="测试前确认 redis 已启动并能连上", trust=0.6, valid_from="2027-11-01")
    await write(svc, ALICE, fresh)
    assert (await ids(svc, ALICE, query="redis 先启动"))[0] == fresh["id"], "旧条目按新鲜度衰减"


async def test_near_duplicates_are_deduplicated_by_mmr(dev: Backend) -> None:
    """a 与 b 内容几乎一样（kind 不同，写入时不合并）；MMR 让第二个位置给不同的内容。"""
    svc = dev.service
    a = item(kind="failure", title="redis 容器 启动", guidance="跑 make test 前启动 redis 容器")
    b = item(kind="success", title="redis 容器 启动", guidance="跑 make test 前启动 redis 容器")
    c = item(kind="failure", title="docker 磁盘满 make test", guidance="make test 失败时清理 docker 镜像")
    await write(svc, ALICE, a, b, c)
    found = await ids(svc, ALICE, query="redis 容器 make test", top_k=2)
    assert c["id"] in found and len({a["id"], b["id"]} & set(found)) == 1


async def test_the_token_budget_truncates_the_results(dev: Backend) -> None:
    svc = dev.service
    topics = ["port", "password", "persistence", "memory", "cluster"]
    items = [item(title=f"redis {t} 配置", guidance=f"redis {t} " + " ".join(f"{t}{j}" for j in range(40))) for t in topics]
    await write(svc, ALICE, *items)
    small = await svc.search(ALICE, SearchQuery(trigger="planning", query="redis 配置", token_budget=80, top_k=5))
    large = await svc.search(ALICE, SearchQuery(trigger="planning", query="redis 配置", token_budget=10_000, top_k=5))
    assert 1 <= len(small) < len(large) == 5


async def test_results_carry_their_provenance(dev: Backend) -> None:
    svc = dev.service
    await write(svc, ALICE, item(source="agent_reflection"))
    [hit] = await svc.search(ALICE, SearchQuery(trigger="planning", query="redis make test"))
    assert hit.item["provenance"]["source"] == "agent_reflection" and hit.score > 0


async def test_workspace_items_reach_members_but_not_other_projects(dev: Backend) -> None:
    svc = dev.service
    success = item(kind="success", title="先启动 Redis 再跑测试", evidence=verified(3))
    report = await write(svc, ALICE, success)
    assert report.promoted
    assert report.promoted[0] in await ids(svc, CAROL, query="Redis 测试")
    assert await ids(svc, DAVE, query="Redis 测试") == []


async def test_feedback_updates_stats_and_hurt_lowers_trust(dev: Backend) -> None:
    svc = dev.service
    entry = item(trust=0.8)
    await write(svc, ALICE, entry)
    await svc.feedback(ALICE, "tsk_01J" + "0" * 22 + "F", [Feedback(memory_id=entry["id"], adopted=True, task_succeeded=False)])
    stored = await svc.get(ALICE, entry["id"])
    assert stored is not None and stored["stats"]["hurt"] == 1 and stored["trust"] < 0.8
    await svc.feedback(ALICE, "tsk_01J" + "0" * 22 + "G", [Feedback(memory_id="mem_0" + "Z" * 25, adopted=True, task_succeeded=True)])
