//! fc-manager：宿主上管 Firecracker microVM 的服务（D-20，docs/plan/firecracker-backend.md）。每个沙箱环境一台 VM，
//! 由 jailer 起 firecracker（非 root、chroot、独立的 PID 与网络命名空间、cgroup v2 的上限、firecracker 自带的 seccomp），
//! 客户机里的 sandbox-agent 只经 vsock 可达。对外的 HTTP 接口与 Cloudflare 桥同形（deploy/cloudflare/sandbox-bridge）：
//!
//! ```text
//! GET    /healthz                              不要令牌
//! GET    /v1/envs                              本机在跑的环境（X-Sandbox-Owner 要是 _platform）→ {"environments": […]}
//! POST   /v1/envs                              {"runtime", "cpu_millis", "memory_mb", "disk_mb"?, "labels"?} → 201 {"env_id", …}
//! DELETE /v1/envs/<env_id>                     销毁；已经没了也是 204
//! *      /v1/envs/<env_id>/agent/<agent 路径>   原样转给客户机里的 agent（/v1/exec、/v1/files/…），带上这台 VM 的 agent 令牌
//! POST   /v1/envs/<env_id>/preview             {"port"} → {"address": "IP:端口"}：客户机这个端口的预览入口（D-22，net.rs）
//! ```
//!
//! 认证：`Authorization: Bearer <令牌>`，令牌是 SecretStore 平台作用域的 `sandbox/fc-manager-token`（`AR_SECRETS_FILE`，
//! 文件变了就重新读，换令牌不用重启）。作用域：`X-Sandbox-Owner`（租户 ID 或 `_platform`），建时记下，之后每个请求核对，
//! 不对就是 404。错误一律 `{"code", "message"}`；超过宿主容量、启动失败是 503（transient），带 `Retry-After`。
//!
//! fc-manager 重启时**终止**上一个进程留下的 VM，不接管（manager.rs 写了理由）；systemd 的 ExecStopPost 再跑一次
//! `fc-manager sweep`。agent 令牌每台 VM 随机生成、只在内存里，经 vsock 交给客户机（不进内核命令行、磁盘与日志）。
//!
//! 只在 Linux 上运行。可移植的部分（HTTP 解析、配置、规格校验、SecretStore 文件）在别的平台上也编译，单元测试照跑。

#[cfg(target_os = "linux")]
pub mod blob;
pub mod blob_cache;
pub mod config;
pub mod http;
pub mod log;
pub mod secrets;
pub mod spec;
pub mod timefmt;

#[cfg(target_os = "linux")]
pub mod app;
#[cfg(target_os = "linux")]
pub mod manager;
#[cfg(target_os = "linux")]
pub mod net;
#[cfg(target_os = "linux")]
pub mod proxy;
#[cfg(target_os = "linux")]
pub mod server;
#[cfg(target_os = "linux")]
pub mod store;
#[cfg(target_os = "linux")]
pub mod sys;
#[cfg(target_os = "linux")]
pub mod vm;
#[cfg(target_os = "linux")]
pub mod vsock;
