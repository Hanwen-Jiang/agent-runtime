//! /v1/grep 的正则：语法与语义按 Go 的 regexp（RE2，Go 1.26 的 regexp/syntax），用 regex crate 执行。
//!
//! 两者大体相同，差别在细处（代审记录 2026-11-04 第 4 节要求 Go 与 Rust 的正则语义一致）。这里把 Go 的写法逐字解析一遍、
//! 改写成意思相同的 regex 写法，Go 不收的一律报错（`invalid_pattern`），不让 regex crate 多收：
//!
//! - `\d \w \s \b` 在 Go 里只认 ASCII（`\s` 是 `[\t\n\f\r ]`，没有 `\v`）；regex crate 默认按 Unicode。改写成显式的 ASCII 集合。
//! - Go 的 `\Q…\E`、八进制 `\123`、转义的标点（`\<` 在 Go 里就是 `<`，在 regex crate 里是词首）、`{` 不成重复时当字面量。
//! - Go 不收的：`\C`、`\e`、`\u`、`\Z`、`(?x)`、`(?u)`、重复套重复（`a**`）、超过 1000 的重复次数（含嵌套相乘）、
//!   不认识的 Unicode 类名（表来自 Go 1.26 的 unicode 包，名字的大小写、下划线按 Go 的 canonicalName 放宽）。
//! - 命名分组改成普通分组（Go 收重名，regex crate 不收；grep 只看匹不匹配，用不到名字）。
//!
//! 匹配在按 Go 的规矩逐字节解码过的行上做（不合法的字节是 U+FFFD，见 gostr::decode_lossy），与 Go 在不合法的 UTF-8 上的行为一致。

use regex::bytes::{Regex, RegexBuilder};

#[derive(Debug, PartialEq, Eq)]
pub struct BadPattern;

/// 编译一个 Go 语法的正则。
pub fn compile(pattern: &str) -> Result<Regex, BadPattern> {
    let translated = translate(pattern)?;
    RegexBuilder::new(&translated)
        .unicode(true)
        .nest_limit(1000)
        .size_limit(64 << 20)
        .build()
        .map_err(|_| BadPattern)
}

// Go 的 \d \w \s 与 [[:…:]]（ASCII）。
const DIGIT: &str = "0-9";
const WORD: &str = "0-9A-Za-z_";
const SPACE: &str = r"\x{9}\x{A}\x{C}\x{D}\x{20}";
const POSIX: &[&str] = &[
    "alnum", "alpha", "ascii", "blank", "cntrl", "digit", "graph", "lower", "print", "punct",
    "space", "upper", "word", "xdigit",
];

