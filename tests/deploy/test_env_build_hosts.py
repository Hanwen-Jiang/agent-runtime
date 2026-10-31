"""按项目定制的运行环境（D-17）：构建经出网网关拉基础镜像，Docker Hub 的镜像层从 CDN 下载。

Docker Hub 按镜像把下载重定向到 production.cloudflare.docker.com 或 production.cloudfront.docker.com。2026-10-31 的 SWE-bench 冒烟里只放行了前一个，
官方镜像的构建全被出网网关拒绝（host_not_granted），Task 退回了通用环境。
"""

from __future__ import annotations

import json

from deploy_tests import CLOUD_VALUES, by_kind, render


def test_the_builder_can_download_from_both_docker_hub_cdns() -> None:
    [config] = [m for m in by_kind(render(CLOUD_VALUES), "ConfigMap") if m["metadata"]["name"].endswith("-sandbox-backends")]
    hosts = json.loads(config["data"]["backends.json"])["environments"]["build_hosts"]
    assert {"registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com", "production.cloudfront.docker.com"} <= set(hosts)
