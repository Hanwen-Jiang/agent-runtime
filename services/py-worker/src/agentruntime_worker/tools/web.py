"""Tier-3 external web tools: web_fetch (multi-hop safe redirect + readable extraction) and web_search (MAT-TOOL-WEB)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
from typing import Any, Protocol

from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError, TransientError
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer", "aside"})
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "br", "hr"})


class _ReadableHTMLParser(HTMLParser):
    """Pure-stdlib HTML-to-readable-text extractor that strips boilerplate tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in _SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and low in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif self._skip_depth == 0 and low in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        compact: list[str] = []
        blank = False
        for line in lines:
            if not line:
                if compact and not blank:
                    compact.append("")
                blank = True
            else:
                compact.append(line)
                blank = False
        return "\n".join(compact).strip()


def extract_readable(raw_html: str) -> str:
    """Convert HTML into clean readable text; returns stripped text untouched if plain text."""
    if "<" not in raw_html or ">" not in raw_html:
        return raw_html.strip()
    parser = _ReadableHTMLParser()
    with contextlib.suppress(Exception):
        parser.feed(raw_html)
        parser.close()
    extracted = parser.text()
    return extracted if extracted else raw_html.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_ip(ip.ipv4_mapped)
    return ip.is_global


def _parse_url(raw: str) -> tuple[urllib.parse.SplitResult, str]:
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise InvalidError("invalid_url", "只支持 http 与 https 网址")
    return parsed, parsed.hostname.lower()


async def _ensure_public_host(host: str, port: int, *, allow_private: bool) -> None:
    if allow_private:
        return
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
        infos: Sequence[Any] = [(None, None, None, None, (str(ip), port))]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as err:
            raise TransientError("dns_failed", f"解析不了 {host}") from err
    for info in infos:
        addr = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not _is_public_ip(ip):
            raise DeniedError("private_address", f"{host} 解析到了非公网地址")