/// Go 1.26 的 regexp 收的 Unicode 类名：canonicalName 之后的名字 → regex crate 认的名字。
const UNICODE_NAMES: &[(&str, &str)] = &[
    ("Adlam", "Adlam"),
    ("Ahom", "Ahom"),
    ("Any", "Any"),
    ("Arabic", "Arabic"),
    ("Armenian", "Armenian"),
    ("Ascii", "ASCII"),
    ("Assigned", "Assigned"),
    ("Avestan", "Avestan"),
    ("Balinese", "Balinese"),
    ("Bamum", "Bamum"),
    ("Batak", "Batak"),
    ("Bengali", "Bengali"),
    ("Bhaiksuki", "Bhaiksuki"),
    ("Bopomofo", "Bopomofo"),
    ("Brahmi", "Brahmi"),
    ("Braille", "Braille"),
    ("Buginese", "Buginese"),
    ("Buhid", "Buhid"),
    ("C", "C"),
    ("Carian", "Carian"),
    ("Casedletter", "Cased_Letter"),
    ("Cc", "Cc"),
    ("Cf", "Cf"),
    ("Chakma", "Chakma"),
    ("Cham", "Cham"),
    ("Cherokee", "Cherokee"),
    ("Chorasmian", "Chorasmian"),
    ("Closepunctuation", "Close_Punctuation"),
    ("Cn", "Cn"),
    ("Cntrl", "cntrl"),
    ("Co", "Co"),
    ("Combiningmark", "Combining_Mark"),
    ("Common", "Common"),
    ("Connectorpunctuation", "Connector_Punctuation"),
    ("Control", "Control"),
    ("Coptic", "Coptic"),
    ("Cs", "Cs"),
    ("Cuneiform", "Cuneiform"),
    ("Currencysymbol", "Currency_Symbol"),
    ("Cypriot", "Cypriot"),
    ("Cyrillic", "Cyrillic"),
    ("Dashpunctuation", "Dash_Punctuation"),
    ("Decimalnumber", "Decimal_Number"),
    ("Deseret", "Deseret"),
    ("Devanagari", "Devanagari"),
    ("Digit", "digit"),
    ("Dogra", "Dogra"),
    ("Duployan", "Duployan"),
    ("Elbasan", "Elbasan"),
    ("Elymaic", "Elymaic"),
    ("Enclosingmark", "Enclosing_Mark"),
    ("Ethiopic", "Ethiopic"),
    ("Finalpunctuation", "Final_Punctuation"),
    ("Format", "Format"),
    ("Georgian", "Georgian"),
    ("Glagolitic", "Glagolitic"),
    ("Gothic", "Gothic"),
    ("Grantha", "Grantha"),
    ("Greek", "Greek"),
    ("Gujarati", "Gujarati"),
    ("Gurmukhi", "Gurmukhi"),
    ("Han", "Han"),
    ("Hangul", "Hangul"),
    ("Hanunoo", "Hanunoo"),
    ("Hatran", "Hatran"),
    ("Hebrew", "Hebrew"),
    ("Hiragana", "Hiragana"),
    ("Inherited", "Inherited"),
    ("Initialpunctuation", "Initial_Punctuation"),
    ("Javanese", "Javanese"),
    ("Kaithi", "Kaithi"),
    ("Kannada", "Kannada"),
    ("Katakana", "Katakana"),
    ("Kawi", "Kawi"),
    ("Kharoshthi", "Kharoshthi"),
    ("Khmer", "Khmer"),
    ("Khojki", "Khojki"),
    ("Khudawadi", "Khudawadi"),
    ("L", "L"),
    ("Lao", "Lao"),
    ("Latin", "Latin"),
    ("Lc", "LC"),
    ("Lepcha", "Lepcha"),
    ("Letter", "Letter"),
    ("Letternumber", "Letter_Number"),
    ("Limbu", "Limbu"),
    ("Lineseparator", "Line_Separator"),
    ("Lisu", "Lisu"),
    ("Ll", "Ll"),
    ("Lm", "Lm"),
    ("Lo", "Lo"),
    ("Lowercaseletter", "Lowercase_Letter"),
    ("Lt", "Lt"),
    ("Lu", "Lu"),
    ("Lycian", "Lycian"),
    ("Lydian", "Lydian"),
    ("M", "M"),
    ("Mahajani", "Mahajani"),
    ("Makasar", "Makasar"),
    ("Malayalam", "Malayalam"),
    ("Mandaic", "Mandaic"),
    ("Manichaean", "Manichaean"),
    ("Marchen", "Marchen"),
    ("Mark", "Mark"),
    ("Mathsymbol", "Math_Symbol"),
    ("Mc", "Mc"),
    ("Me", "Me"),
    ("Medefaidrin", "Medefaidrin"),
    ("Miao", "Miao"),
    ("Mn", "Mn"),
    ("Modi", "Modi"),
    ("Modifierletter", "Modifier_Letter"),
    ("Modifiersymbol", "Modifier_Symbol"),
    ("Mongolian", "Mongolian"),
    ("Mro", "Mro"),
    ("Multani", "Multani"),
    ("Myanmar", "Myanmar"),
    ("N", "N"),
    ("Nabataean", "Nabataean"),
    ("Nandinagari", "Nandinagari"),
    ("Nd", "Nd"),
    ("Newa", "Newa"),
    ("Nko", "Nko"),
    ("Nl", "Nl"),
    ("No", "No"),
    ("Nonspacingmark", "Nonspacing_Mark"),
    ("Number", "Number"),
    ("Nushu", "Nushu"),
    ("Ogham", "Ogham"),
    ("Openpunctuation", "Open_Punctuation"),
    ("Oriya", "Oriya"),
    ("Osage", "Osage"),
    ("Osmanya", "Osmanya"),
    ("Other", "Other"),
    ("Otherletter", "Other_Letter"),
    ("Othernumber", "Other_Number"),
    ("Otherpunctuation", "Other_Punctuation"),
    ("Othersymbol", "Other_Symbol"),
    ("P", "P"),
    ("Palmyrene", "Palmyrene"),
    ("Paragraphseparator", "Paragraph_Separator"),
    ("Pc", "Pc"),
    ("Pd", "Pd"),
    ("Pe", "Pe"),
    ("Pf", "Pf"),
    ("Phoenician", "Phoenician"),
    ("Pi", "Pi"),
    ("Po", "Po"),
    ("Privateuse", "Private_Use"),
    ("Ps", "Ps"),
    ("Punct", "punct"),
    ("Punctuation", "Punctuation"),
    ("Rejang", "Rejang"),
    ("Runic", "Runic"),
    ("S", "S"),
    ("Samaritan", "Samaritan"),
    ("Saurashtra", "Saurashtra"),
    ("Sc", "Sc"),
    ("Separator", "Separator"),
    ("Sharada", "Sharada"),
    ("Shavian", "Shavian"),
    ("Siddham", "Siddham"),
    ("Sinhala", "Sinhala"),
    ("Sk", "Sk"),
    ("Sm", "Sm"),
    ("So", "So"),
    ("Sogdian", "Sogdian"),
    ("Soyombo", "Soyombo"),
    ("Spaceseparator", "Space_Separator"),
    ("Spacingmark", "Spacing_Mark"),
    ("Sundanese", "Sundanese"),
    ("Surrogate", "Surrogate"),
    ("Symbol", "Symbol"),
    ("Syriac", "Syriac"),
    ("Tagalog", "Tagalog"),
    ("Tagbanwa", "Tagbanwa"),
    ("Takri", "Takri"),
    ("Tamil", "Tamil"),
    ("Tangsa", "Tangsa"),
    ("Tangut", "Tangut"),
    ("Telugu", "Telugu"),
    ("Thaana", "Thaana"),
    ("Thai", "Thai"),
    ("Tibetan", "Tibetan"),
    ("Tifinagh", "Tifinagh"),
    ("Tirhuta", "Tirhuta"),
    ("Titlecaseletter", "Titlecase_Letter"),
    ("Toto", "Toto"),
    ("Ugaritic", "Ugaritic"),
    ("Unassigned", "Unassigned"),
    ("Uppercaseletter", "Uppercase_Letter"),
    ("Vai", "Vai"),
    ("Vithkuqi", "Vithkuqi"),
    ("Wancho", "Wancho"),
    ("Yezidi", "Yezidi"),
    ("Yi", "Yi"),
    ("Z", "Z"),
    ("Zl", "Zl"),
    ("Zp", "Zp"),
    ("Zs", "Zs"),
];

