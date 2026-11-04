//! 与 Go 标准库逐字对齐的几个小函数：path.Clean、UTF-8 的逐字节解码、strconv.Quote（%q）、URL 的转义与反转义。
//! agent 的行为（路径规则、错误信息、JSON）要与 Go 版一致（D-28），这些细节不交给近似的实现。

/// Go 的 path.Clean（只认 `/`）。
pub fn clean(p: &str) -> String {
    if p.is_empty() {
        return ".".into();
    }
    let rooted = p.starts_with('/');
    let mut out: Vec<&str> = Vec::new();
    for seg in p.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                if out.last().is_some_and(|s| *s != "..") {
                    out.pop();
                } else if !rooted {
                    out.push("..");
                }
            }
            s => out.push(s),
        }
    }
    let joined = out.join("/");
    match (rooted, joined.is_empty()) {
        (true, _) => format!("/{joined}"),
        (false, true) => ".".into(),
        (false, false) => joined,
    }
}

/// Go 的 path.Dir。
pub fn dir(p: &str) -> String {
    match p.rfind('/') {
        Some(i) => clean(&p[..=i]),
        None => ".".into(),
    }
}

/// Go 的 path.Join（两段）。
pub fn join(a: &str, b: &str) -> String {
    match (a.is_empty(), b.is_empty()) {
        (true, true) => String::new(),
        (true, false) => clean(b),
        (false, true) => clean(a),
        (false, false) => clean(&format!("{a}/{b}")),
    }
}

/// net/http 的 cleanPath：规范化，但保留结尾的 `/`。
pub fn clean_url_path(p: &str) -> String {
    if p.is_empty() {
        return "/".into();
    }
    let p = if p.starts_with('/') {
        p.to_string()
    } else {
        format!("/{p}")
    };
    let mut np = clean(&p);
    if p.ends_with('/') && np != "/" {
        np.push('/');
    }
    np
}

/// 按 Go 的 utf8.DecodeRune 逐个解码：合法的序列照原样，每个不合法的字节换成一个 U+FFFD（与 Go 的 []rune(s)、
/// for range 一致；与 String::from_utf8_lossy 不同，后者把一段不完整的序列合成一个）。
pub fn decode_lossy(mut b: &[u8]) -> String {
    let mut out = String::with_capacity(b.len());
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.push_str(s);
                return out;
            }
            Err(e) => {
                let ok = e.valid_up_to();
                // 前 ok 个字节已验证是 UTF-8。
                out.push_str(std::str::from_utf8(&b[..ok]).unwrap_or_default());
                out.push('\u{FFFD}');
                b = &b[ok + 1..];
            }
        }
    }
}

/// 逐段遍历：合法的 UTF-8 片段与单个不合法的字节（JSON 编码时 Go 把后者写成 `�`）。
pub enum Piece<'a> {
    Valid(&'a str),
    Invalid,
}