class WebFetcher:
    """web_fetch handler with SSRF protection, safe multi-hop redirect check, and readable extraction."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def tool(self) -> tuple[ToolSpec, Any]:
        return (
            ToolSpec(
                name="web_fetch",
                version="v1",
                effect=Effect.READ,
                description="抓取登记主机上的公开 http/https 网页，校验重定向并支持正文清洗提取（只读；内网地址拦截；配 AR_CRAWLER_ENDPOINT 时经独立爬虫网关隔离抓取）",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "minLength": 1},
                        "zone": {"type": "string", "enum": ["external", "internal"], "description": "抓取区域：external（公网网页）或 internal（白名单内网文档服务）"},
                        "render_js": {"type": "boolean", "description": "是否启用无头浏览器渲染动态页面（默认 false，需爬虫网关支持）"},
                        "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
                        "extract_readable": {"type": "boolean", "description": "从 HTML 中剔除脚本/样式/导航并提取可读正文（默认 true）"},
                        "follow_redirects": {"type": "boolean", "description": "是否安全跟踪合法公开域名重定向（默认 true，最多 5 跳；目标受限则返回 3xx）"},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permissions=frozenset(),
                timeout=timedelta(seconds=max(30, int(getattr(self.config, "fetch_timeout_s", 20)) + 10)),
            ),
            self.fetch,
        )

    def _allowed(self, host: str) -> bool:
        for pattern in self.config.fetch_hosts:
            item = pattern.lower().lstrip(".")
            if item == "*" or host == item or host.endswith("." + item):
                return True
        return False

    async def _validate_target(self, url: str) -> urllib.parse.SplitResult:
        parsed, host = _parse_url(url)
        if not self._allowed(host):
            raise DeniedError("host_not_allowed", f"主机 {host} 不在 web_fetch 允许列表里")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        await _ensure_public_host(host, port, allow_private=bool(getattr(self.config, "allow_private_fetch", False)))
        return parsed

    async def fetch(self, invocation: ToolInvocation) -> dict[str, Any]:
        url = str(invocation.arguments["url"]).strip()
        zone = str(invocation.arguments.get("zone") or "external").strip().lower()
        if zone not in ("external", "internal"):
            raise InvalidError("invalid_zone", f"不支持的抓取区域：{zone}")
        render_js = bool(invocation.arguments.get("render_js", False))
        limit = min(int(invocation.arguments.get("max_bytes", self.config.fetch_limit)), 1_000_000)
        readable = bool(invocation.arguments.get("extract_readable", True))
        follow = bool(invocation.arguments.get("follow_redirects", True))

        crawler_endpoint = os.environ.get("AR_CRAWLER_ENDPOINT", "").strip().rstrip("/")
        if crawler_endpoint:
            headers: dict[str, str] = {}
            crawler_token = os.environ.get("AR_CRAWLER_TOKEN", "").strip()
            if crawler_token:
                headers["Authorization"] = f"Bearer {crawler_token}"
            try:
                return await asyncio.to_thread(
                    _http_json,
                    f"{crawler_endpoint}/v1/fetch",
                    method="POST",
                    payload={
                        "url": url,
                        "zone": zone,
                        "max_bytes": limit,
                        "extract_readable": readable,
                        "follow_redirects": follow,
                        "render_js": render_js,
                    },
                    headers=headers,
                    timeout=float(max(15, int(getattr(self.config, "fetch_timeout_s", 20)))),
                )
            except DeniedError as err:
                raise DeniedError("crawler_denied", err.message) from None
            except InvalidError as err:
                raise InvalidError("crawler_invalid", err.message) from None
            except TransientError as err:
                raise TransientError("crawler_unavailable", err.message) from None

        if zone == "internal":
            raise DeniedError("crawler_required", "未配置 AR_CRAWLER_ENDPOINT 时禁止直连抓取 internal 内网地址")

        await self._validate_target(url)
        current_url = url
        redirects: list[str] = []
        status = 0
        headers: dict[str, str] = {}
        body = b""
        truncated = False
        redirect_blocked = False
        redirect_block_reason = ""

        for hop in range(MAX_REDIRECTS + 1):
            status, headers, body, truncated = await asyncio.to_thread(self._get, current_url, limit)
            location = headers.get("location")
            if not (300 <= status < 400 and location):
                break
            if not follow or hop >= MAX_REDIRECTS:
                break
            next_url = urllib.parse.urljoin(current_url, location)
            try:
                await self._validate_target(next_url)
            except (DeniedError, InvalidError) as err:
                redirect_blocked = True
                redirect_block_reason = err.message
                break
            redirects.append(next_url)
            current_url = next_url

        text = body.decode("utf-8", errors="replace")
        content_type = headers.get("content-type", "")
        if readable and ("html" in content_type.lower() or text.lstrip().startswith("<")):
            extracted = extract_readable(text)
            if extracted:
                text = extracted

        out: dict[str, Any] = {
            "url": current_url,
            "status": status,
            "content_type": content_type,
            "text": text,
            "truncated": truncated,
        }
        if redirects:
            out["redirects"] = redirects
        if "location" in headers:
            out["location"] = headers["location"]
        if redirect_blocked:
            out["redirect_blocked"] = True
            out["redirect_block_reason"] = redirect_block_reason
        return out

    def _get(self, url: str, limit: int) -> tuple[int, dict[str, str], bytes, bool]:
        req = urllib.request.Request(url, headers={"User-Agent": "agentruntime-worker/1.0"})
        try:
            with self.opener.open(req, timeout=self.config.fetch_timeout_s) as response:
                return _read_response(response, limit)
        except urllib.error.HTTPError as response:
            headers = {k.lower(): v for k, v in response.headers.items()} if response.headers else {}
            return response.code, headers, response.read(limit) if response.fp else b"", False
        except (urllib.error.URLError, TimeoutError) as err:
            raise TransientError("fetch_failed", f"抓取 {url} 失败：{err}") from err


def _read_response(response: Any, limit: int) -> tuple[int, dict[str, str], bytes, bool]:
    headers = {k.lower(): v for k, v in response.headers.items()}
    raw = response.read(limit + 1)
    return response.status, headers, raw[:limit], len(raw) > limit


# --- web_search ---


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(self.url)
        item: dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": (parsed.hostname or "").lower(),
        }
        if self.score is not None:
            item["score"] = self.score
        return item


class WebSearchProvider(Protocol):
    name: str
    fallback_mode: bool

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]: ...


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req_headers = {"User-Agent": "agentruntime-worker/1.0", **(headers or {})}
    if payload is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(req, timeout=timeout) as resp:
            raw = resp.read(2 << 20)
    except urllib.error.HTTPError as err:
        body = err.read(512).decode("utf-8", errors="replace") if err.fp else ""
        if err.code in (401, 403):
            raise DeniedError("web_search_denied", f"搜索服务鉴权失败（HTTP {err.code}）：{body}") from None
        if err.code in (408, 429, 500, 502, 503, 504):
            raise TransientError("web_search_unavailable", f"搜索服务暂不可用（HTTP {err.code}）") from None
        raise InvalidError("web_search_rejected", f"搜索请求被拒绝（HTTP {err.code}）：{body}") from None
    except (urllib.error.URLError, TimeoutError) as err:
        raise TransientError("web_search_unavailable", f"搜索网络请求失败：{err}") from err
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as err:
        raise TransientError("web_search_invalid_response", "搜索响应不是合法 JSON") from err


class TavilyProvider:
    name = "tavily"
    fallback_mode = False

    def __init__(self, api_key: str, *, base_url: str = "https://api.tavily.com/search", timeout: float = 12.0) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        body: dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        }
        if include_domains:
            body["include_domains"] = include_domains
        data = await asyncio.to_thread(_http_json, self.base_url, method="POST", payload=body, timeout=self.timeout)
        results = []
        for row in data.get("results") or []:
            if isinstance(row, dict) and row.get("url"):
                results.append(SearchResult(
                    title=str(row.get("title") or ""),
                    url=str(row.get("url")),
                    snippet=str(row.get("content") or row.get("snippet") or "")[:600],
                    score=float(row["score"]) if isinstance(row.get("score"), (int, float)) else None,
                ))
        return results[:max_results]


class SerperProvider:
    name = "serper"
    fallback_mode = False

    def __init__(self, api_key: str, *, base_url: str = "https://google.serper.dev/search", timeout: float = 12.0) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        scoped_query = _apply_site_filter(query, include_domains)
        data = await asyncio.to_thread(
            _http_json,
            self.base_url,
            method="POST",
            payload={"q": scoped_query, "num": max_results},
            headers={"X-API-KEY": self.api_key},
            timeout=self.timeout,
        )
        results = []
        for row in data.get("organic") or []:
            if isinstance(row, dict) and row.get("link"):
                results.append(SearchResult(
                    title=str(row.get("title") or ""),
                    url=str(row.get("link")),
                    snippet=str(row.get("snippet") or "")[:600],
                ))
        return results[:max_results]


class BraveProvider:
    name = "brave"
    fallback_mode = False

    def __init__(self, api_key: str, *, base_url: str = "https://api.search.brave.com/res/v1/web/search", timeout: float = 12.0) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        scoped_query = _apply_site_filter(query, include_domains)
        url = f"{self.base_url}?{urllib.parse.urlencode({'q': scoped_query, 'count': max_results})}"
        data = await asyncio.to_thread(
            _http_json,
            url,
            method="GET",
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
            timeout=self.timeout,
        )
        rows = (data.get("web") or {}).get("results") or []
        return [
            SearchResult(title=str(r.get("title") or ""), url=str(r["url"]), snippet=str(r.get("description") or "")[:600])
            for r in rows
            if isinstance(r, dict) and r.get("url")
        ][:max_results]


class ExaProvider:
    name = "exa"
    fallback_mode = False

    def __init__(self, api_key: str, *, base_url: str = "https://api.exa.ai/search", timeout: float = 12.0) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        body: dict[str, Any] = {"query": query, "numResults": max_results, "contents": {"highlights": True}}
        if include_domains:
            body["includeDomains"] = include_domains
        data = await asyncio.to_thread(
            _http_json,
            self.base_url,
            method="POST",
            payload=body,
            headers={"x-api-key": self.api_key},
            timeout=self.timeout,
        )
        results = []
        for row in data.get("results") or []:
            if isinstance(row, dict) and row.get("url"):
                highlights = row.get("highlights") or []
                snippet = " ... ".join(str(h) for h in highlights[:2]) if highlights else str(row.get("text") or "")
                results.append(SearchResult(
                    title=str(row.get("title") or ""),
                    url=str(row["url"]),
                    snippet=snippet[:600],
                    score=float(row["score"]) if isinstance(row.get("score"), (int, float)) else None,
                ))
        return results[:max_results]


class SearXNGProvider:
    name = "searxng"
    fallback_mode = True

    def __init__(self, base_url: str, *, timeout: float = 12.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        scoped_query = _apply_site_filter(query, include_domains)
        url = f"{self.base_url}/search?{urllib.parse.urlencode({'q': scoped_query, 'format': 'json'})}"
        data = await asyncio.to_thread(_http_json, url, method="GET", timeout=self.timeout)
        rows = data.get("results") or []
        return [
            SearchResult(title=str(r.get("title") or ""), url=str(r["url"]), snippet=str(r.get("content") or "")[:600])
            for r in rows
            if isinstance(r, dict) and r.get("url")
        ][:max_results]


class SelfHostedCrawlerProvider:
    """Wave-4 adapter skeleton for dedicated external + internal clean crawler cluster."""

    name = "self_hosted_crawler"
    fallback_mode = False

    def __init__(self, endpoint: str, *, token: str = "", timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        data = await asyncio.to_thread(
            _http_json,
            f"{self.endpoint}/v1/search",
            method="POST",
            payload=payload,
            headers=headers,
            timeout=self.timeout,
        )
        rows = data.get("results") or []
        return [
            SearchResult(
                title=str(r.get("title") or ""),
                url=str(r["url"]),
                snippet=str(r.get("snippet") or r.get("content") or "")[:600],
                score=float(r["score"]) if isinstance(r.get("score"), (int, float)) else None,
            )
            for r in rows
            if isinstance(r, dict) and r.get("url")
        ][:max_results]


class _DDGHTMLParser(HTMLParser):
    """Minimal HTML parser for DuckDuckGo Lite/HTML search result pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_link = False
        self._in_snippet = False
        self._href = ""
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._pending_title = ""
        self._pending_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        cls = attr_map.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._in_link = True
            self._href = attr_map.get("href") or ""
            self._title_parts = []
        elif "result__snippet" in cls:
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_link and tag == "a":
            self._in_link = False
            self._pending_title = re.sub(r"\s+", " ", "".join(self._title_parts)).strip()
            self._pending_url = _decode_ddg_href(self._href)
        elif self._in_snippet and tag in ("a", "div", "span", "td"):
            self._in_snippet = False
            snippet = re.sub(r"\s+", " ", "".join(self._snippet_parts)).strip()
            if self._pending_url:
                self.results.append(SearchResult(title=self._pending_title, url=self._pending_url, snippet=snippet[:600]))
                self._pending_title = ""
                self._pending_url = ""

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


