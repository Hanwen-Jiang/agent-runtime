#!/bin/sh
set -eu
base=/run/swe50-1123-grader
cache=/opt/swebench/cache/swe-tuning-20261123
exec 9>"$cache/grader-host.lock"
flock -x 9
python3 - <<'PY'
import json
from pathlib import Path
runs = Path("/opt/swebench/runs")
for name in ("init50-gpt62-tuned-20261123", "mini50-tuned-c-gpt62-20261123"):
    matches = list((runs/name/"grading").glob("*.json"))
    if not any(len(set(json.loads(f.read_bytes()).get("completed_ids", []))) == 50 for f in matches):
        raise SystemExit(f"official grading still pending for {name}")
PY
test "$(findmnt --raw --noheadings --target "$base/data" --output SOURCE)" = swe50-1123-grader
test -z "$(docker -H "unix://$base/docker.sock" container ls --quiet)"
before=$(cat /sys/class/net/docker0/ifindex)
ns=$(ip netns exec swe50-1123-grader readlink /proc/self/ns/net)
test "$ns" != "$(readlink /proc/1/ns/net)"
for unit in swe50-1123-grader.service swe50-1123-grader-containerd.service; do
    pid=$(systemctl show "$unit" -p MainPID --value)
    test "$(readlink "/proc/$pid/ns/net")" = "$ns"
done
systemctl stop swe50-1123-grader.service swe50-1123-grader-containerd.service
systemctl stop swe50-1123-grader-net.service
test -z "$(ip netns pids swe50-1123-grader)"
ip netns del swe50-1123-grader
rm -f /etc/netns/swe50-1123-grader/resolv.conf
rmdir /etc/netns/swe50-1123-grader
umount "$base/data"
test "$(cat /sys/class/net/docker0/ifindex)" = "$before"
