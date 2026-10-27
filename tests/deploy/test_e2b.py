"""e2b 分支 E-03：自托管 E2B 的部署脚手架（deploy/e2b、scripts/e2b_template.py；docs/plan/e2b-backend.md）。

- Embed 的 overlay 按提交固定上游（不是 main），四个补丁都在：专用节点的容忍、team key 取自 Secret、没有网页控制台、
  ready 容器不打印 key。补丁的字段与固定版本的上游对得上（容器名）。
- 防火墙脚本：失败即停；3000、3002 只给 ALLOW_CIDRS；没有认证的 5008 等端口不从外面进来；参数做了校验。
- Secret 样例只有占位符；与 Runtime 的 SecretStore 用同一个名字（sandbox/e2b-api-key）。
- values-e2b.yaml 叠在 values-cloud.yaml 后渲染出的后端登记能被 Worker 读进来，e2b 后端是 microvm。
- 模板脚本对着假 E2B：按 digest 的镜像才构建；key 与仓库密码不出现在输出里；构建失败退出码非 0。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentruntime_core.adapters import e2btest
from deploy_tests import CLOUD_VALUES, REPO_ROOT, by_kind, render, run

E2B = REPO_ROOT / "deploy" / "e2b"
PINNED = "67b318240f4b6acc497411aa968b817b338f62e6"
SCRIPT = REPO_ROOT / "scripts" / "e2b_template.py"
DIGEST = "ghcr.io/agentruntime/sandbox-python@sha256:" + "a" * 64


def load(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text("utf-8")) if d]


def test_overlay_pins_the_upstream_commit_and_applies_every_patch() -> None:
    k = yaml.safe_load((E2B / "kustomization.yaml").read_text("utf-8"))
    [resource] = k["resources"]
    assert resource.endswith(f"?ref={PINNED}") and "github.com/e2b-dev/runtime//embed/kubernetes" in resource
    assert k["namespace"] == "e2b"
    paths = {p["path"] for p in k["patches"]}
    assert paths == {"patches/dedicated-node.yaml", "patches/team-key.yaml", "patches/no-dashboard.yaml", "patches/quiet-ready.yaml"}
    for p in paths:
        assert (E2B / p).is_file()
    assert PINNED in (REPO_ROOT / "docs" / "plan" / "e2b-backend.md").read_text("utf-8"), "设计稿里写着同一个固定提交"


def test_patches_target_the_upstream_containers() -> None:
    [dedicated] = load(E2B / "patches" / "dedicated-node.yaml")
    assert dedicated["spec"]["template"]["spec"]["tolerations"] == [
        {"key": "agentruntime.io/dedicated", "operator": "Equal", "value": "e2b", "effect": "NoSchedule"}
    ]
    [team] = load(E2B / "patches" / "team-key.yaml")
    [seed] = team["spec"]["template"]["spec"]["initContainers"]
    [env] = seed["env"]
    assert seed["name"] == "seed" and env["name"] == "SEED_TEAM_API_KEY"
    assert "value" in env and env["value"] is None, "value: null 删掉上游的 random"
    assert env["valueFrom"] == {"secretKeyRef": {"name": "e2b-api", "key": "TEAM_API_KEY"}}
    [nodash] = load(E2B / "patches" / "no-dashboard.yaml")
    assert {c["name"] for c in nodash["spec"]["template"]["spec"]["initContainers"]} == {"dashboard", "dashboard-api"}
    assert all(c["$patch"] == "delete" for c in nodash["spec"]["template"]["spec"]["initContainers"])
    [quiet] = load(E2B / "patches" / "quiet-ready.yaml")
    [ready] = quiet["spec"]["template"]["spec"]["containers"]
    script = ready["command"][-1]
    assert ready["name"] == "ready" and "team-api-key" in script and "sdk.env" in script
    assert "cat /run/e2b/team-api-key" not in script and "E2B_API_KEY" not in script, "不把 key 写进日志或 sdk.env"


def test_firewall_keeps_the_control_ports_local() -> None:
    text = (E2B / "firewall.sh").read_text("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text and "\r\n" not in text
    local = re.search(r'LOCAL_ONLY_PORTS="([^"]+)"', text).group(1)
    assert {p.strip() for p in local.split(",")} == {"3001", "3003", "3010", "5007", "5008", "5009", "5010", "5016", "5017", "5018", "5109"}
    assert re.search(r'PUBLIC_PORTS="3000, 3002"', text)
    assert "ip saddr @clients accept" in text and "nft -c -f" in text, "先检查规则再生效"
    assert 'ALLOW_CIDRS="${ALLOW_CIDRS:?' in text and 'EXTERNAL_IFACES="${EXTERNAL_IFACES:?' in text, "两个参数都必填"
    assert "^[0-9./,]+$" in text, "ALLOW_CIDRS 做了格式校验，不会把任意文字拼进 nft 规则"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上的 bash 是 WSL 的，读不到网络盘；CI（Linux）上检查语法")
def test_firewall_script_parses() -> None:
    bash = shutil.which("bash")
    assert bash, "要有 bash"
    run([bash, "-n", E2B / "firewall.sh"])


def test_secret_example_has_placeholders_only() -> None:
    [secret] = load(E2B / "secrets.example.yaml")
    assert secret["metadata"] == {"name": "e2b-api", "namespace": "e2b"}
    assert set(secret["stringData"]) == {"ADMIN_TOKEN", "SANDBOX_ACCESS_TOKEN_HASH_SEED", "TEAM_API_KEY"}
    assert all(v == "REPLACE_ME" for v in secret["stringData"].values())
    assert "sandbox/e2b-api-key" in (E2B / "secrets.example.yaml").read_text("utf-8")
    from agentruntime_worker.tools.sandbox_e2b import API_KEY_SECRET

    assert API_KEY_SECRET == "sandbox/e2b-api-key"


def test_values_overlay_adds_a_microvm_backend_the_worker_accepts(tmp_path: Path) -> None:
    manifests = render(CLOUD_VALUES, E2B / "values-e2b.yaml")
    [cm] = [m for m in by_kind(manifests, "ConfigMap") if m["metadata"]["name"].endswith("-sandbox-backends")]
    registry = json.loads(cm["data"]["backends.json"])
    assert {"gvisor", "kata", "e2b"} <= set(registry["backends"]), "叠加，不替换 values-cloud 里的后端"
    e2b = registry["backends"]["e2b"]
    assert e2b["kind"] == "e2b" and e2b["isolation"] == "microvm" and e2b["pause_on_idle"] is True
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(registry), "utf-8")
    from agentruntime_worker.tools.sandboxes import Sandboxes

    assert "e2b" in Sandboxes.from_file(path).names


@pytest.fixture(scope="module")
def fake():
    f = e2btest.FakeE2B()
    yield f
    f.stop()


def template(fake: e2btest.FakeE2B, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """只带 PATH 等必需的变量跑脚本，key 只经环境变量给。"""
    full = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    full.update(env)
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--api-url", fake.api_url, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=full, timeout=120,
    )


def test_template_script_builds_from_a_pinned_image(fake: e2btest.FakeE2B) -> None:
    proc = template(fake, "--name", "agentruntime-python", "--image", DIGEST, AR_E2B_API_KEY=fake.api_key,
                    AR_E2B_REGISTRY_USERNAME="ci-bot", AR_E2B_REGISTRY_PASSWORD="registry-pass-4e1")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["template"] == "agentruntime-python" and out["image"] == DIGEST
    start = [b for b in fake.builds if b.get("build_id") == out["build_id"] and "fromImage" in b]
    assert start and start[-1]["fromImage"] == DIGEST
    assert start[-1]["fromImageRegistry"] == {"type": "registry", "username": "ci-bot", "password": "registry-pass-4e1"}
    for leak in (fake.api_key, "registry-pass-4e1"):
        assert leak not in proc.stdout and leak not in proc.stderr


@pytest.mark.parametrize("image", ["ghcr.io/agentruntime/sandbox-python:0.1.0", "ghcr.io/x@sha256:abc"])
def test_template_script_refuses_unpinned_images(fake: e2btest.FakeE2B, image: str) -> None:
    proc = template(fake, "--name", "agentruntime-python", "--image", image, AR_E2B_API_KEY=fake.api_key)
    assert proc.returncode == 1 and "digest" in proc.stderr


def test_template_script_needs_the_key_from_the_environment(fake: e2btest.FakeE2B) -> None:
    proc = template(fake, "--name", "agentruntime-python", "--image", DIGEST)
    assert proc.returncode == 1 and "AR_E2B_API_KEY" in proc.stderr
    wrong = template(fake, "--name", "agentruntime-python", "--image", DIGEST, AR_E2B_API_KEY="e2b_" + "0" * 40)
    assert wrong.returncode == 1 and "401" in wrong.stderr and "0" * 40 not in wrong.stderr