def _decode_ddg_href(href: str) -> str:
    raw = html.unescape(href.strip())
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urllib.parse.urlsplit(raw)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return qs["uddg"][0]
    return raw if raw.startswith(("http://", "https://")) else ""


class DuckDuckGoLiteProvider:
    name = "duckduckgo_lite"
    fallback_mode = True

    def __init__(self, *, endpoint: str = "https://html.duckduckgo.com/html/", timeout: float = 12.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        scoped_query = _apply_site_filter(query, include_domains)
        raw_html = await asyncio.to_thread(self._fetch_html, scoped_query)
        parser = _DDGHTMLParser()
        parser.feed(raw_html)
        parser.close()
        return _filter_domains(parser.results, include_domains)[:max_results]

    def _fetch_html(self, query: str) -> str:
        url = f"{self.endpoint}?{urllib.parse.urlencode({'q': query})}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; agentruntime-worker/1.0)",
                "Accept": "text/html",
            },
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(req, timeout=self.timeout) as resp:
                return resp.read(512_000).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as err:
            raise TransientError("web_search_unavailable", f"零密钥搜索回退请求失败：{err}") from err


def _apply_site_filter(query: str, include_domains: list[str] | None) -> str:
    domains = [d.strip().lower() for d in (include_domains or []) if d.strip()]
    if not domains:
        return query
    clause = " OR ".join(f"site:{d}" for d in domains[:5])
    return f"({query}) ({clause})"


