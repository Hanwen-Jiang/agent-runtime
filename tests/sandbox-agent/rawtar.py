"""手写 tar（恶意存档与边界用例）：不经 tarfile 的规范化，名字、类型、PAX 与 GNU 扩展都照原样写。"""

from __future__ import annotations

from dataclasses import dataclass, field

BLOCK = 512


@dataclass
class Item:
    name: str
    typ: bytes = b"0"
    body: bytes = b""
    link: str = ""
    mode: int = 0o644
    prefix: str = ""
    pax: dict[str, str] = field(default_factory=dict)
    gnu_longname: str | None = None
    gnu_longlink: str | None = None
    size: int | None = None  # 头里写的大小；None 时等于 len(body)


def _octal(n: int, width: int) -> bytes:
    return (format(n, "o").rjust(width - 1, "0") + "\0").encode()


def header(name: bytes, typ: bytes, *, size: int = 0, mode: int = 0o644, link: bytes = b"", prefix: bytes = b"", mtime: int = 1700000000) -> bytes:
    h = bytearray(BLOCK)
    h[0:100] = name[:100].ljust(100, b"\0")
    h[100:108] = _octal(mode, 8)
    h[108:116] = _octal(0, 8)
    h[116:124] = _octal(0, 8)
    h[124:136] = _octal(size, 12)
    h[136:148] = _octal(mtime, 12)
    h[148:156] = b" " * 8
    h[156:157] = typ
    h[157:257] = link[:100].ljust(100, b"\0")
    h[257:263] = b"ustar\0"
    h[263:265] = b"00"
    h[345:500] = prefix[:155].ljust(155, b"\0")
    chk = sum(h)
    h[148:156] = (format(chk, "o").rjust(6, "0") + "\0 ").encode()
    return bytes(h)


def _pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % BLOCK)


def _pax_records(pax: dict[str, str]) -> bytes:
    out = b""
    for k, v in pax.items():
        rec = f" {k}={v}\n".encode()
        n = len(rec)
        while True:
            line = str(n).encode() + rec
            if len(line) == n:
                break
            n = len(line)
        out += line
    return out


def build(*items: Item, end: bool = True) -> bytes:
    out = b""
    for it in items:
        if it.pax:
            recs = _pax_records(it.pax)
            out += header(b"PaxHeaders/x", b"x", size=len(recs)) + _pad(recs)
        if it.gnu_longname is not None:
            data = it.gnu_longname.encode() + b"\0"
            out += header(b"././@LongLink", b"L", size=len(data)) + _pad(data)
        if it.gnu_longlink is not None:
            data = it.gnu_longlink.encode() + b"\0"
            out += header(b"././@LongLink", b"K", size=len(data)) + _pad(data)
        size = len(it.body) if it.size is None else it.size
        out += header(it.name.encode(), it.typ, size=size, mode=it.mode, link=it.link.encode(), prefix=it.prefix.encode())
        out += _pad(it.body)
    if end:
        out += b"\0" * (2 * BLOCK)
    return out


def file(name: str, body: bytes | str = b"x", **kw: object) -> Item:
    return Item(name, b"0", body.encode() if isinstance(body, str) else body, **kw)  # type: ignore[arg-type]


def dir(name: str, mode: int = 0o755) -> Item:  # noqa: A001
    return Item(name, b"5", mode=mode)


def symlink(name: str, target: str) -> Item:
    return Item(name, b"2", link=target, mode=0o777)
