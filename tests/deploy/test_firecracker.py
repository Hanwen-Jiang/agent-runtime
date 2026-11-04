"""D-20：Firecracker 后端在宿主上的部署件（docs/plan/firecracker-backend.md）。

静态检查（总是跑）：
- 客户机内核（deploy/images/firecracker）：版本固定且发布满两周、源码与 guest 配置都按 sha256 核对、先核对再解包；
  叠上的配置不要模块、有串口控制台、没有网卡驱动，构建后逐项核对还在。
- Firecracker 的发布包：版本与 sha256 在 versions.conf，install.sh 按这里的值核对（不信下载来的校验文件）。
- fc-manager.service：仿 ar-firewall.service，只在有 /dev/kvm 时起、排在防火墙与 k3s 之后，启动前与退出后各清扫一次，
  单元与配置样例里没有密钥；install.sh 核对 /dev/kvm 与 fc-manager 二进制的哈希。
真 VM 的验收在 services/rs-fc-manager/tests/real_vm.rs（AR_TEST_FIRECRACKER=1）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from deploy_tests import REPO_ROOT

CLUSTER = REPO_ROOT / "deploy" / "cluster"
KERNEL = REPO_ROOT / "deploy" / "images" / "firecracker"
CRATE = REPO_ROOT / "services" / "rs-fc-manager"
HEX64 = re.compile(r"[0-9a-f]{64}")


def conf(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text("utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """systemd 单元：节 → 键 → 值（同一个键可以出现多次）。"""
    sections: dict[str, dict[str, list[str]]] = {}
    current = ""
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        key, _, value = line.partition("=")
        sections[current].setdefault(key.strip(), []).append(value.strip())
    return sections


def section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i)]


# --- 客户机内核 ---


def test_kernel_is_pinned_two_weeks_old_and_checksummed() -> None:
    k = conf(KERNEL / "kernel.conf")
    assert re.fullmatch(r"\d+\.\d+\.\d+", k["KERNEL_VERSION"]), k["KERNEL_VERSION"]
    assert HEX64.fullmatch(k["KERNEL_SHA256"]) and HEX64.fullmatch(k["FC_CONFIG_SHA256"])
    released, pinned = date.fromisoformat(k["KERNEL_RELEASED"]), date.fromisoformat(k["PINNED_ON"])
    assert pinned - released >= timedelta(days=14), "内核版本要发布满两周"
    assert pinned <= date.today()
    # guest 配置取 Firecracker 的发布标签（不是分支），与宿主上装的 Firecracker 同一个版本，且对应内核的大版本。
    assert re.fullmatch(r"v\d+\.\d+\.\d+", k["FC_CONFIG_REF"])
    assert k["FC_CONFIG_REF"] == conf(CLUSTER / "versions.conf")["FIRECRACKER_VERSION"]
    major_minor = ".".join(k["KERNEL_VERSION"].split(".")[:2])
    assert k["FC_CONFIG_NAME"] == f"microvm-kernel-ci-x86_64-{major_minor}.config"


def test_kernel_fragment_has_no_modules_a_serial_console_and_no_nic() -> None:
    lines = set((KERNEL / "agentruntime.config").read_text("utf-8").splitlines())
    for want in [
        "# CONFIG_MODULES is not set",
        "CONFIG_SERIAL_8250_CONSOLE=y",
        "CONFIG_VIRTIO_VSOCKETS=y",
        "CONFIG_VIRTIO_BLK=y",
        "CONFIG_EXT4_FS=y",
        "CONFIG_OVERLAY_FS=y",
        "CONFIG_DEVTMPFS_MOUNT=y",
        "# CONFIG_VIRTIO_NET is not set",
        "# CONFIG_VSOCKETS_LOOPBACK is not set",
    ]:
        assert want in lines, want
    assert not any(line.endswith("=m") for line in lines), "不要模块"


def test_build_script_verifies_before_unpacking_and_checks_the_result() -> None:
    text = (KERNEL / "build-kernel.sh").read_text("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    assert "\r\n" not in text
    assert 'source "$HERE/kernel.conf"' in text
    kernel_check = text.index('"${KERNEL_SHA256}  ')
    config_check = text.index('"${FC_CONFIG_SHA256}  ')
    unpack = text.index("tar -xJf")
    assert kernel_check < unpack and config_check < unpack, "先核对哈希再解包"
    assert text.count("sha256sum -c") >= 2
    assert "merge_config.sh" in text and "olddefconfig" in text
    assert "grep -qxF" in text, "叠上的每一项构建后都要还在"
    assert "'=m$'" in text, "最终配置里不能有模块"
    assert re.search(r"make .*vmlinux", text) and "vmlinux.sha256" in text
    assert "kernelversion" in text, "解出来的源码版本要对得上"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上的 bash 是 WSL 的，读不到网络盘；CI（Linux）上检查语法")
def test_build_script_parses() -> None:
    bash = shutil.which("bash")
    assert bash, "要有 bash"
    subprocess.run([bash, "-n", KERNEL / "build-kernel.sh"], check=True)


# --- Firecracker 的发布包与 install.sh ---


def test_firecracker_release_is_pinned_and_checked_against_versions_conf() -> None:
    v = conf(CLUSTER / "versions.conf")
    assert re.fullmatch(r"v\d+\.\d+\.\d+", v["FIRECRACKER_VERSION"])
    assert HEX64.fullmatch(v["FIRECRACKER_SHA256"])
    text = (CLUSTER / "install.sh").read_text("utf-8")
    block = section(text, "# --- 3b.", "# --- 4.")
    check = block.index('"${FIRECRACKER_SHA256}  ')
    assert check < block.index("tar -xzf"), "先核对再解包"
    assert ".sha256.txt" not in block, "按 versions.conf 的值核对，不用下载来的校验文件"
    assert "install -m 0755" in block and "/usr/local/bin/jailer" in block and "/usr/local/bin/firecracker" in block


def test_install_checks_kvm_and_the_manager_binary_hash() -> None:
    block = section((CLUSTER / "install.sh").read_text("utf-8"), "# --- 3b.", "# --- 4.")
    assert block.splitlines()[3].startswith("if [[ -e /dev/kvm ]]"), "整段只在有 /dev/kvm 时跑"
    assert "-r /dev/kvm && -w /dev/kvm" in block
    assert 'FC_MANAGER_SHA256:-' in block and "die" in block, "给了 FC_MANAGER_BIN 就要给哈希"
    assert block.index('"${FC_MANAGER_SHA256}  ${FC_MANAGER_BIN}"') < block.index('install -m 0755 "$FC_MANAGER_BIN"'), "先核对再装"
    assert '"$HERE/fc-manager.service" /etc/systemd/system/fc-manager.service' in block
    # 配置样例只在没有时拷，不覆盖人改过的；密钥文件不由脚本生成。
    assert "[[ -f /etc/agentruntime/fc-manager.env ]] ||" in block
    assert "fc-manager-secrets.json" in block and "install -m 0600" in block
    assert "fc-manager check" in block


# --- systemd 单元与配置样例 ---


def test_unit_is_modelled_on_the_firewall_unit_and_sweeps_on_both_ends() -> None:
    fw = unit(CLUSTER / "ar-firewall.service")
    u = unit(CLUSTER / "fc-manager.service")
    assert set(u) == {"Unit", "Service", "Install"} == set(fw)
    assert u["Install"]["WantedBy"] == fw["Install"]["WantedBy"] == ["multi-user.target"]
    assert u["Unit"]["ConditionPathExists"] == ["/dev/kvm"]
    after = " ".join(u["Unit"]["After"]).split()
    assert "ar-firewall.service" in after and "k3s.service" in after
    assert u["Unit"]["Requires"] == ["ar-firewall.service"], "防火墙没装好不起"
    s = u["Service"]
    assert s["ExecStart"] == ["/usr/local/bin/fc-manager serve"]
    assert s["ExecStartPre"] == ["/usr/local/bin/fc-manager sweep"]
    assert s["ExecStopPost"] == ["/usr/local/bin/fc-manager sweep"], "主进程怎么退出都清扫，不留孤儿 VM"
    assert s["Restart"] == ["always"]
    assert s["EnvironmentFile"] == ["/etc/agentruntime/fc-manager.env"]
    assert s["NoNewPrivileges"] == ["yes"]
    # 这些会给服务单独建挂载命名空间：ip netns 的绑定挂载就不在宿主上了，ExecStopPost 清不掉。
    for key in ("ProtectSystem", "PrivateTmp", "PrivateMounts", "ProtectHome"):
        assert key not in s, key
    assert "Environment" not in s, "配置在 EnvironmentFile，密钥在 SecretStore 文件"
    text = (CLUSTER / "fc-manager.service").read_text("utf-8")
    assert "\r\n" not in text and "TOKEN=" not in text.upper()


def test_env_example_holds_no_secrets_and_only_known_settings() -> None:
    env = conf(CLUSTER / "fc-manager.env.example")
    known = set(re.findall(r'"(AR_[A-Z0-9_]+)"', (CRATE / "src" / "config.rs").read_text("utf-8")))
    assert set(env) <= known, f"fc-manager 不认的变量：{set(env) - known}"
    assert not [k for k in env if re.search(r"TOKEN|KEY|PASSWORD|SECRET$", k)], "密钥不放在配置里"
    assert env["AR_SECRETS_FILE"].startswith("/etc/agentruntime/")
    host = env["AR_FC_LISTEN"].rsplit(":", 1)[0]
    assert host not in ("0.0.0.0", "::", "[::]"), "只听 cni0 的地址，不听全部网卡"
    # jail、内核与 rootfs 要在同一个文件系统上（硬链接）：放在同一个目录下。
    base = "/var/lib/agentruntime/fc/"
    for key in ("AR_FC_CHROOT_BASE", "AR_FC_IMAGES_DIR", "AR_FC_KERNEL", "AR_FC_STATE_DIR"):
        assert env[key].startswith(base), key


def test_token_name_matches_the_plan() -> None:
    plan = (REPO_ROOT / "docs" / "plan" / "firecracker-backend.md").read_text("utf-8")
    secrets = (CRATE / "src" / "secrets.rs").read_text("utf-8")
    assert '"sandbox/fc-manager-token"' in secrets and "sandbox/fc-manager-token" in plan
