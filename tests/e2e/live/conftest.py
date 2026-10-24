"""T-13 真模型端到端（docs/plan/live-e2e.md）：只在 AR_LIVE_MODEL_PROVIDER=1 时运行，不进 CI 与夜间任务（第 3 节第 1 条）。

整套由 scripts/dev_stack.py 拉起（与人手动演示同一个脚本）：真 Planner、真 Reviewer、经网关的 tool_loop，Registry 打开，
平台内工具的工作区在本次运行的目录里。供应商的地址与密钥只在本地密钥文件里（默认 ~/.agentruntime/model-provider.env，
AR_MODEL_PROVIDER_ENV_FILE 可以换）；脚本经 dev-secret 的标准输入写进开发 SecretStore。这里读密钥只为检查它没有出现在任何输出里。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from e2e_support.devstack import LIVE_TENANT, DevStack, run_script

ENV_LIVE = "AR_LIVE_MODEL_PROVIDER"
ENV_FILE = "AR_MODEL_PROVIDER_ENV_FILE"
DEFAULT_ENV_FILE = Path.home() / ".agentruntime" / "model-provider.env"
# 样本仓库不是测试：不收集。
collect_ignore_glob = ["workspaces/*"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(ENV_LIVE) == "1":
        return
    skip = pytest.mark.skip(reason=f"真模型端到端只在 {ENV_LIVE}=1 时运行（花钱、有随机性；docs/plan/live-e2e.md 第 3 节第 1 条）")
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(skip)


def provider_env_file() -> Path:
    return Path(os.environ.get(ENV_FILE) or DEFAULT_ENV_FILE)


def provider_key(path: Path) -> str:
    for line in path.read_text("utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "AR_MODEL_PROVIDER_API_KEY":
            return value.strip()
    return ""


@pytest.fixture(scope="session")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DevStack]:
    env_file = provider_env_file()
    if not env_file.exists():
        pytest.fail(f"找不到供应商的密钥文件 {env_file}（{ENV_FILE} 可以指定别处）", pytrace=False)
    key = provider_key(env_file)
    if not key:
        pytest.fail(f"{env_file} 里没有 AR_MODEL_PROVIDER_API_KEY", pytrace=False)
    root = tmp_path_factory.mktemp("live")
    try:
        stack = DevStack.up(
            root,
            "--provider", "openai_compat",
            "--provider-env", str(env_file),
            "--decisions", "model",
            "--tenant-effect", f"{LIVE_TENANT}=write_reversible",
            secrets=[key],
        )
    except RuntimeError as err:
        run_script("down", "--root", str(root))
        pytest.fail(str(err), pytrace=False)
    try:
        yield stack
    finally:
        stack.down()
