//! JSON：读用 serde_json 的 Value，再按 Go 的 encoding/json 的规矩取字段；写自己拼，与 Go 的输出逐字节一致
//! （字符串里 `<`、`>`、`&`、U+2028、U+2029 转义，不合法的 UTF-8 字节写成 `�`，末尾一个换行）。

use serde_json::{Map, Value};

use crate::gostr::{Piece, pieces};

/// 写 JSON 字符串（带引号）。
pub fn str_bytes(out: &mut String, b: &[u8]) {
    out.push('"');
    pieces(b, |p| match p {
        Piece::Valid(s) => escape_into(out, s),
        Piece::Invalid => out.push_str("\\ufffd"),
    });
    out.push('"');
}

pub fn str(out: &mut String, s: &str) {
    str_bytes(out, s.as_bytes());
}

fn escape_into(out: &mut String, s: &str) {
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '<' | '>' | '&' => out.push_str(&format!("\\u00{:02x}", c as u32)),
            '\u{2028}' | '\u{2029}' => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u00{:02x}", c as u32)),
            c => out.push(c),
        }
    }
}

/// 一个 JSON 对象，按写入的顺序输出键（Go 的 struct 按字段顺序，map 按键排序：调用方按需要的顺序写）。
pub struct Obj {
    buf: String,
    first: bool,
}

impl Default for Obj {
    fn default() -> Self {
        Self::new()
    }
}

impl Obj {
    pub fn new() -> Self {
        Obj {
            buf: String::from("{"),
            first: true,
        }
    }

    fn key(&mut self, k: &str) -> &mut String {
        if !self.first {
            self.buf.push(',');
        }
        self.first = false;
        str(&mut self.buf, k);
        self.buf.push(':');
        &mut self.buf
    }

    pub fn s(mut self, k: &str, v: &str) -> Self {
        str(self.key(k), v);
        self
    }

    pub fn b(mut self, k: &str, v: &[u8]) -> Self {
        str_bytes(self.key(k), v);
        self
    }

    pub fn i(mut self, k: &str, v: i64) -> Self {
        self.key(k).push_str(&v.to_string());
        self
    }

    pub fn opt_i(mut self, k: &str, v: Option<i64>) -> Self {
        let o = self.key(k);
        match v {
            Some(v) => o.push_str(&v.to_string()),
            None => o.push_str("null"),
        }
        self
    }

    pub fn bool(mut self, k: &str, v: bool) -> Self {
        self.key(k).push_str(if v { "true" } else { "false" });
        self
    }

    /// 已经是 JSON 的值（数组、对象）。
    pub fn raw(mut self, k: &str, v: &str) -> Self {
        self.key(k).push_str(v);
        self
    }

    pub fn done(mut self) -> String {
        self.buf.push('}');
        self.buf
    }
}

pub fn array(items: impl IntoIterator<Item = String>) -> String {
    let mut out = String::from("[");
    for (i, it) in items.into_iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&it);
    }
    out.push(']');
    out
}

// --- 读 ---

/// 解析请求体里的第一个 JSON 值（Go 的 json.Decoder.Decode 只读第一个值，后面的不管）。
pub fn parse_first(body: &[u8]) -> Option<Value> {
    serde_json::Deserializer::from_slice(body)
        .into_iter::<Value>()
        .next()?
        .ok()
}

/// 字段类型不对（Go 的 UnmarshalTypeError）。
#[derive(Debug)]
pub struct TypeError;

/// 把值当作对象：null 是空对象（Go 把 null 解进 struct 什么都不做），别的类型报错。
pub fn object(v: &Value) -> Result<Option<&Map<String, Value>>, TypeError> {
    match v {
        Value::Object(m) => Ok(Some(m)),
        Value::Null => Ok(None),
        _ => Err(TypeError),
    }
}

/// 按 Go 的规矩找字段：先找名字完全相同的，再找不分大小写相同的。
pub fn field<'a>(m: Option<&'a Map<String, Value>>, name: &str) -> Option<&'a Value> {
    let m = m?;
    if let Some(v) = m.get(name) {
        return Some(v);
    }
    m.iter()
        .find(|(k, _)| k.to_lowercase() == name.to_lowercase())
        .map(|(_, v)| v)
}

pub fn get_string(v: Option<&Value>) -> Result<String, TypeError> {
    match v {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        _ => Err(TypeError),
    }
}

