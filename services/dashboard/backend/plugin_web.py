"""Plugin: web search and URL fetch tools.

Exposes PLUGIN_TOOLS (list of Anthropic tool schemas) and execute(name, input).
Uses DuckDuckGo HTML search (no API key required) and httpx for fetching.

Dependencies (must be present in requirements.txt):
    beautifulsoup4
    httpx
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic tool schemas
# ---------------------------------------------------------------------------

PLUGIN_TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for a query and return top results with title, url, and snippet. "
            "Uses DuckDuckGo HTML (no API key needed) as a free fallback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch a URL and return its main text content (stripped of HTML, nav, ads). "
            "Useful for reading full articles after web_search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "default": 5000},
            },
            "required": ["url"],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute(name: str, input: dict) -> dict | None:
    """Return result dict, or None if this plugin does not handle *name*."""
    if name == "web_search":
        return _web_search(**input)
    if name == "fetch_url":
        return _fetch_url(**input)
    return None


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

def _web_search(query: str, num_results: int = 5) -> dict:
    import httpx
    from bs4 import BeautifulSoup

    url = "https://html.duckduckgo.com/html/"
    try:
        response = httpx.post(
            url,
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("web_search failed for %r: %s", query, exc)
        return {"error": f"search failed: {exc}"}

    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    for node in soup.select(".result")[:num_results]:
        title_el = node.select_one(".result__title a")
        snippet_el = node.select_one(".result__snippet")
        if title_el is None:
            continue
        results.append(
            {
                "title": title_el.get_text(strip=True),
                "url": title_el.get("href"),
                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
            }
        )

    logger.info("web_search(%r) -> %d results", query, len(results))
    return {"query": query, "count": len(results), "results": results}


def _is_safe_public_url(url: str) -> tuple[bool, str]:
    """SSRF guard: reject non-http(s), loopback, link-local, private-range,
    cloud metadata, and reserved IPs. Resolves the host and checks every
    returned address — not just the first — so split-horizon DNS can't
    bypass the check.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"unparsable url: {exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"disallowed scheme: {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return False, "missing host"

    # Reject obvious dangerous hostnames outright
    _DENY_HOSTS = {
        "localhost", "localhost.localdomain", "ip6-localhost",
        "metadata", "metadata.google.internal", "instance-data",
    }
    if host.lower() in _DENY_HOSTS:
        return False, f"blocked hostname: {host}"

    # Resolve all IPs and check every one. socket.getaddrinfo covers v4+v6.
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"dns resolution failed: {exc}"

    for family, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparsable ip: {ip_str}"
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"blocked ip range: {ip}"

    return True, ""


def _fetch_url(url: str, max_chars: int = 5000) -> dict:
    import httpx
    from bs4 import BeautifulSoup

    ok, reason = _is_safe_public_url(url)
    if not ok:
        logger.warning("fetch_url blocked %r: %s", url, reason)
        return {"error": f"blocked by SSRF guard: {reason}"}

    try:
        # follow_redirects=False so a 302 can't bounce us into a blocked range.
        response = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
            follow_redirects=False,
        )
        # Manually follow up to 3 redirects, re-validating each hop.
        hops = 0
        while response.is_redirect and hops < 3:
            next_url = response.headers.get("location")
            if not next_url:
                break
            ok, reason = _is_safe_public_url(next_url)
            if not ok:
                return {"error": f"redirect blocked: {reason}"}
            response = httpx.get(
                next_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
                follow_redirects=False,
            )
            hops += 1
        response.raise_for_status()
    except Exception as exc:
        logger.warning("fetch_url failed for %r: %s", url, exc)
        return {"error": f"fetch failed: {exc}"}

    soup = BeautifulSoup(response.text, "html.parser")
    # Drop script/style/nav/footer/aside/header
    for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
        tag.decompose()
    # Prefer article/main if present, fall back to body or root
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)
    # Collapse blank lines
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    text = "\n".join(lines)[:max_chars]

    logger.info("fetch_url(%r) -> %d chars (truncated=%s)", url, len(text), len(text) >= max_chars)
    return {"url": url, "text": text, "truncated": len(text) >= max_chars}
