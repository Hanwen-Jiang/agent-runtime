#!/bin/sh
set -eu
base=/run/swe50-1123-grader
sh /opt/swebench/cache/swe-tuning-20261123/grader-netns.sh
umask 077
mkdir -p "$base/data" "$base/exec"
if ! mountpoint -q "$base/data"; then
    test -z "$(ls -A "$base/data")"
    mount -t tmpfs -o size=28G,nosuid,nodev,mode=700 swe50-1123-grader "$base/data"
fi
cfg=/opt/swebench/cache/swe-tuning-20261123/grader-daemon.json
echo '{}' > "$cfg"
if ! systemctl is-active --quiet swe50-1123-grader-containerd.service; then
    printf 'version = 2
' > "$base/containerd.toml"
    systemd-run --unit=swe50-1123-grader-containerd --collect --property=MemoryMax=36G \
      /usr/sbin/ip netns exec swe50-1123-grader /usr/bin/containerd --config "$base/containerd.toml" --root "$base/data/containerd" --state "$base/containerd-state" --address "$base/containerd.sock"
    for n in $(seq 1 15); do
        test ! -S "$base/containerd.sock" || break
        sleep 1
    done
fi
if ! systemctl is-active --quiet swe50-1123-grader.service; then
    systemd-run --unit=swe50-1123-grader --collect --property=MemoryMax=36G --property=MemoryHigh=30G \
      /usr/sbin/ip netns exec swe50-1123-grader /usr/bin/dockerd --config-file="$cfg" --host="unix://$base/docker.sock" --pidfile="$base/docker.pid" \
      --exec-root="$base/exec" --data-root="$base/data/docker" --containerd="$base/containerd.sock" --containerd-namespace=swe50-1123-grader \
      --containerd-plugins-namespace=swe50-1123-grader-plugins --bip=172.21.241.1/24 --ip6tables=false --userland-proxy=false --group=root
fi
for n in $(seq 1 30); do
    if docker -H "unix://$base/docker.sock" info --format '{{.DockerRootDir}} {{.Driver}}' 2>/dev/null; then exit 0; fi
    sleep 1
done
exit 1