pub fn pieces(mut b: &[u8], mut f: impl FnMut(Piece<'_>)) {
    while !b.is_empty() {
        match std::str::from_utf8(b) {
            Ok(s) => {
                f(Piece::Valid(s));
                return;
            }
            Err(e) => {
                let ok = e.valid_up_to();
                if ok > 0 {
                    f(Piece::Valid(
                        std::str::from_utf8(&b[..ok]).unwrap_or_default(),
                    ));
                }
                f(Piece::Invalid);
                b = &b[ok + 1..];
            }
        }
    }
}

/// Go 的 strconv.Quote（错误信息里的 %q）。
pub fn quote(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{7}' => out.push_str("\\a"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{b}' => out.push_str("\\v"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c if c.is_control() => out.push_str(&if (c as u32) < 0x10000 {
                format!("\\u{:04x}", c as u32)
            } else {
                format!("\\U{:08x}", c as u32)
            }),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn unhex(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

/// URL 路径的反转义（url.PathUnescape：`+` 不变）；转义不合法时返回 None。
pub fn path_unescape(s: &str) -> Option<Vec<u8>> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let hi = b.get(i + 1).copied().and_then(unhex)?;
            let lo = b.get(i + 2).copied().and_then(unhex)?;
            out.push(hi << 4 | lo);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    Some(out)
}

/// 查询串的反转义（url.QueryUnescape：`+` 是空格）。
pub fn query_unescape(s: &str) -> Option<Vec<u8>> {
    path_unescape(&s.replace('+', " "))
}

/// url.Values.Get：第一个同名参数的值；解析不了的参数跳过（与 Go 的 ParseQuery 一样只丢那一项）。
pub fn query_get(raw: &str, key: &str) -> Option<String> {
    for part in raw.split('&') {
        if part.is_empty() || part.contains(';') {
            continue;
        }
        let (k, v) = part.split_once('=').unwrap_or((part, ""));
        let (Some(k), Some(v)) = (query_unescape(k), query_unescape(v)) else {
            continue;
        };
        if k == key.as_bytes() {
            return Some(String::from_utf8_lossy(&v).into_owned());
        }
    }
    None
}

/// Go 的 url.URL{Path: p}.String() 对路径的转义（encodePath）：不转义 unreserved 与 `$&+,/:;=@`。
pub fn escape_path(p: &str) -> String {
    let mut out = String::with_capacity(p.len());
    for &c in p.as_bytes() {
        if c.is_ascii_alphanumeric() || b"-_.~$&+,/:;=@".contains(&c) {
            out.push(c as char);
        } else {
            out.push_str(&format!("%{c:02X}"));
        }
    }
    out
}

/// html.EscapeString 的子集（http.Redirect 的正文用）。
pub fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&#34;")
        .replace('\'', "&#39;")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_matches_go() {
        for (inp, want) in [
            ("", "."),
            (".", "."),
            ("a/b", "a/b"),
            ("a//b", "a/b"),
            ("a/./b", "a/b"),
            ("a/../b", "b"),
            ("../a", "../a"),
            ("a/../../b", "../b"),
            ("/../a", "/a"),
            ("/", "/"),
            ("a/", "a"),
            ("./", "."),
            ("..", ".."),
            ("a/b/..", "a"),
            ("//a//", "/a"),
        ] {
            assert_eq!(clean(inp), want, "{inp}");
        }
        assert_eq!(dir("a/b/c"), "a/b");
        assert_eq!(dir("a"), ".");
        assert_eq!(join("a/b", "../c"), "a/c");
        assert_eq!(join(".", "x"), "x");
        assert_eq!(clean_url_path("/v1/files/a//b/"), "/v1/files/a/b/");
        assert_eq!(clean_url_path("/v1/files/../x"), "/v1/x");
    }

    #[test]
    fn decode_like_go() {
        assert_eq!(decode_lossy(b"a\xe2\x82b"), "a\u{FFFD}\u{FFFD}b");
        assert_eq!(decode_lossy("退款".as_bytes()), "退款");
        let mut n = 0;
        pieces(b"\xff\xfeok", |p| {
            if matches!(p, Piece::Invalid) {
                n += 1
            }
        });
        assert_eq!(n, 2);
    }

    #[test]
    fn quote_like_go() {
        assert_eq!(quote("BAD-NAME"), "\"BAD-NAME\"");
        assert_eq!(quote("a\"b\\c\n\x01"), "\"a\\\"b\\\\c\\n\\x01\"");
        assert_eq!(quote("中"), "\"中\"");
    }

    #[test]
    fn url_bits() {
        assert_eq!(path_unescape("a%2Fb%e9%80%80").unwrap(), "a/b退".as_bytes());
        assert!(path_unescape("%zz").is_none());
        assert_eq!(
            query_get("x=1&max_bytes=42&max_bytes=7", "max_bytes").as_deref(),
            Some("42")
        );
        assert_eq!(query_get("max_bytes", "max_bytes").as_deref(), Some(""));
        assert_eq!(escape_path("/v1/files/%2E%2E/y"), "/v1/files/%252E%252E/y");
    }
}
