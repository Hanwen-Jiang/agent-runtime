"""存档互通的 golden 用的工作目录（D-28 验收 2）。make_golden.py 用 Go 版 agent 导出它，test_interop.py 用它核对。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FILES: dict[str, bytes] = {
    "README.md": "# 退款服务\n".encode(),
    "empty.txt": b"",
    "src/app/main.py": b"print('hi')\n",
    "src/app/__init__.py": b"",
    "src/lib/data.bin": bytes(range(256)) * 9,
    ".git/HEAD": b"ref: refs/heads/main\n",
    ".git/objects/ab/cdef": b"\x78\x9c" + b"\0" * 30,
    "文档/说明 文件.md": "中文名字与空格\n".encode(),
    ("deep/" + "n" * 60 + "/" + "m" * 60 + "/" + "f" * 80 + ".txt"): b"long path\n",
    "crlf.txt": b"a\r\nb\r\n",
}
MODES = {"src/app/main.py": 0o755, "src/lib/data.bin": 0o600}
EMPTY_DIRS = ["empty-dir", "src/empty"]
DIR_MODES = {"src/empty": 0o700}
SYMLINKS = {"link-to-readme": "README.md", "src/app/up": "../lib", "dangling": "nowhere.txt"}


def make(root: Path) -> None:
    for rel, body in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    for rel in EMPTY_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for rel, mode in MODES.items():
            (root / rel).chmod(mode)
        for rel, mode in DIR_MODES.items():
            (root / rel).chmod(mode)
    if sys.platform == "linux":
        for rel, target in SYMLINKS.items():
            os.symlink(target, root / rel)
