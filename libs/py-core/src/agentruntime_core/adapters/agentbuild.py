"""测试里编译 sandbox-agent（D-28）：默认是 Rust 版（services/rs-sandbox-agent，仓库根的 Cargo workspace）。py-worker 的测试
（本机的二进制：真的 Sandbox Service 的本地后端每个环境起一个进程，SBX-01）与 scripts/kind_cluster.py（Linux 静态二进制，放进
FROM scratch 的镜像）用。

- 环境变量 AR_SANDBOX_AGENT_BIN 给现成的二进制时直接用（不编译）。
- 两版并存的一个发布周期内，AR_SANDBOX_AGENT_IMPL=go 改用 Go 版（services/go-sandbox-agent）对照；之后 Go 版删掉，这个开关一起删。
- 缺 cargo（或 go）时报 MissingTool，调用方按各自的规矩处理（失败，或 AR_ALLOW_MISSING_TOOLS=1 时跳过）。
只用标准库。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
LINUX_TARGET = "x86_64-unknown-linux-musl"


class MissingTool(RuntimeError):
    pass


def implementation() -> str:
    impl = os.environ.get("AR_SANDBOX_AGENT_IMPL", "rust").strip() or "rust"
    if impl not in ("rust", "go"):
        raise ValueError(f"AR_SANDBOX_AGENT_IMPL 只能是 rust 或 go：{impl!r}")
    return impl


def build(out_dir: Path, *, linux_static: bool = False, env: dict[str, str] | None = None) -> Path:
    """编译 agent，放到 out_dir/sandbox-agent（Windows 上本机的是 .exe）。linux_static 为真时编译 Linux 的静态二进制（kind 用）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = "" if linux_static or os.name != "nt" else ".exe"
    dest = out_dir / f"sandbox-agent{exe}"
    given = os.environ.get("AR_SANDBOX_AGENT_BIN")
    if given:
        shutil.copy2(given, dest)
        return dest
    base_env = {**os.environ, **(env or {})}
    if implementation() == "go":
        go = shutil.which("go")
        if go is None:
            raise MissingTool("找不到 go（AR_SANDBOX_AGENT_IMPL=go 时要编译 Go 版 sandbox-agent）")
        go_env = {**base_env, **({"GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0"} if linux_static else {})}
        _run([go, "build", "-trimpath", "-o", str(dest), "./cmd/sandbox-agent"], REPO_ROOT / "services" / "go-sandbox-agent", go_env)
        return dest
    cargo = shutil.which("cargo")
    if cargo is None:
        raise MissingTool("找不到 cargo（要编译 sandbox-agent，Rust 工具链见 rust-toolchain.toml）")
    args = [cargo, "build", "--locked", "--release", "-p", "rs-sandbox-agent"]
    built = REPO_ROOT / "target" / "release" / f"sandbox-agent{exe}"
    if linux_static:
        args += ["--target", LINUX_TARGET]
        built = REPO_ROOT / "target" / LINUX_TARGET / "release" / "sandbox-agent"
        if os.name == "nt":
            # Windows 上没有 musl 的 C 工具链：用 Rust 自带的 lld 链接（与 fc-manager 的做法一样）。
            base_env.setdefault("CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER", "rust-lld")
    _run(args, REPO_ROOT, base_env)
    shutil.copy2(built, dest)
    dest.chmod(0o755)
    return dest


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    if result.returncode != 0:
        raise RuntimeError(f"编译 sandbox-agent 失败：{result.stderr[-4000:]}")
