//! fc-manager 的入口（说明见 lib.rs）。
//!
//! ```text
//! fc-manager serve     清扫上一个进程留下的 VM，然后起服务
//! fc-manager sweep     只清扫（systemd 的 ExecStopPost；serve 在跑时拿不到锁，不会误杀）
//! fc-manager check     核对配置、宿主与 SecretStore 里的令牌
//! fc-manager version
//! ```

use std::process::ExitCode;

fn main() -> ExitCode {
    let cmd = std::env::args().nth(1).unwrap_or_default();
    match cmd.as_str() {
        "version" => {
            println!("{}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        "serve" | "sweep" | "check" => run(&cmd),
        _ => {
            eprintln!("用法：fc-manager serve | sweep | check | version");
            ExitCode::from(2)
        }
    }
}

#[cfg(target_os = "linux")]
fn run(cmd: &str) -> ExitCode {
    match fc_manager::app::run(cmd) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("fc-manager：{e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn run(_cmd: &str) -> ExitCode {
    eprintln!("fc-manager 只在 Linux 上运行");
    ExitCode::from(2)
}