def _filter_domains(results: Iterable[SearchResult], include_domains: list[str] | None) -> list[SearchResult]:
    domains = [d.strip().lower().lstrip(".") for d in (include_domains or []) if d.strip()]
    items = list(results)
    if not domains:
        return items
    matched: list[SearchResult] = []
    for item in items:
        host = (urllib.parse.urlsplit(item.url).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in domains):
            matched.append(item)
    return matched


_API_PROVIDERS: dict[str, Callable[[str], WebSearchProvider]] = {
    "tavily": lambda key: TavilyProvider(key),
    "serper": lambda key: SerperProvider(key),
    "brave": lambda key: BraveProvider(key),
    "exa": lambda key: ExaProvider(key),
}


def resolve_search_provider(*, secret_value: bytes | None = None, env: dict[str, str] | None = None) -> WebSearchProvider:
    """Resolve active WebSearchProvider cleanly from crawler, SecretStore/env keys, SearXNG, or zero-key fallback."""
    environ = os.environ if env is None else env
    crawler_url = environ.get("AR_CRAWLER_ENDPOINT", "").strip()
    if crawler_url:
        return SelfHostedCrawlerProvider(crawler_url, token=environ.get("AR_CRAWLER_TOKEN", "").strip())

    explicit = environ.get("AR_WEB_SEARCH_PROVIDER", "").strip().lower()
    secret_str = secret_value.decode("utf-8", errors="replace").strip() if secret_value else ""
    key = secret_str or environ.get("AR_WEB_SEARCH_API_KEY", "").strip()
    if key:
        if explicit in _API_PROVIDERS:
            return _API_PROVIDERS[explicit](key)
        return TavilyProvider(key)

    for env_var, name in (
        ("TAVILY_API_KEY", "tavily"),
        ("SERPER_API_KEY", "serper"),
        ("BRAVE_API_KEY", "brave"),
        ("EXA_API_KEY", "exa"),
    ):
        vendor_key = environ.get(env_var, "").strip()
        if vendor_key and explicit in ("", name):
            return _API_PROVIDERS[name](vendor_key)

    searxng_url = environ.get("AR_SEARXNG_URL", "").strip()
    if searxng_url:
        return SearXNGProvider(searxng_url)

    return DuckDuckGoLiteProvider()


