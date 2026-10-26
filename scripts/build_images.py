"""构建（并可推送）Runtime 的镜像（docs/plan/cloud-deploy.md D-01）。只用标准库。

    python scripts/build_images.py                      # 构建 deploy/images/images.json 里的全部镜像
    python scripts/build_images.py --only worker,memory # 只构建这几个
    python scripts/build_images.py --tag 0.1.0 --push   # 打标签并推送（CI 在打标签时这样跑，见 .github/workflows/images.yml）
    python scripts/build_images.py --list [--json]      # 只列出要构建的镜像与命令

镜像名是 <registry>/<名字>:<tag>，与 Chart 的 ar.image 一致：registry 默认取 Chart values 的 image.registry，
tag 默认取 Chart 的 appVersion。构建上下文是仓库根目录（.dockerignore 在那里）；只做 linux/amd64。
docker 从 PATH 找，或者 AR_DOCKER_BIN 指定的位置（与 scripts/check.py 相同）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "images" / "images.json"
CHART = ROOT / "deploy" / "helm" / "agent-runtime"
PLATFORM = "linux/amd64"


def manifest() -> dict[str, dict]:
    return json.loads(MANIFEST.read_text("utf-8"))["images"]


def chart_defaults() -> tuple[str, str]:
    """Chart values 的 image.registry 与 Chart 的 appVersion。"""
    values = (CHART / "values.yaml").read_text("utf-8")
    registry = re.search(r"(?m)^image:\s*\n(?:\s+.*\n)*?\s+registry:\s*(\S+)", values)
    version = re.search(r'(?m)^appVersion:\s*"?([^"\s]+)"?', (CHART / "Chart.yaml").read_text("utf-8"))
    if not registry or not version:
        raise SystemExit("读不出 Chart 的 image.registry 或 appVersion")
    return registry.group(1), version.group(1)


def docker() -> str:
    configured = os.environ.get("AR_DOCKER_BIN", "")
    if configured:
        path = Path(configured)
        found = shutil.which("docker", path=str(path if path.is_dir() else path.parent))
        if found:
            return found
    found = shutil.which("docker")
    if not found:
        raise SystemExit("找不到 docker（PATH 上没有，也没有 AR_DOCKER_BIN）")
    return found


def revision() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def build_command(name: str, spec: dict, *, ref: str, rev: str, exe: str = "docker") -> list[str]:
    cmd = [exe, "build", "--platform", PLATFORM, "-f", spec["dockerfile"], "-t", ref]
    for key, value in sorted(spec.get("args", {}).items()):
        cmd += ["--build-arg", f"{key}={value}"]
    cmd += [
        "--label", f"org.opencontainers.image.revision={rev}",
        "--label", f"org.opencontainers.image.title=agentruntime-{name}",
        ".",
    ]
    return cmd


def main(argv: list[str] | None = None) -> int:
    default_registry, default_tag = chart_defaults()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--registry", default=default_registry)
    parser.add_argument("--tag", default=default_tag)
    parser.add_argument("--only", default="", help="逗号分隔的镜像名")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.tag == "latest":
        parser.error("不要用 latest 标签")
    images = manifest()
    names = [n.strip() for n in args.only.split(",") if n.strip()] or sorted(images)
    unknown = [n for n in names if n not in images]
    if unknown:
        parser.error(f"没有这些镜像：{', '.join(unknown)}（有 {', '.join(sorted(images))}）")
    rev = revision()
    plan = [{"name": n, "ref": f"{args.registry}/{n}:{args.tag}", "command": build_command(n, images[n], ref=f"{args.registry}/{n}:{args.tag}", rev=rev)} for n in names]
    if args.list:
        if args.json:
            print(json.dumps({"images": plan}, ensure_ascii=False, indent=2))
        else:
            for item in plan:
                print(f"{item['ref']}\n  {' '.join(item['command'])}")
        return 0
    exe = docker()
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    for item in plan:
        print(f"==> {item['ref']}", flush=True)
        if subprocess.run([exe, *item["command"][1:]], cwd=ROOT, env=env).returncode != 0:
            print(f"构建失败：{item['name']}", file=sys.stderr)
            return 1
        if args.push and subprocess.run([exe, "push", item["ref"]], cwd=ROOT, env=env).returncode != 0:
            print(f"推送失败：{item['ref']}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
