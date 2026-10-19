# libs

Python 与 Go 的共享库（[强档实施计划](../docs/plan/strong-tier-plan.md) S-02）。

| 内容 | Python（`py-core`，包 `agentruntime_core`） | Go（`go-core`） |
| --- | --- | --- |
| 错误模型 | `errors.py` | `providererr` |
| 租户绑定 | `tenancy.py` | `tenancy` |
| 密钥值 | `secrets.py` | `secret` |
| 时钟、测试支持 | `clock.py`、`testing.py` | `clock`、`testkit` |
| Provider 接口 | `providers/` | `providers/` |
| 一致性测试 | `conformance/` | `conformance/` |
| 开发实现 | `dev/`、`adapters/temporal.py` | `dev/`、`adapters/temporal` |

## 接口的语言划分

| 接口 | Python | Go |
| --- | --- | --- |
| DurableEngine、RelationalStore、Cache、ObjectStore、SecretStore、Telemetry | ✓ | ✓ |
| VectorStore、EventStream、ToolDriver、Sandbox、HumanChannel | ✓ | |
| ModelProvider、Ingress | | ✓ |

两种语言都有的接口，方法一一对应，由 `tests/contract/test_provider_parity.py` 比对。

## 开发实现

全部开发实现都通过了各自的一致性测试（Python `py-core/tests/dev/`，Go `go-core/dev/*/`、`go-core/adapters/temporal/`）。
有过期语义的实现接收时钟，所有开发实现接收故障注入器，在每个公开方法开头检查操作名（Python 用 snake_case 方法名，Go 用方法名）。

- SecretStore 的密钥文件格式与数据密钥包装格式在两种语言间相同（`dev/_keywrap.py` ↔ `dev/filesecrets/keywrap.go`）。
- RelationalStore：Python 用标准库 sqlite3，Go 用纯 Go 的 modernc.org/sqlite；库文件布局相同。
- DurableEngine：两种语言的 Temporal 适配器都把作用域记在 memo 的 `agentruntime_scope` 里，可以互相访问对方启动的工作流。

构造方式：

| 接口 | Python | Go |
| --- | --- | --- |
| Cache | `dev.cache.InMemoryCacheProvider(clock=, faults=)` | `memcache.New(memcache.Options{Clock, Faults})` |
| ObjectStore | `dev.object_store.LocalDiskObjectStoreProvider(root, signing_key=, clock=, faults=)`，另有 `fetch_url`、`upload_url`、`run_lifecycle` | `diskobjects.New(Options{Root, SigningKey, Clock, Faults})`，另有 `FetchURL`、`UploadURL`、`RunLifecycle` |
| SecretStore | `dev.secret_store.LocalFileSecretStoreProvider(path, max_ttl=, clock=, faults=)`，另有 `seed_secret`、`grant_role` | `filesecrets.New(Options{Path, MaxTTL, Clock, Faults})`，另有 `SeedSecret`、`GrantRole` |
| Telemetry | `dev.telemetry.ConsoleTelemetryProvider(stream=, clock=, faults=)`，记录在 `exported_spans`、`exported_metrics`、`exported_logs` | `consoletelemetry.New(Options{Writer, Faults})`，记录由 `Spans()`、`Metrics()`、`Logs()` 返回 |
| RelationalStore | `dev.relational_store.SQLiteRelationalStoreProvider(directory, faults=)` | `sqlitestore.New(Options{Dir, Faults})` |
| DurableEngine | `adapters.temporal.TemporalDurableEngineProvider(client)` | `temporaladapter.New(client)` |
| VectorStore | `dev.vector_store.InMemoryVectorStoreProvider(faults=)` | |
| EventStream | `dev.event_stream.InMemoryEventStreamProvider(clock=, faults=)` | |
| ToolDriver | `dev.tool_driver.LocalToolDriverProvider(faults=)`，另有 `register`、`grant` | |
| Sandbox | `dev.sandbox.LocalSubprocessSandboxProvider(root, faults=)` | |
| HumanChannel | `dev.human_channel.CliHumanChannelProvider(sink, output=, clock=, faults=)`，另有 `receive_answer` | |
| ModelProvider | | `mockmodel.New(Options{Faults})`，模型名常量 `GenerateModel`、`EmbeddingModel`，另有 `Calls()` |
| Ingress | | `localingress.New(Options{SigningKey, Clock, Limit, Window})`，另有 `IssueToken` |
