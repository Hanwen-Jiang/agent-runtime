"""T-13 交付 1、2（docs/plan/live-e2e.md 第 2 节）：scripts/dev_stack.py 在本机拉起整套，开发种子能载入 Registry。

离线：Model Gateway 用 mock 供应商、Planner 与 Reviewer 用桩（`--decisions stub`），不调真模型、不要密钥。
整套与 tests/e2e 其余用例的 Stack 互不相干（各自的端口与目录）。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.devstack import COMPONENTS, DevStack, last_json, run_script
from e2e_support.stack import REPO_ROOT, wait_until

pytestmark = pytest.mark.e2e

SEED_FILE = REPO_ROOT / "deploy" / "dev" / "registry-seed.json"
SCHEMAS_DIR = REPO_ROOT / "schemas"
# registry-seed 的条目种类 → 校验它的 schema。
SEED_SCHEMAS = {
    "node_template": "node_template.schema.json",
    "recipe": "recipe.schema.json",
    "model_profile": "model_profile.schema.json",
    "tenant_policy": "tenant_policy.schema.json",
}


@pytest.fixture(scope="module")
def dev_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DevStack]:
    root = tmp_path_factory.mktemp("dev-stack")
    try:
        stack = DevStack.up(root, "--provider", "mock", "--decisions", "stub", "--tenant-effect", "t1=read")
    except RuntimeError as err:
        run_script("down", "--root", str(root))
        pytest.fail(str(err), pytrace=False)
    try:
        yield stack
    finally:
        stack.down()


def test_up_reports_every_component(dev_stack: DevStack) -> None:
    info = dev_stack.info
    assert set(info["components"]) == set(COMPONENTS)
    for name, entry in info["components"].items():
        assert entry["alive"], f"{name} 没在运行"
        assert Path(entry["log"]).exists(), f"{name} 的日志 {entry['log']} 不存在"
    with urllib.request.urlopen(info["task_api"] + "/healthz", timeout=5) as resp:
        assert resp.status == 200
    with urllib.request.urlopen(f"http://{info['model_gateway']}/healthz", timeout=5) as resp:
        assert resp.status == 200
    assert info["registry"] != info["task_api"].removeprefix("http://"), "Registry 在单独的内部端口上（T-08）"


def test_status_sees_every_component_running(dev_stack: DevStack) -> None:
    status = dev_stack.status()
    assert {name for name, entry in status["components"].items() if entry["alive"]} == set(COMPONENTS)


def test_a_task_is_delivered_through_the_whole_stack(dev_stack: DevStack) -> None:
    api = TaskApi(dev_stack.task_api, dev_stack.token("t1", "u1"))
    sub = api.submit("dev-stack 冒烟：经整套交付")
    result = finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=180))
    assert result["status"] == "TASK_STATUS_DONE", result

    records = [r for r in dev_stack.metering() if r.get("task_id") == sub["taskId"]]
    assert records, "model 节点应经 Model Gateway（mock 供应商）"
    assert {r["tenant_id"] for r in records} == {"t1"}, "计量记在 Task 的租户名下"

    pinned = api.query(sub["taskId"])["taskState"]["value"]["pinned"]
    assert {"stub_answer", "answer"} <= set(pinned.get("node_templates") or {}), "Task 固定的模板版本来自 Registry 的开发种子"
    assert pinned.get("model_profiles"), "Model Profile 的版本也固定"


def test_env_prints_every_component_without_secrets(tmp_path: Path) -> None:
    key = "sk-test-dev-stack-DO-NOT-PRINT-7f3a"
    env_file = tmp_path / "model-provider.env"
    env_file.write_text(f"AR_MODEL_PROVIDER_BASE_URL = https://example.invalid/v1\nAR_MODEL_PROVIDER_API_KEY = {key}\n", "utf-8")
    proc = run_script("env", "--root", str(tmp_path / "stack"), "--provider", "openai_compat", "--provider-env", str(env_file), "--json")
    assert proc.returncode == 0, proc.stderr.replace(key, "***")
    output = proc.stdout + proc.stderr
    if key in output:
        pytest.fail("dev_stack.py env 的输出里出现了密钥（已隐去）", pytrace=False)
    envs = last_json(proc)["components"]
    assert set(envs) == set(COMPONENTS) - {"registry"}, "Registry 在 task-service 进程里，没有自己的环境"
    assert "AR_MODEL_PROVIDER_API_KEY" not in envs["model-gateway"], "密钥只经 dev-secret 的标准输入进 SecretStore"
    assert envs["model-gateway"]["AR_MODEL_PROVIDER_BASE_URL"] == "https://example.invalid/v1"
    worker = envs["worker"]
    assert worker["AR_PLANNER"] == worker["AR_REVIEWER"] == worker["AR_TOOL_LOOP"] == "model", "默认用真决策"
    assert worker["AR_MODEL_GATEWAY_ADDRESS"] and worker["AR_REGISTRY_ADDRESS"] and worker["AR_TOOL_WORKSPACE_ROOT"]
    assert worker["AR_OBJECT_STORE_DIR"] == envs["task-service"]["AR_OBJECT_STORE_DIR"], "Claim-Check 两边要互相取回"
    assert worker["AR_SECRETS_FILE"] == envs["task-service"]["AR_SECRETS_FILE"]


def test_down_stops_everything_and_is_idempotent(tmp_path: Path) -> None:
    stack = DevStack.up(tmp_path, "--provider", "mock", "--decisions", "stub")
    pids = [entry["pid"] for entry in stack.info["components"].values()]
    assert stack.down().returncode == 0

    def all_gone() -> bool:
        return not any(entry["alive"] for entry in stack.status()["components"].values())

    wait_until(all_gone, "dev_stack.py down 之后所有组件都停了", timeout=30)
    assert stack.down().returncode == 0, "再 down 一次不报错"
    assert pids, "up 时记下了各组件的 pid"


def test_the_dev_seed_documents_match_their_schemas() -> None:
    seed = json.loads(SEED_FILE.read_text("utf-8"))
    entries = seed["entries"]
    kinds = {e["kind"] for e in entries}
    assert kinds == {"node_template", "model_profile"}, "租户策略由 dev_stack.py 按 --tenant-effect 另外种下"
    names = {e["name"] for e in entries if e["kind"] == "node_template"}
    assert {"stub_answer", "stub_confirm", "answer", "rewrite"} <= names, "桩 Planner 与真 Planner 用到的模板都要登记"
    profiles = json.loads((REPO_ROOT / "deploy" / "dev" / "model-profiles.json").read_text("utf-8"))["profiles"]
    assert {e["name"] for e in entries if e["kind"] == "model_profile"} == {p["id"] for p in profiles}, "与网关的开发 Profile 一致"

    resources = [(doc["$id"], Resource.from_contents(doc)) for doc in (json.loads(p.read_text("utf-8")) for p in SCHEMAS_DIR.glob("*.schema.json"))]
    registry: Registry[Any] = Registry().with_resources(resources)
    for entry in entries:
        schema = json.loads((SCHEMAS_DIR / SEED_SCHEMAS[entry["kind"]]).read_text("utf-8"))
        errors = list(Draft202012Validator(schema, registry=registry).iter_errors(entry["document"]))
        assert not errors, f"{entry['kind']} {entry['name']} 不合 schema：{errors[0].message}"


def test_the_dev_seed_loads_into_the_registry_idempotently(tmp_path: Path) -> None:
    from e2e_support.stack import Stack

    stack = Stack(root=tmp_path)
    stack._build_task_service()
    assert stack.task_service_bin is not None
    env = {**os.environ, "AR_DATA_DIR": str(tmp_path / "data"), "AR_SCHEMAS_DIR": str(SCHEMAS_DIR)}
    for _ in range(2):
        proc = subprocess.run(
            [str(stack.task_service_bin), "registry-seed", str(SEED_FILE)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert proc.returncode == 0, f"registry-seed 失败：\n{proc.stderr}"