class WebSearchTool:
    """web_search tool backed by pluggable WebSearchProvider."""

    def __init__(
        self,
        config: Any,
        *,
        secret_getter: Callable[[], Awaitable[bytes | None]] | None = None,
        provider_override: WebSearchProvider | None = None,
    ) -> None:
        self.config = config
        self.secret_getter = secret_getter
        self.provider_override = provider_override

    def tool(self) -> tuple[ToolSpec, Any]:
        return (
            ToolSpec(
                name="web_search",
                version="v1",
                effect=Effect.READ,
                description="检索外部网页、官方文档与最新信息（只读；支持域名过滤，自动选用已配置接口或免密钥回退）",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 500},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                        "include_domains": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                        "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                permissions=frozenset(),
                timeout=timedelta(seconds=30),
            ),
            self.search,
        )

    async def search(self, invocation: ToolInvocation) -> dict[str, Any]:
        query = str(invocation.arguments["query"]).strip()
        if not query:
            raise InvalidError("invalid_query", "query 不能为空")
        max_results = max(1, min(int(invocation.arguments.get("max_results", 5)), 10))
        include_domains = [str(d).strip() for d in (invocation.arguments.get("include_domains") or []) if str(d).strip()]
        search_depth = str(invocation.arguments.get("search_depth") or "basic")

        provider = self.provider_override
        if provider is None:
            secret = await self.secret_getter() if self.secret_getter is not None else None
            provider = resolve_search_provider(secret_value=secret)

        results = await provider.search(
            query,
            max_results=max_results,
            include_domains=include_domains or None,
            search_depth=search_depth,
        )
        rows = _filter_domains(results, include_domains or None)[:max_results]
        return {
            "query": query,
            "provider": provider.name,
            "fallback_mode": bool(getattr(provider, "fallback_mode", False)),
            "results": [r.as_dict() for r in rows],
            "total": len(rows),
        }
