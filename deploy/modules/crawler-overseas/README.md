# Optional Overseas Crawler Module (`crawler-overseas`)

默认主集群（`ar-node-1`）通过商业/API 方式直连 WebSearch 上游（支持 `tavily`、`brave`、`serper`、`exa`、`bocha`，以及零密钥兜底 `ddg_lite`），**不强制依赖海外 VPS**。

当以后需要自建海外动态 JS 渲染（Headless Chromium）或自建元搜索（SearXNG）时，可按需启用此可选模块。

## 模式 A：在任意海外 Docker 主机独立部署

```bash
cd deploy/modules/crawler-overseas
cp .env.example .env   # 填入 AR_HEADLESS_TOKEN / SEARXNG_SECRET（勿提交）
docker compose up -d --build
```

然后在主集群 Helm values 中填入公网/隧道地址：

```yaml
crawlerGateway:
  searchProvider: auto # 或 searxng
  searchUpstreamUrl: "https://searxng.your-domain.example"
  headlessUrl: "https://headless.your-domain.example"
  headlessTokenSecret:
    name: crawler-gateway-secret
    key: headless-token
```

## 模式 B：直接启用集群内 Helm 伴生组件

若集群本身位于海外网络且内存充足，使用同目录 `helm-values-overseas.yaml` 覆盖：

```bash
helm upgrade ar deploy/helm/agent-runtime -n agent-runtime \
  -f /opt/ar-cluster/ar-values-cluster.yaml \
  -f deploy/modules/crawler-overseas/helm-values-overseas.yaml
```
