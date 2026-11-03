"""CACHE-01 验收（docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：task-service 的 Cache 接 Valkey。

values 的 secrets.valkeyUrl 引用一个已有的 Secret（地址里有口令，values 里只写名字与键），渲染成 task-service 的
AR_VALKEY_URL；不配时不渲染（进程内的开发实现）。deploy/data 单独装一个 Runtime 用的 Valkey：不落盘、口令来自 Secret、
不出现在命令行里，镜像钉版本。
"""

from __future__ import annotations

from typing import Any

import yaml

from deploy_tests import REPO_ROOT, by_kind, env_of, pod_spec, render


def task_service(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    [ts] = [m for m in by_kind(manifests, "Deployment") if m["metadata"]["name"].endswith("-task-service")]
    return env_of(pod_spec(ts)["containers"][0])


def test_off_by_default() -> None:
    assert "AR_VALKEY_URL" not in task_service(render())


def test_valkey_url_comes_from_a_secret() -> None:
    env = task_service(render(set_values=["secrets.valkeyUrl.name=agent-runtime-valkey"]))
    assert env["AR_VALKEY_URL"] == {
        "name": "AR_VALKEY_URL",
        "valueFrom": {"secretKeyRef": {"name": "agent-runtime-valkey", "key": "url"}},
    }


def test_data_valkey_manifest() -> None:
    docs = [d for d in yaml.safe_load_all((REPO_ROOT / "deploy" / "data" / "valkey.yaml").read_text(encoding="utf-8")) if d]
    [deploy] = [d for d in docs if d["kind"] == "Deployment"]
    [svc] = [d for d in docs if d["kind"] == "Service"]
    assert deploy["metadata"]["namespace"] == svc["metadata"]["namespace"] == "data"
    [c] = deploy["spec"]["template"]["spec"]["containers"]
    image = c["image"]
    assert image.startswith("valkey/valkey:") and image.split(":")[1][0].isdigit(), "镜像要钉到具体版本"
    script = " ".join(c["args"])
    assert "--save ''" in script and "--appendonly no" in script, "Cache 只存可丢失的数据，不落盘"
    assert "--requirepass" not in script, "口令写进内存卷上的配置文件，不上命令行"
    [pw] = [e for e in c["env"] if e["name"] == "VALKEY_PASSWORD"]
    assert pw["valueFrom"]["secretKeyRef"]["name"] == "valkey-agentruntime"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    install = (REPO_ROOT / "deploy" / "data" / "install.sh").read_text(encoding="utf-8")
    assert "valkey.yaml" in install and "valkey-agentruntime" in install
    example = (REPO_ROOT / "deploy" / "cluster" / "secrets" / "secrets.example.yaml").read_text(encoding="utf-8")
    assert "name: valkey-agentruntime" in example and "name: agent-runtime-valkey" in example
