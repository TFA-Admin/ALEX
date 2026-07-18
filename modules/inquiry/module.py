import ipaddress
import socket
from urllib.parse import urlparse, parse_qs, unquote

import httpx
from bs4 import BeautifulSoup

from llm.ollama_client import ollama_manager

DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 3
MAX_CONTENT_BYTES = 500_000
MAX_TEXT_CHARS = 8_000  # per-page cap fed into synthesis
FETCH_TIMEOUT = 8.0


def init():
    return "inquiry module ready — gated web search with SSRF/content safety hardening"


def _is_safe_url(url: str) -> bool:
    """SSRF protection: http(s) only, and every DNS-resolved address for
    the hostname must be public — not private/loopback/link-local/
    multicast/reserved/unspecified. Known limitation, stated honestly:
    this doesn't fully close a DNS-rebinding race (the real httpx
    connection does its own DNS resolution afterward, which could in
    theory return a different address than what was checked here) — an
    accepted v1 tradeoff for a personal, low-volume project, not a claim
    of airtight protection against an actively adversarial target."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except Exception:
        return False

    for _, _, _, _, sockaddr in addrinfo:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False

        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False

    return True


def _extract_real_url(href: str) -> str:
    """DuckDuckGo occasionally wraps a result link in its own redirector
    (//duckduckgo.com/l/?uddg=<encoded>&...) — unwrap it so SSRF
    validation and fetching happen against the real target, not DDG's
    own redirect page."""
    if href.startswith("//"):
        href = "https:" + href

    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path == "/l/":
        qs = parse_qs(parsed.query)
        real = qs.get("uddg", [None])[0]
        if real:
            return unquote(real)

    return href


async def _search(query: str, max_results: int = MAX_RESULTS):
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            r = await client.post(
                DDG_SEARCH_URL, data={"q": query},
                headers={"User-Agent": "Mozilla/5.0"}
            )
    except Exception:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    results = []

    for link in soup.select("a.result__a")[:max_results]:
        url = _extract_real_url(link.get("href", ""))
        title = link.get_text(strip=True)
        if url and _is_safe_url(url):
            results.append({"title": title, "url": url})

    return results


async def _fetch_text(url: str):
    """Text only, never executed or saved — nothing is ever written to
    disk here, and content-type is checked before a single byte of body
    is trusted, so a non-text response is rejected before it's even
    read."""
    if not _is_safe_url(url):
        return None

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                content_type = response.headers.get("content-type", "")

                if not (content_type.startswith("text/html") or content_type.startswith("text/plain")):
                    return None

                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_CONTENT_BYTES:
                        break
                    chunks.append(chunk)

                raw = b"".join(chunks)
    except Exception:
        return None

    if content_type.startswith("text/html"):
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
    else:
        text = raw.decode("utf-8", errors="ignore")

    return text[:MAX_TEXT_CHARS]


async def _synthesize(query: str, pages: list) -> str:
    context = "\n\n".join(
        f"Source: {p['url']}\n{p['text']}" for p in pages if p.get("text")
    )

    if not context:
        return "I searched, but couldn't fetch readable content from any of the results."

    prompt = f"""Answer this question using ONLY the real content below — do not add
anything from your own general knowledge. If the content doesn't actually
answer the question, say so plainly instead of guessing.

Question: {query}

{context}

Answer:"""

    result = await ollama_manager.generate_text(prompt, temperature=0)
    return result or "I found sources but couldn't summarize them right now."


async def run_search(query: str):
    """The actual gated action — only ever called once a query_report has
    been search_approved (see systems/controller/_inquiry.py). Returns
    (findings_text, sources_text); writes nothing itself — attaching
    findings to the report, and later writing to learned_knowledge on
    retain approval, are both the caller's job, kept out of this module
    so it only ever needs 'network' scope, nothing broader."""
    results = await _search(query)

    if not results:
        return "No search results found.", ""

    pages = []
    for r in results:
        text = await _fetch_text(r["url"])
        pages.append({"url": r["url"], "title": r["title"], "text": text or ""})

    findings = await _synthesize(query, pages)
    sources = "\n".join(p["url"] for p in pages if p.get("text"))

    return findings, sources


async def handle(command, state, user_id=None):
    """Not the normal invocation path — a real search only ever runs
    through the query_reports approval flow, never ad-hoc conversation.
    This exists so the module still satisfies the standard contract
    (execution-tested at install time, self-describable via help())."""
    if state is None:
        state = {}
    return (
        "Search runs through the approval flow, not directly — ask to "
        "look something up and I'll request approval first.",
        state
    )


def help():
    return ("Gated web search: real content, fetched and synthesized, "
            "behind a two-stage approval (search, then retain).")
