"""黑盒测试里 agent 执行的命令：python helper.py <模式> [参数…]。只用标准库，Linux 与 Windows 都能跑。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def beat(path: str) -> None:
    # 不停地往文件里追加；文件不再变长说明进程停了。到时间自己退出，不留在机器上。
    end = time.monotonic() + 30
    while time.monotonic() < end:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(".")
        except OSError:
            pass
        time.sleep(0.05)


def probe() -> None:
    # 命令一侧看到的父进程（agent）的 /proc（AGENT-TOK-01）。
    pid = os.getppid()
    out: dict[str, object] = {"pid": pid, "environ": "", "environ_err": "", "cmdline": "", "mem_opened": False, "self_readable": False}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            out["environ"] = f.read().decode("utf-8", "replace")
    except OSError as e:
        out["environ_err"] = str(e)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            out["cmdline"] = f.read().decode("utf-8", "replace")
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/mem", "rb"):
            out["mem_opened"] = True
    except OSError:
        pass
    try:
        with open("/proc/self/environ", "rb") as f:
            f.read()
        out["self_readable"] = True
    except OSError:
        pass
    sys.stdout.write(json.dumps(out))


def main(mode: str, args: list[str]) -> int:
    out = sys.stdout.buffer
    if mode == "echo-upper":
        out.write(sys.stdin.buffer.read().upper())
    elif mode == "print":
        out.write(args[0].encode())
    elif mode == "fail":
        sys.stderr.buffer.write(args[1].encode())
        return int(args[0])
    elif mode == "sleep":
        time.sleep(float(args[0]))
    elif mode == "burn":
        end = time.process_time() + float(args[0])
        x = 0
        while time.process_time() < end:
            x += 1
        out.write(b"true" if x else b"false")
    elif mode == "env":
        out.write(os.environ.get(args[0], "<unset>").encode())
    elif mode == "env-names":
        out.write(json.dumps(sorted(os.environ)).encode())
    elif mode == "cwd":
        out.write(os.getcwd().encode())
    elif mode == "cat":
        try:
            with open(args[0], "rb") as f:
                out.write(f.read())
        except OSError:
            return 2
    elif mode == "write":
        with open(args[0], "w", encoding="utf-8") as f:
            f.write(args[1])
    elif mode == "beat":
        beat(args[0])
    elif mode == "tree":
        # 起一个子进程（同一个进程组）再自己也跳：断开或取消时两个都要停。
        subprocess.Popen([sys.executable, __file__, "beat", args[0] + ".child"])
        beat(args[0])
    elif mode == "spam":
        out.write(b"x" * int(args[0]) + b"END")
    elif mode == "both":
        out.write(b"o" * int(args[0]))
        out.flush()
        sys.stderr.buffer.write(b"e" * int(args[0]))
    elif mode == "signal":
        out.flush()
        os.kill(os.getpid(), getattr(signal, args[0]))
        time.sleep(5)
    elif mode == "orphan":
        # 留一个占着输出管道的后台进程（另一个会话，杀进程组杀不到）：agent 等它最多 WaitDelay 就返回。
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        subprocess.Popen([sys.executable, __file__, "sleep", args[0]], **kwargs)
        out.write(b"parent-done")
    elif mode == "probe":
        probe()
    else:
        sys.stderr.write(f"unknown mode {mode}")
        return 99
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
