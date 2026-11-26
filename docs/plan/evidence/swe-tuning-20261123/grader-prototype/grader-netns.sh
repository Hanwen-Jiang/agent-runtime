#!/bin/sh
set -eu
name=swe50-1123-grader
cache=/opt/swebench/cache/swe-tuning-20261123/slirp
helper="$cache/root/usr/bin/slirp4netns"
if ! test -x "$helper"; then
    mkdir -p "$cache/pkg" "$cache/root"
    (cd "$cache/pkg"; apt-get download slirp4netns libslirp0; for file in *.deb; do dpkg-deb -x "$file" "$cache/root"; done)
fi
if ! test -e "/run/netns/$name"; then
    ip netns add "$name"
fi
ip netns exec "$name" ip link set lo up
mkdir -p "/etc/netns/$name"
printf 'nameserver 10.0.2.3\n' > "/etc/netns/$name/resolv.conf"
if ! systemctl is-active --quiet swe50-1123-grader-net.service; then
    systemd-run --unit=swe50-1123-grader-net --collect --property=MemoryMax=512M \
      --setenv="LD_LIBRARY_PATH=$cache/root/usr/lib/x86_64-linux-gnu" \
      "$helper" --netns-type=path --configure --disable-host-loopback "/run/netns/$name" tap0
fi
for n in $(seq 1 20); do
    if ip netns exec "$name" ip route show default | grep -q 'via 10.0.2.2'; then break; fi
    sleep 1
done
status=$(ip netns exec "$name" curl --connect-timeout 5 --max-time 15 -s -o /dev/null -w '%{http_code}' https://registry-1.docker.io/v2/)
test "$status" = 401
