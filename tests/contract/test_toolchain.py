"""S-01 验收：buf lint 与 buf breaking 通过；Go 与 Python 生成代码能编译或导入；仓库骨架齐全。

另含两个“测试的测试”：故意删字段、改字段编号，buf breaking 必须报错，证明兼容性检查确实生效
（计划 S-01 难点“字段编号预留”）。
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import tomllib
from pathlib import Path

from contract_tests import protos
from contract_tests.paths import PROTO_DIR, REPO_ROOT
from contract_tests.tools import require_tool, run

# 设计书 4.15 的目录结构，加上 Buf、uv workspace、Go workspace 的入口文件。
REQUIRED_LAYOUT = (
    "buf.yaml",
    "buf.gen.yaml",
    "go.work",
    "pyproject.toml",
    "uv.lock",
    "proto",
    "schemas",
    "deploy",
    "services/go-task-service",
    "services/go-model-gateway",
    "services/py-worker",
    "services/py-memory",
    "services/py-learning",
)
GO_MODULES = (
    "libs/go-core",
    "proto/gen/go",
    "services/go-task-service",
    "services/go-model-gateway",
    "tests/contract/go",
)
UV_MEMBERS = (
    "libs/py-core",
    "proto/gen/python",
    "services/py-worker",
    "services/py-memory",
    "services/py-learning",
    "tests/contract",
    "tests/e2e",
)
GENERATED_DIRS = (PROTO_DIR / "gen" / "go", PROTO_DIR / "gen" / "python")
GENERATED_SUFFIXES = (".pb.go", ".connect.go", "_pb2.py", "_pb2.pyi", "_pb2_grpc.py")
# buf breaking 的基线：人审通过的契约镜像，每次契约变更经人审后重新生成（CLAUDE.md 常用命令）。
BREAKING_BASELINE = PROTO_DIR / "breaking-baseline.binpb"


# --- 仓库骨架 ---


def test_repository_layout():
    missing = [rel for rel in REQUIRED_LAYOUT if not (REPO_ROOT / rel).exists()]
    assert not missing, f"缺少：{missing}"


def test_uv_workspace_members():
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    members = set(config["tool"]["uv"]["workspace"]["members"])
    missing = [m for m in UV_MEMBERS if m not in members]
    assert not missing, f"uv workspace 缺少成员：{missing}"


def test_uv_lock_is_current():
    run([require_tool("uv"), "lock", "--check"])


# --- Buf ---


def test_buf_lint():
    run([require_tool("buf"), "lint"])


def test_buf_format_is_clean():
    run([require_tool("buf"), "format", "--diff", "--exit-code"])


def test_buf_breaking_against_baseline():
    """以人审通过的契约镜像为基线。不用 .git#branch=main：仓库在网络共享盘上时 buf 无法克隆它，
    而且 main 上也可能有尚未人审的契约改动。"""
    buf = require_tool("buf")
    assert BREAKING_BASELINE.is_file(), f"缺少兼容性基线 {BREAKING_BASELINE.name}"
    run([buf, "breaking", "--against", BREAKING_BASELINE])


def _copy_buf_module(dst: Path) -> Path:
    for name in ("buf.yaml", "buf.lock"):
        if (REPO_ROOT / name).exists():
            shutil.copy2(REPO_ROOT / name, dst / name)
    shutil.copytree(PROTO_DIR, dst / "proto", ignore=shutil.ignore_patterns("gen", "*.binpb"))
    return dst


def _file_defining(root: Path, message: str) -> Path:
    for path in sorted((root / "proto").rglob("*.proto")):
        if re.search(rf"^message\s+{message}\s*\{{", path.read_text("utf-8"), flags=re.M):
            return path
    raise AssertionError(f"找不到定义 {message} 的 .proto 文件")


def _mutate(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text("utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    assert count == 1, f"{path.name} 中没有匹配 {pattern!r}"
    path.write_text(new_text, "utf-8")


def _breaking_after(tmp_path: Path, pattern: str, replacement: str):
    buf = require_tool("buf")
    (tmp_path / "module").mkdir()
    work = _copy_buf_module(tmp_path / "module")
    baseline = tmp_path / "baseline.binpb"
    run([buf, "build", "-o", baseline], cwd=work)
    _mutate(_file_defining(work, "TaskWorkflowInput"), pattern, replacement)
    return run([buf, "breaking", "--against", baseline], cwd=work, check=False)


def test_buf_breaking_detects_deleted_field(tmp_path):
    proc = _breaking_after(tmp_path, r"^\s*PinnedVersions\s+pinned_versions\s*=\s*\d+\s*;.*\n", "")
    assert proc.returncode != 0, "删除字段后 buf breaking 应当失败"
    assert "pinned_versions" in proc.stdout + proc.stderr


def test_buf_breaking_detects_renumbered_field(tmp_path):
    proc = _breaking_after(tmp_path, r"(\bstring\s+task_id\s*=\s*)\d+", r"\g<1>9999")
    assert proc.returncode != 0, "修改字段编号后 buf breaking 应当失败"


def _generated_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith(GENERATED_SUFFIXES)
    }


def test_generated_code_is_up_to_date(tmp_path):
    """提交的生成代码与 buf generate 的结果一致，契约改了必须重新生成。"""
    run([require_tool("buf"), "generate", "--output", tmp_path])
    for gen_dir in GENERATED_DIRS:
        relative = gen_dir.relative_to(REPO_ROOT)
        fresh = _generated_files(tmp_path / relative)
        committed = _generated_files(gen_dir)
        assert fresh, f"buf generate 没有产出 {relative}"
        assert sorted(fresh) == sorted(committed), f"{relative} 的文件清单与生成结果不一致"
        stale = sorted(path for path in fresh if fresh[path] != committed[path])
        assert not stale, f"{relative} 中这些文件过期：{stale}"


# --- Go ---


def _go_workspace_modules() -> list[Path]:
    proc = run([require_tool("go"), "work", "edit", "-json"])
    work = json.loads(proc.stdout)
    return [(REPO_ROOT / use["DiskPath"]).resolve() for use in work.get("Use") or []]


def test_go_workspace_lists_all_modules():
    modules = set(_go_workspace_modules())
    missing = [rel for rel in GO_MODULES if (REPO_ROOT / rel).resolve() not in modules]
    assert not missing, f"go.work 缺少模块：{missing}"


def test_go_modules_build_vet_and_test(tmp_path):
    go = require_tool("go")
    for module in _go_workspace_modules():
        # -o 指向临时目录：模块里只有一个 main 包时，go build 会把可执行文件写进模块目录。
        # 没有 main 包的模块不接受 -o 目录，此时普通的 go build 也不会写出文件。
        proc = run([go, "build", "-o", tmp_path, "./..."], cwd=module, check=False)
        if proc.returncode != 0:
            assert "no main packages" in proc.stderr + proc.stdout, proc.stderr
            run([go, "build", "./..."], cwd=module)
        run([go, "vet", "./..."], cwd=module)
        run([go, "test", "./..."], cwd=module)


# --- Python ---


def test_python_generated_code_imports():
    assert protos.files(), "没有找到生成的 *_pb2 模块"
    grpc_modules = protos.generated_modules("_pb2_grpc")
    assert grpc_modules, "没有生成 gRPC 存根（*_pb2_grpc）"
    for name in grpc_modules:
        importlib.import_module(name)
