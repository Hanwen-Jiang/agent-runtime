//! fc-manager 的 API 令牌：SecretStore 平台作用域的 `sandbox/fc-manager-token`。文件格式与开发 SecretStore 相同
//! （libs/go-core/dev/filesecrets、agentruntime_core.dev.secret_store）：
//!
//! ```text
//! {"version": 1, "scopes": {"_platform": {"secrets": {"sandbox/fc-manager-token": "…"}, …}, …}}
//! ```
//!
//! 宿主上这个文件只给 root 读（deploy/cluster/fc-manager.service）；Worker 从同一个作用域取同一个值。文件变了（换令牌）
//! 下一个请求就按新的核对，不用重启（重启会终止所有 VM）。错误信息里只有路径与原因，没有值。

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;

use serde::Deserialize;

pub const TOKEN_NAME: &str = "sandbox/fc-manager-token";
pub const MIN_TOKEN_LEN: usize = 32;

/// 令牌。Debug 不打印值，也没有 Display。
#[derive(Clone)]
pub struct Token(String);

impl Token {
    pub fn new(value: String) -> Token {
        Token(value)
    }

    pub fn expose(&self) -> &str {
        &self.0
    }

    /// 定长比较（长度不同直接不等，与 Go 的 subtle.ConstantTimeCompare 一样）。
    pub fn matches(&self, presented: &str) -> bool {
        let (a, b) = (self.0.as_bytes(), presented.as_bytes());
        if a.len() != b.len() {
            return false;
        }
        a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
    }
}

impl std::fmt::Debug for Token {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Token(***)")
    }
}

#[derive(Deserialize)]
struct SecretsFile {
    #[serde(default)]
    scopes: BTreeMap<String, ScopeEntry>,
}

#[derive(Deserialize)]
struct ScopeEntry {
    #[serde(default)]
    secrets: BTreeMap<String, String>,
}

pub fn read_token(path: &Path) -> Result<Token, String> {
    let data = fs::read(path)
        .map_err(|e| format!("读不了 AR_SECRETS_FILE（{}）：{}", path.display(), e.kind()))?;
    // serde 的错误里可能带着出错位置附近的值，不往外说。
    let file: SecretsFile = serde_json::from_slice(&data)
        .map_err(|_| format!("AR_SECRETS_FILE（{}）不是 SecretStore 文件", path.display()))?;
    file.scopes
        .get("_platform")
        .and_then(|s| s.secrets.get(TOKEN_NAME))
        .filter(|v| v.len() >= MIN_TOKEN_LEN)
        .map(|v| Token(v.clone()))
        .ok_or_else(|| {
            format!("SecretStore 平台作用域的 {TOKEN_NAME} 缺失或短于 {MIN_TOKEN_LEN} 个字符")
        })
}

type Stamp = (SystemTime, u64);

fn stamp(path: &Path) -> Option<Stamp> {
    let m = fs::metadata(path).ok()?;
    Some((m.modified().ok()?, m.len()))
}

/// 按文件的修改时间与大小判断要不要重新读；读坏了沿用之前的令牌（并记日志），不让一次写坏把服务锁死。
pub struct TokenSource {
    path: PathBuf,
    state: Mutex<(Option<Stamp>, Token)>,
}

impl TokenSource {
    pub fn load(path: &Path) -> Result<TokenSource, String> {
        let st = stamp(path);
        let token = read_token(path)?;
        Ok(TokenSource {
            path: path.to_path_buf(),
            state: Mutex::new((st, token)),
        })
    }

    pub fn check(&self, presented: &str) -> bool {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let now = stamp(&self.path);
        if now.is_some() && now != st.0 {
            st.0 = now;
            match read_token(&self.path) {
                Ok(t) => st.1 = t,
                Err(e) => crate::warn!("{e}；沿用之前的令牌"),
            }
        }
        st.1.matches(presented)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str, body: &str) -> PathBuf {
        let p =
            std::env::temp_dir().join(format!("fcm-secrets-{}-{name}.json", std::process::id()));
        fs::write(&p, body).unwrap();
        p
    }

    #[test]
    fn reads_the_platform_token_and_hides_it() {
        let value = "v".repeat(40);
        let p = tmp(
            "ok",
            &format!(
                r#"{{"version":1,"scopes":{{"_platform":{{"secrets":{{"{TOKEN_NAME}":"{value}"}},"roles":[]}}}}}}"#
            ),
        );
        let t = read_token(&p).unwrap();
        assert!(t.matches(&value));
        assert!(!t.matches(&value[1..]));
        assert!(!t.matches(&format!("{value}x")));
        assert_eq!(format!("{t:?}"), "Token(***)");
        let _ = fs::remove_file(p);
    }

    #[test]
    fn errors_never_contain_values() {
        let short = "short-secret-value";
        for (name, body) in [
            (
                "short",
                format!(
                    r#"{{"scopes":{{"_platform":{{"secrets":{{"{TOKEN_NAME}":"{short}"}}}}}}}}"#
                ),
            ),
            (
                "tenant",
                format!(
                    r#"{{"scopes":{{"t1":{{"secrets":{{"{TOKEN_NAME}":"{}"}}}}}}}}"#,
                    "x".repeat(40)
                ),
            ),
            ("broken", format!(r#"{{"scopes": [{short}"#)),
        ] {
            let p = tmp(name, &body);
            let e = read_token(&p).unwrap_err();
            assert!(!e.contains(short), "{e}");
            let _ = fs::remove_file(p);
        }
        let e = read_token(Path::new("/nonexistent/fcm/secrets.json")).unwrap_err();
        assert!(e.contains("AR_SECRETS_FILE"));
    }
}
