# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Dependency-free, bounded web search and page fetching tools."""

from __future__ import annotations

import base64
import gzip
import html
import http.client
import ipaddress
import json
import socket
import zlib
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .base import Tool, ToolError, object_schema
from .context import ToolContext

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TEXT_CHARS = 12_000
MAX_TEXT_CHARS = 30_000
USER_AGENT = "CodeLite/1.0 (+local coding agent)"


def _public_addresses(url: str) -> tuple[str, ...]:
    """Resolve a public URL once, returning the exact addresses safe to connect to.

    The caller must use these addresses directly. Resolving here and later
    connecting by hostname leaves a DNS-rebinding window in between.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("Only public http:// and https:// URLs are supported.")
    if parsed.username or parsed.password:
        raise ToolError("URLs containing credentials are not supported.")
    try:
        resolved = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ToolError(f"Could not resolve `{parsed.hostname}`: {error}") from error

    addresses = tuple(dict.fromkeys(item[4][0] for item in resolved))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ToolError("Private, local, and reserved network addresses are blocked.")
    return addresses


def _validate_public_url(url: str) -> None:
    _public_addresses(url)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that never resolves its hostname after validation."""

    def __init__(self, host: str, *, addresses: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._addresses = addresses

    def connect(self) -> None:
        error: OSError | None = None
        for address in self._addresses:
            try:
                self.sock = socket.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                return
            except OSError as exc:
                error = exc
        raise error or OSError("No validated address was available.")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS variant that pins the TCP peer but preserves hostname validation/SNI."""

    def __init__(self, host: str, *, addresses: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._addresses = addresses

    def connect(self) -> None:
        error: OSError | None = None
        for address in self._addresses:
            try:
                sock = socket.create_connection(
                    (address, self.port), self.timeout, self.source_address
                )
                break
            except OSError as exc:
                error = exc
        else:
            raise error or OSError("No validated address was available.")
        self.sock = sock
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPHandler(HTTPHandler):
    def http_open(self, request: Request):
        addresses = _public_addresses(request.full_url)
        connection = lambda host, **kwargs: _PinnedHTTPConnection(
            host, addresses=addresses, **kwargs
        )
        return self.do_open(connection, request)


class _PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self, request: Request):
        addresses = _public_addresses(request.full_url)
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(host, addresses=addresses, **kwargs),
            request,
            context=self._context,
        )


class _SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str) -> tuple[str, str, bytes]:
    _validate_public_url(url)
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.2",
            "Accept-Encoding": "identity",
        },
    )
    try:
        # Disable environment proxies: they can redirect a validated request to
        # an arbitrary private endpoint. Each handler pins its TCP peer to the
        # public address set obtained immediately before connecting.
        opener = build_opener(
            ProxyHandler({}), _PinnedHTTPHandler(), _PinnedHTTPSHandler(), _SafeRedirects()
        )
        with opener.open(request, timeout=15) as response:
            final_url = response.geturl()
            _validate_public_url(final_url)
            content_type = response.headers.get_content_type()
            content_encoding = (response.headers.get("Content-Encoding") or "").lower()
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except ToolError:
        raise
    except Exception as error:  # noqa: BLE001 - converted to an agent-visible error
        raise ToolError(f"Request failed: {error}") from error
    if len(data) > MAX_RESPONSE_BYTES:
        raise ToolError("The response exceeds the 2 MB download limit.")
    try:
        if content_encoding == "gzip" or data.startswith(b"\x1f\x8b"):
            data = gzip.decompress(data)
        elif content_encoding == "deflate":
            data = zlib.decompress(data)
    except (OSError, zlib.error) as error:
        raise ToolError(f"Could not decompress the response: {error}") from error
    if len(data) > MAX_RESPONSE_BYTES:
        raise ToolError("The decompressed response exceeds the 2 MB limit.")
    return final_url, content_type, data


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._ignored += 1
        elif not self._ignored and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "pre", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self._ignored:
            self._ignored -= 1
        elif not self._ignored and tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "pre", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(part.split()) for part in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


class _SearchResults(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field = ""
        self._href = ""
        self._parts: list[str] = []
        self._bing_result = False
        self._bing_heading = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self._bing_result = True
        elif self._bing_result and tag == "h2":
            self._bing_heading = True
        elif tag == "a" and ("result__a" in classes or self._bing_heading):
            self._field, self._href, self._parts = "title", values.get("href") or "", []
        elif "result__snippet" in classes or (
            self._bing_result and tag == "p" and any(name.startswith("b_lineclamp") for name in classes)
        ):
            self._field, self._parts = "snippet", []

    def handle_endtag(self, tag: str) -> None:
        if self._field == "title" and tag == "a":
            href = _result_url(self._href)
            query = parse_qs(urlparse(href).query)
            if query.get("uddg"):
                href = unquote(query["uddg"][0])
            self.results.append({"title": " ".join("".join(self._parts).split()), "url": href, "snippet": ""})
            self._field = ""
        elif self._field == "snippet" and tag in {"a", "div", "span", "p"}:
            if self.results:
                self.results[-1]["snippet"] = " ".join("".join(self._parts).split())
            self._field = ""
        if tag == "h2":
            self._bing_heading = False
        elif tag == "li":
            self._bing_result = False

    def handle_data(self, data: str) -> None:
        if self._field:
            self._parts.append(data)


def _result_url(href: str) -> str:
    query = parse_qs(urlparse(html.unescape(href)).query)
    if query.get("uddg"):
        return unquote(query["uddg"][0])
    encoded = query.get("u", [""])[0]
    if encoded.startswith("a1"):
        try:
            payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
            decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
            if urlparse(decoded).scheme in {"http", "https"}:
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    return html.unescape(href)


def _web_search(arguments: dict[str, Any], _ctx: ToolContext) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ToolError("`query` must not be empty.")
    count = max(1, min(int(arguments.get("count", 5)), 10))
    _, _, data = _download(
        "https://www.bing.com/search?q=" + quote_plus(query) + f"&count={count}"
    )
    parser = _SearchResults()
    parser.feed(data.decode("utf-8", errors="replace"))
    results = [item for item in parser.results if item["title"] and item["url"]][:count]
    if not results:
        raise ToolError("The search provider returned no readable results.")
    return json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2)


def _web_fetch(arguments: dict[str, Any], _ctx: ToolContext) -> str:
    url = str(arguments.get("url") or "").strip()
    limit = max(1_000, min(int(arguments.get("max_chars", DEFAULT_TEXT_CHARS)), MAX_TEXT_CHARS))
    final_url, content_type, data = _download(url)
    charset = "utf-8"
    text = data.decode(charset, errors="replace")
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _PageText()
        parser.feed(text)
        text = parser.text()
    elif content_type == "application/json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    elif not (content_type.startswith("text/") or content_type == "application/json"):
        raise ToolError(f"Unsupported content type `{content_type}`; fetch text, HTML, or JSON.")
    text = html.unescape(text).strip()
    clipped = text[:limit]
    if len(text) > limit:
        clipped += f"\n\n[Page truncated; {len(text) - limit:,} more characters]"
    return f"URL: {final_url}\nContent-Type: {content_type}\n\n{clipped}"


WEB_SEARCH = Tool(
    name="web_search",
    description="Search the public web. Returns titles, URLs, and short snippets. Use web_fetch to read a result.",
    parameters=object_schema(
        {
            "query": {"type": "string", "description": "Search query."},
            "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Number of results (default 5)."},
        },
        ["query"],
    ),
    run=_web_search,
)

WEB_FETCH = Tool(
    name="web_fetch",
    description="Fetch a public HTTP(S) page and return bounded readable text. Local/private network addresses are blocked.",
    parameters=object_schema(
        {
            "url": {"type": "string", "description": "Public http:// or https:// URL."},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": MAX_TEXT_CHARS, "description": "Maximum returned characters (default 12000)."},
        },
        ["url"],
    ),
    run=_web_fetch,
)