/// Go 的 canonicalName：首字母大写、其余小写，去掉 `_`、`-` 与空格。
fn canonical(name: &str) -> String {
    let mut out = String::new();
    let mut first = true;
    for c in name.chars() {
        match c {
            '_' | '-' | ' ' => {}
            c if first => {
                out.push(c.to_ascii_uppercase());
                first = false;
            }
            c => out.push(c.to_ascii_lowercase()),
        }
    }
    out
}

fn unicode_class(name: &str) -> Option<&'static str> {
    let c = canonical(name);
    UNICODE_NAMES.iter().find(|(k, _)| *k == c).map(|(_, v)| *v)
}

/// 匹配不到任何字符的类（Go 收代理区的码点，解码过的文本里永远没有它们）。
const NOTHING: &str = r"[^\x{0}-\x{10FFFF}]";

/// 把一个码点写成 regex 的字面量。
fn lit_u32(out: &mut String, c: u32) {
    match char::from_u32(c) {
        Some(c) => lit(out, c),
        None => out.push_str(NOTHING),
    }
}

/// 把一个字符写成 regex 的字面量。
fn lit(out: &mut String, c: char) {
    if c.is_alphanumeric() && !c.is_ascii() || c.is_ascii_alphanumeric() {
        out.push(c);
    } else {
        out.push_str(&format!(r"\x{{{:X}}}", c as u32));
    }
}

/// 计数重复的嵌套，用来按 Go 的 repeatIsValid 判断（只有 `{n,m}` 算，嵌套的次数相乘不超过 1000）。
#[derive(Clone, Default)]
struct RNode {
    m: u32,
    kids: Vec<RNode>,
}

fn repeat_is_valid(re: &RNode, mut n: u32) -> bool {
    if re.m > 0 {
        if re.m > n {
            return false;
        }
        n /= re.m;
    }
    re.kids.iter().all(|k| repeat_is_valid(k, n))
}

