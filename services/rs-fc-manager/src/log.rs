//! 日志：一行一条写到标准错误（systemd 收进 journald）。只写方法、路径、状态与内部原因，不写请求头、请求体与查询串；
//! 令牌与环境凭据一律不进日志（tests/acceptance.rs 的 tokens_stay_out_of_logs_errors_and_disk）。

use std::io::Write;

pub fn line(level: &str, args: std::fmt::Arguments<'_>) {
    let _ = writeln!(std::io::stderr().lock(), "fc-manager {level}: {args}");
}

#[macro_export]
macro_rules! info {
    ($($t:tt)*) => { $crate::log::line("info", format_args!($($t)*)) };
}

#[macro_export]
macro_rules! warn {
    ($($t:tt)*) => { $crate::log::line("warn", format_args!($($t)*)) };
}