pub fn get_i64(v: Option<&Value>) -> Result<i64, TypeError> {
    match v {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Number(n)) => n.as_i64().ok_or(TypeError),
        _ => Err(TypeError),
    }
}

pub fn get_strings(v: Option<&Value>) -> Result<Vec<String>, TypeError> {
    match v {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(a)) => a.iter().map(|x| get_string(Some(x))).collect(),
        _ => Err(TypeError),
    }
}

/// map[string]string：null 的值是空串。返回按键排序的对。
pub fn get_string_map(v: Option<&Value>) -> Result<Option<Vec<(String, String)>>, TypeError> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Object(m)) => m
            .iter()
            .map(|(k, v)| Ok((k.clone(), get_string(Some(v))?)))
            .collect::<Result<Vec<_>, _>>()
            .map(Some),
        _ => Err(TypeError),
    }
}

/// []byte：标准 base64（带填充）；null 是空。
pub fn get_bytes(v: Option<&Value>) -> Result<Vec<u8>, TypeError> {
    match v {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::String(s)) => b64_decode(s).ok_or(TypeError),
        _ => Err(TypeError),
    }
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn b64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// 与 Go 的 base64.StdEncoding.Decode 一致：忽略 \r 与 \n，要求填充，末尾多余的位不检查。
pub fn b64_decode(s: &str) -> Option<Vec<u8>> {
    let clean: Vec<u8> = s.bytes().filter(|&c| c != b'\r' && c != b'\n').collect();
    if !clean.len().is_multiple_of(4) {
        return None;
    }
    let val = |c: u8| B64.iter().position(|&x| x == c).map(|p| p as u32);
    let mut out = Vec::with_capacity(clean.len() / 4 * 3);
    let n = clean.len() / 4;
    for (i, q) in clean.chunks(4).enumerate() {
        let last = i + 1 == n;
        let pads = q.iter().rev().take_while(|&&c| c == b'=').count();
        if pads > 2 || (pads > 0 && !last) || q[..4 - pads].contains(&b'=') {
            return None;
        }
        let mut acc = 0u32;
        for (j, &c) in q.iter().enumerate() {
            acc <<= 6;
            if j < 4 - pads {
                acc |= val(c)?;
            }
        }
        let bytes = [(acc >> 16) as u8, (acc >> 8) as u8, acc as u8];
        out.extend_from_slice(&bytes[..3 - pads]);
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strings_are_escaped_like_go() {
        let mut o = String::new();
        str_bytes(&mut o, b"a<b>&\"\\\n\x01\xff\xe2\x80\xa8\xe9\x80\x80");
        assert_eq!(
            o,
            "\"a\\u003cb\\u003e\\u0026\\\"\\\\\\n\\u0001\\ufffd\\u2028退\""
        );
        let obj = Obj::new()
            .s("code", "x")
            .opt_i("exit_code", None)
            .bool("t", true)
            .raw("a", &array(vec!["1".into()]))
            .done();
        assert_eq!(obj, r#"{"code":"x","exit_code":null,"t":true,"a":[1]}"#);
    }

    #[test]
    fn base64_round_trips() {
        for data in [&b""[..], b"a", b"ab", b"abc", b"abcd", "退款".as_bytes()] {
            assert_eq!(b64_decode(&b64_encode(data)).unwrap(), data);
        }
        assert_eq!(b64_encode(b"ab"), "YWI=");
        assert_eq!(b64_decode("YW\nI=").unwrap(), b"ab");
        assert!(b64_decode("YWI").is_none());
        assert!(b64_decode("!!!!").is_none());
        assert!(b64_decode("YQ==YQ==").is_none());
        assert_eq!(b64_decode("YR==").unwrap(), b"a", "多余的位不检查");
    }

    #[test]
    fn fields_like_go() {
        let v = parse_first(br#"{"ARGV": ["x"], "timeout_ms": 5} trailing"#).unwrap();
        let m = object(&v).unwrap();
        assert_eq!(get_strings(field(m, "argv")).unwrap(), vec!["x"]);
        assert_eq!(get_i64(field(m, "timeout_ms")).unwrap(), 5);
        assert!(get_i64(Some(&serde_json::json!(1.5))).is_err());
        assert!(object(&serde_json::json!([1])).is_err());
        assert!(object(&Value::Null).unwrap().is_none());
    }
}