struct Frame {
    items: Vec<RNode>,
    /// 这一层最后一个可重复的项（字面量、类、分组）在输出里从哪里开始；没有时重复报错。
    atom_at: Option<usize>,
    /// 那一项已经带了重复（Go 收 `a*(?i)*`：中间隔着标志时不算“重复套重复”；改写时要给它加一层括号）。
    atom_repeated: bool,
    /// 这一层的 `(` 在输出里的位置（分组结束后它就是上一层的最后一项）。
    open_at: usize,
}

impl Frame {
    fn new(open_at: usize) -> Frame {
        Frame {
            items: Vec::new(),
            atom_at: None,
            atom_repeated: false,
            open_at,
        }
    }
}

struct Parser<'a> {
    s: &'a [char],
    i: usize,
    out: String,
}

impl Parser<'_> {
    fn peek(&self, k: usize) -> Option<char> {
        self.s.get(self.i + k).copied()
    }

    fn starts_with(&self, p: &str) -> bool {
        p.chars().enumerate().all(|(k, c)| self.peek(k) == Some(c))
    }

    /// Go 的 parseEscape（`\` 开头，返回一个码点；可能是代理区的，Go 收、永远匹配不到）。
    fn escape(&mut self) -> Result<u32, BadPattern> {
        self.i += 1; // \
        let c = self.peek(0).ok_or(BadPattern)?;
        self.i += 1;
        match c {
            '1'..='7' if !matches!(self.peek(0), Some('0'..='7')) => Err(BadPattern),
            '0'..='7' => {
                let mut r = c as u32 - '0' as u32;
                for _ in 1..3 {
                    match self.peek(0) {
                        Some(d @ '0'..='7') => {
                            r = r * 8 + (d as u32 - '0' as u32);
                            self.i += 1;
                        }
                        _ => break,
                    }
                }
                Ok(r)
            }
            'x' => {
                let c = self.peek(0).ok_or(BadPattern)?;
                self.i += 1;
                if c == '{' {
                    let mut r: u32 = 0;
                    let mut n = 0;
                    loop {
                        let c = self.peek(0).ok_or(BadPattern)?;
                        self.i += 1;
                        if c == '}' {
                            break;
                        }
                        r = r * 16 + c.to_digit(16).ok_or(BadPattern)?;
                        if r > 0x10FFFF {
                            return Err(BadPattern);
                        }
                        n += 1;
                    }
                    if n == 0 {
                        return Err(BadPattern);
                    }
                    return Ok(r);
                }
                let y = self.peek(0).ok_or(BadPattern)?;
                self.i += 1;
                Ok(c.to_digit(16).ok_or(BadPattern)? * 16 + y.to_digit(16).ok_or(BadPattern)?)
            }
            'a' => Ok(7),
            'f' => Ok(0xc),
            'n' => Ok(0xa),
            'r' => Ok(0xd),
            't' => Ok(9),
            'v' => Ok(0xb),
            c if c.is_ascii() && !c.is_ascii_alphanumeric() => Ok(c as u32),
            _ => Err(BadPattern),
        }
    }

    /// `\p…`、`\P…`：返回 regex 的写法。
    fn unicode_class(&mut self) -> Result<String, BadPattern> {
        let mut neg = self.peek(1) == Some('P');
        self.i += 2;
        let name: String = match self.peek(0) {
            None => return Err(BadPattern),
            Some('{') => {
                let rest = &self.s[self.i..];
                let end = rest.iter().position(|&c| c == '}').ok_or(BadPattern)?;
                let name: String = rest[1..end].iter().collect();
                self.i += end + 1;
                name
            }
            Some(c) => {
                self.i += 1;
                c.to_string()
            }
        };
        let name = match name.strip_prefix('^') {
            Some(n) => {
                neg = !neg;
                n.to_string()
            }
            None => name,
        };
        let rust = unicode_class(&name).ok_or(BadPattern)?;
        if rust == "Cs" || rust == "Surrogate" {
            // 代理区：解码过的文本里没有（Go 把那些字节当作不合法的 UTF-8），regex crate 也不收这个类。
            return Ok(if neg {
                r"[\x{0}-\x{10FFFF}]".into()
            } else {
                NOTHING.into()
            });
        }
        Ok(format!(r"\{}{{{}}}", if neg { 'P' } else { 'p' }, rust))
    }

    /// `\d` 之类：返回 (取反, 集合)。
    fn perl_class(&self) -> Option<(bool, &'static str)> {
        if self.peek(0) != Some('\\') {
            return None;
        }
        match self.peek(1)? {
            'd' => Some((false, DIGIT)),
            'D' => Some((true, DIGIT)),
            'w' => Some((false, WORD)),
            'W' => Some((true, WORD)),
            's' => Some((false, SPACE)),
            'S' => Some((true, SPACE)),
            _ => None,
        }
    }

    /// Go 的 parseClass：`[` 开头，写出等价的 regex 字符类。
    fn class(&mut self) -> Result<(), BadPattern> {
        self.i += 1;
        let mut out = String::from("[");
        if self.peek(0) == Some('^') {
            out.push('^');
            self.i += 1;
        }
        let mut first = true;
        loop {
            match self.peek(0) {
                None => return Err(BadPattern),
                Some(']') if !first => break,
                _ => {}
            }
            first = false;
            if self.starts_with("[:") {
                // 名字到下一个 ":]" 为止（Go 在整个剩下的串里找）。
                let rest = &self.s[self.i + 2..];
                if let Some(pos) = rest.windows(2).position(|w| w == [':', ']']) {
                    let name: String = rest[..pos].iter().collect();
                    let (neg, base) = match name.strip_prefix('^') {
                        Some(b) => (true, b),
                        None => (false, name.as_str()),
                    };
                    if !POSIX.contains(&base) {
                        return Err(BadPattern);
                    }
                    out.push_str(&format!("[:{}{}:]", if neg { "^" } else { "" }, base));
                    self.i += 2 + pos + 2;
                    continue;
                }
            }
            if self.peek(0) == Some('\\') && matches!(self.peek(1), Some('p') | Some('P')) {
                out.push_str(&self.unicode_class()?);
                continue;
            }
            if let Some((neg, set)) = self.perl_class() {
                out.push_str(&if neg {
                    format!("[^{set}]")
                } else {
                    set.to_string()
                });
                self.i += 2;
                continue;
            }
            let lo = self.class_char()?;
            let mut hi = lo;
            if self.peek(0) == Some('-') && self.peek(1).is_some_and(|c| c != ']') {
                self.i += 1;
                hi = self.class_char()?;
                if hi < lo {
                    return Err(BadPattern);
                }
            }
            // 端点落在代理区时收窄到两边合法的字符（代理区本身匹配不到）。
            let lo = if (0xD800..0xE000).contains(&lo) {
                0xE000
            } else {
                lo
            };
            let hi = if (0xD800..0xE000).contains(&hi) {
                0xD7FF
            } else {
                hi
            };
            if lo <= hi {
                lit_u32(&mut out, lo);
                if hi != lo {
                    out.push('-');
                    lit_u32(&mut out, hi);
                }
            } else {
                out.push_str(NOTHING);
            }
        }
        self.i += 1; // ]
        out.push(']');
        self.out.push_str(&out);
        Ok(())
    }

    fn class_char(&mut self) -> Result<u32, BadPattern> {
        match self.peek(0) {
            None => Err(BadPattern),
            Some('\\') => self.escape(),
            Some(c) => {
                self.i += 1;
                Ok(c as u32)
            }
        }
    }

    /// Go 的 parseRepeat：`{n}`、`{n,}`、`{n,m}`；不成形时返回 None（`{` 当字面量）。
    fn counted(&self) -> Option<(i64, i64, usize)> {
        let mut k = 1;
        let int = |k: &mut usize| -> Option<i64> {
            let start = *k;
            while matches!(self.peek(*k), Some('0'..='9')) {
                *k += 1;
            }
            if *k == start || (*k - start >= 2 && self.peek(start) == Some('0')) {
                return None;
            }
            let digits: String = self.s[self.i + start..self.i + *k].iter().collect();
            Some(if digits.len() > 8 {
                -1
            } else {
                digits
                    .parse::<i64>()
                    .ok()
                    .filter(|&n| n < 100_000_000)
                    .unwrap_or(-1)
            })
        };
        let min = int(&mut k)?;
        let max;
        match self.peek(k)? {
            ',' => {
                k += 1;
                if self.peek(k)? == '}' {
                    max = -1;
                } else {
                    max = int(&mut k)?;
                }
            }
            _ => max = min,
        }
        if self.peek(k)? != '}' {
            return None;
        }
        Some((min, max, k + 1))
    }
}

