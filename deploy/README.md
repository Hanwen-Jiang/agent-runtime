# deploy

Helm Chart 与各环境的配置（设计书 4.15、附录 L.19）。

| 目录 | 内容 |
| --- | --- |
| `helm/agent-runtime` | Chart（T-15，细化见 `docs/plan/deploy-telemetry.md`）：task-service（Task API 与单独的 Registry 内部端口）、Model Gateway、Memory Service、按任务队列分池的 Worker（每池一个 KEDA ScaledObject，temporal 触发器）、Learning Pipeline 的 KEDA ScaledJob（kafka 触发器）、OpenTelemetry Collector |
| `helm/agent-runtime/files/otel-collector.yaml` | Collector 的配置：全部 span 到 Tempo，语义 span 到 Langfuse 与 Redpanda（主题 `telemetry.spans`，供 Learning Pipeline 消费）；本地也能直接拿去跑 |
| `helm/agent-runtime/values-production.example.yaml` | 生产 values 的样例 |
| `dev` | 本地开发用的配置（开发用 Model Profile） |

第三方组件（Temporal、Postgres、Qdrant、Redpanda、Langfuse、Tempo）不在 Chart 里，只在 values 的 `external` 填地址；密钥只引用已有的 Secret。
镜像与 Dockerfile 还没有，Chart 里的镜像名是占位。

```powershell
helm lint --strict deploy/helm/agent-runtime
helm template ar deploy/helm/agent-runtime --values deploy/helm/agent-runtime/values-production.example.yaml
```

结构检查（队列名、环境变量名与代码一致，Registry 端口不外露，没有明文密钥，容器的安全约束，Collector 的三路导出）在 `tests/deploy`。