/// 把 Go 的正则改写成 regex crate 的写法；Go 不收的报错。
///
/// 只改写到“匹不匹配”相同为止：grep 只用 is_match，贪婪与否、捕获组的编号都不影响结果。所以 `(?flags)` 可以挪到它后面
/// 紧跟着的重复符号之后（Go 里 `a(?i)*` 的 `*` 作用在 `a` 上，regex crate 不收这种写法）。
pub fn translate(pattern: &str) -> Result<String, BadPattern> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut p = Parser {
        s: &chars,
        i: 0,
        out: String::new(),
    };
    let mut stack: Vec<Frame> = vec![Frame::new(0)];
    let mut last_repeat = false;
    // 还没写出去的标志（见上）。
    let mut pending = String::new();
    while let Some(c) = p.peek(0) {
        let is_repeat = match c {
            '*' | '+' | '?' => true,
            '{' => p.counted().is_some(),
            _ => false,
        };
        // 只有标志的分组接着攒，不先写出前面攒的。
        let flags_only = c == '(' && p.peek(1) == Some('?') && {
            let mut k = 2;
            while matches!(p.peek(k), Some('i' | 'm' | 's' | 'U' | '-')) {
                k += 1;
            }
            p.peek(k) == Some(')')
        };
        if !is_repeat && !flags_only && !pending.is_empty() {
            p.out.push_str(&pending);
            pending.clear();
        }
        let mut repeat = false;
        // 这一步之后的“最后一项”：Some(位置) 表示新的一项从输出的这个位置开始。
        let mut atom: Option<usize> = None;
        let start = p.out.len();
        match c {
            '(' if p.peek(1) == Some('?') => {
                if p.starts_with("(?P<") || p.starts_with("(?<") {
                    let skip = if p.starts_with("(?P<") { 4 } else { 3 };
                    let rest = &chars[p.i..];
                    let end = rest.iter().position(|&c| c == '>').ok_or(BadPattern)?;
                    let name = &rest[skip..end];
                    if name.is_empty()
                        || !name.iter().all(|&c| c == '_' || c.is_ascii_alphanumeric())
                    {
                        return Err(BadPattern);
                    }
                    p.i += end + 1;
                    p.out.push('(');
                    stack.push(Frame::new(start));
                } else {
                    // 标志：i m s U，最多一个 -，以 : 或 ) 结尾；后出现的为准（Go 收 (?ii)、(?i-i)，regex crate 不收重复的标志）。
                    p.i += 2;
                    let (mut on, mut off) = (String::new(), String::new());
                    let mut neg = false;
                    let mut saw = false;
                    let end = loop {
                        let c = p.peek(0).ok_or(BadPattern)?;
                        p.i += 1;
                        match c {
                            'i' | 'm' | 's' | 'U' => {
                                saw = true;
                                let (add, del) = if neg {
                                    (&mut off, &mut on)
                                } else {
                                    (&mut on, &mut off)
                                };
                                del.retain(|x| x != c);
                                if !add.contains(c) {
                                    add.push(c);
                                }
                            }
                            '-' if !neg => {
                                neg = true;
                                saw = false;
                            }
                            ':' | ')' => {
                                if neg && !saw {
                                    return Err(BadPattern);
                                }
                                break c;
                            }
                            _ => return Err(BadPattern),
                        }
                    };
                    let flags = if off.is_empty() {
                        on
                    } else {
                        format!("{on}-{off}")
                    };
                    if end == ':' {
                        p.out.push_str(&format!("(?{flags}:"));
                        stack.push(Frame::new(start));
                    } else {
                        if !flags.is_empty() {
                            pending.push_str(&format!("(?{flags})"));
                        }
                        // 只有标志的分组不算一项，也不打断“上一项”（Go 不往栈里放东西），但重置“刚重复过”。
                        last_repeat = false;
                        continue;
                    }
                }
            }
            '(' => {
                p.i += 1;
                p.out.push('(');
                stack.push(Frame::new(start));
            }
            '|' => {
                p.i += 1;
                p.out.push('|');
                let top = stack.last_mut().expect("至少一层");
                top.atom_at = None;
            }
            ')' => {
                p.i += 1;
                if stack.len() < 2 {
                    return Err(BadPattern);
                }
                let f = stack.pop().expect("上面查过");
                p.out.push(')');
                stack.last_mut().expect("至少一层").items.push(RNode {
                    m: 0,
                    kids: f.items,
                });
                atom = Some(f.open_at);
            }
            '^' | '$' | '.' => {
                p.i += 1;
                p.out.push(c);
                atom = Some(start);
            }
            '[' => {
                p.class()?;
                atom = Some(start);
            }
            '*' | '+' | '?' | '{' => {
                let top = stack.last_mut().expect("至少一层");
                let counted = if c == '{' { p.counted() } else { None };
                if c == '{' && counted.is_none() {
                    // `{` 不成重复：字面量。
                    p.i += 1;
                    lit(&mut p.out, '{');
                    top.items.push(RNode::default());
                    atom = Some(start);
                } else {
                    if let Some((min, max, _)) = counted
                        && (!(0..=1000).contains(&min) || max > 1000 || (max >= 0 && min > max))
                    {
                        return Err(BadPattern);
                    }
                    let Some(at) = top.atom_at else {
                        return Err(BadPattern);
                    };
                    if last_repeat {
                        return Err(BadPattern);
                    }
                    if top.atom_repeated {
                        p.out.insert_str(at, "(?:");
                        p.out.push(')');
                    }
                    match counted {
                        None => {
                            p.i += 1;
                            p.out.push(c);
                        }
                        Some((min, max, len)) => {
                            p.i += len;
                            if max < 0 {
                                p.out.push_str(&format!("{{{min},}}"));
                            } else if max == min {
                                p.out.push_str(&format!("{{{min}}}"));
                            } else {
                                p.out.push_str(&format!("{{{min},{max}}}"));
                            }
                            let inner = top.items.pop().unwrap_or_default();
                            let m = if max == 0 {
                                0
                            } else if max < 0 {
                                min as u32
                            } else {
                                max as u32
                            };
                            let node = RNode {
                                m,
                                kids: vec![inner],
                            };
                            if (min >= 2 || max >= 2) && !repeat_is_valid(&node, 1000) {
                                return Err(BadPattern);
                            }
                            top.items.push(node);
                        }
                    }
                    if p.peek(0) == Some('?') {
                        p.i += 1;
                        p.out.push('?');
                    }
                    top.atom_repeated = true;
                    repeat = true;
                    if !pending.is_empty() {
                        p.out.push_str(&pending);
                        pending.clear();
                    }
                }
            }
            '\\' => {
                match p.peek(1) {
                    Some('A') => {
                        p.i += 2;
                        p.out.push_str(r"\A");
                    }
                    Some('z') => {
                        p.i += 2;
                        p.out.push_str(r"\z");
                    }
                    Some('b') => {
                        p.i += 2;
                        p.out.push_str(r"(?-u:\b)");
                    }
                    Some('B') => {
                        p.i += 2;
                        p.out.push_str(r"(?-u:\B)");
                    }
                    Some('C') => return Err(BadPattern),
                    Some('Q') => {
                        // \Q…\E 里的每个字符各是一项（Go 一个个地 p.literal）。
                        p.i += 2;
                        let mut last = None;
                        while p.peek(0).is_some() && !p.starts_with(r"\E") {
                            let c = p.peek(0).expect("上面查过");
                            last = Some(p.out.len());
                            lit(&mut p.out, c);
                            stack
                                .last_mut()
                                .expect("至少一层")
                                .items
                                .push(RNode::default());
                            p.i += 1;
                        }
                        if p.starts_with(r"\E") {
                            p.i += 2;
                        }
                        if let Some(at) = last {
                            let top = stack.last_mut().expect("至少一层");
                            top.atom_at = Some(at);
                            top.atom_repeated = false;
                        }
                        last_repeat = false;
                        continue;
                    }
                    Some('p') | Some('P') => {
                        let s = p.unicode_class()?;
                        p.out.push_str(&s);
                    }
                    _ => {
                        if let Some((neg, set)) = p.perl_class() {
                            p.out
                                .push_str(&format!("[{}{set}]", if neg { "^" } else { "" }));
                            p.i += 2;
                        } else {
                            let c = p.escape()?;
                            lit_u32(&mut p.out, c);
                        }
                    }
                }
                atom = Some(start);
            }
            c => {
                p.i += 1;
                lit(&mut p.out, c);
                atom = Some(start);
            }
        }
        if let Some(at) = atom {
            let top = stack.last_mut().expect("至少一层");
            if c != ')' {
                top.items.push(RNode::default());
            }
            top.atom_at = Some(at);
            top.atom_repeated = false;
        }
        last_repeat = repeat;
    }
    if stack.len() != 1 {
        return Err(BadPattern);
    }
    p.out.push_str(&pending);
    Ok(p.out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(p: &str, s: &str) -> bool {
        compile(p).unwrap().is_match(s.as_bytes())
    }

    #[test]
    fn every_go_unicode_name_compiles() {
        let bad: Vec<&str> = UNICODE_NAMES
            .iter()
            .map(|(_, n)| *n)
            .filter(|n| compile(&format!(r"\p{{{n}}}")).is_err())
            .collect();
        assert!(bad.is_empty(), "{bad:?}");
        assert!(!m(r"\p{Cs}", "\u{FFFD}") && m(r"\P{Cs}", "x") && m(r"[\p{Cs}x]", "x"));
        assert!(compile(r"\p{greek}").is_ok(), "Go 放宽大小写");
        assert!(
            compile(r"\p{Old_Italic}").is_err(),
            "Go 1.26 不收多词的文字名"
        );
        assert!(compile(r"\p{Emoji}").is_err(), "regex crate 认、Go 不认");
    }

    #[test]
    fn perl_classes_are_ascii_like_go() {
        assert!(m(r"\d", "3") && !m(r"\d", "٣"));
        assert!(m(r"\w", "_") && !m(r"\w", "é"));
        assert!(!m(r"\s", "\u{b}") && m(r"\s", "\u{c}"));
        assert!(m(r"\bbar\b", "foo bar") && !m(r"\bbar\b", "ébarx"));
        assert!(
            m(r"\bbar", "ébar"),
            "é 不是 Go 的单词字符，所以 bar 前有边界"
        );
        assert!(m(r"[\d]x", "1x") && m(r"[^\d]", "a") && !m(r"^[\D]$", "5"));
    }

    #[test]
    fn go_only_syntax() {
        assert!(m(r"\Q.*\E", "a.*b") && !m(r"\Q.*\E", "ab"));
        assert!(m(r"\Q.", "x.") && !m(r"\Q.", "xy"));
        assert!(m(r"\101", "A") && compile(r"\1").is_err());
        assert!(m(r"\<", "<") && m(r"\_", "_"));
        assert!(m(r"a{,3}", "a{,3}"), "Go 里 {{,3}} 是字面量");
        assert!(m(r"a{", "a{") && m(r"}", "}") && m(r"]", "]"));
        assert!(m(r"(?P<1a>x)(?P<1a>y)", "xy"), "Go 收数字开头与重名");
        assert!(m(r"(?ii)A", "a") && m(r"(?i-i)a", "a") && !m(r"(?i-i)a", "A"));
        assert!(m(r"(?)a", "a"));
        assert!(
            m(r"b(?U)(?i){1,3}", "bb") && m(r"ж(?s){1,3}", "жж") && m(r"a*(?i)*", "x"),
            "标志之后的重复作用在前一项上"
        );
        assert!(
            compile("(?i)*").is_err() && compile("a**").is_err() && compile("(?i)a*(?i)*").is_ok()
        );
    }

    #[test]
    fn go_rejects() {
        for p in [
            r"\C",
            r"\e",
            r"\u0041",
            r"\Z",
            "(?x)a",
            "(?u)a",
            "(?-u:.)",
            "a**",
            "a*+",
            "a{2}*",
            "a{1001}",
            "(a{100}){100}",
            r"\8",
            r"\pX",
            "(?i",
            "*",
            "a{2,1}",
            "(",
            ")",
            "[z-a]",
            "[a",
            r"(?=x)",
            "(?P=n)",
            "(?-)a",
            "(?i-)a",
            r"[[:foo:]]",
            r"\p{Foo}",
            r"[\b]",
            r"x\",
            "(?P<>x)",
        ] {
            assert_eq!(compile(p).map(|_| ()), Err(BadPattern), "{p}");
        }
        assert!(compile("a{1000}").is_ok() && compile("(a{10}){100}").is_ok());
    }
}
