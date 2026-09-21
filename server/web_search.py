"""Web search for the Storyboard AD, via a search engine running somewhere \
else on the network — linked by URL, not installed, the same footing as the \
local language model in ``llm.py``.

The wire format is SearXNG's JSON search API (``GET /search?q=...&format=json``,
a ``results`` array of objects with ``title``/``url``/``content``). SearXNG is
just the example this was built and tested against — any search engine or
gateway that speaks the same interface at that URL works too, self-hosted or
otherwise.

Optional: the AD only gets this capability when a search URL is set in
Settings (``server-config.json``'s ``searchUrl``, blank by default). A blank
URL means no web search at all, not a broken one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def search(
    base_url: str, query: str, *, limit: int = 5, timeout: float = 8.0
) -> list[dict[str, str]]:
    """Query a SearXNG-compatible JSON search API.

    Returns up to *limit* ``{title, url, snippet}`` results, or an empty list
    on any failure (unreachable host, non-JSON reply, or — for an actual
    SearXNG instance — its ``json`` format not enabled in its own
    settings.yml) — a broken or unconfigured search engine should degrade the
    AD's answer, not fail the chat turn.
    """
    base_url = (base_url or "").strip().rstrip("/")
    query = (query or "").strip()
    if not base_url or not query:
        return []
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    try:
        req = urllib.request.Request(
            f"{base_url}/search?{params}", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc: Any = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(doc, dict):
        return []
    out: list[dict[str, str]] = []
    for item in (doc.get("results") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        link = str(item.get("url") or "").strip()
        if title and link:
            out.append({
                "title": title,
                "url": link,
                "snippet": str(item.get("content") or "").strip(),
            })
    return out


def format_results(results: list[dict[str, str]]) -> str:
    """Render results as plain text for a follow-up prompt to the model."""
    if not results:
        return "No results — the search returned nothing, or the search engine could not be reached."
    lines = []
    for i, r in enumerate(results, 1):
        line = f"{i}. {r['title']} — {r['url']}"
        if r.get("snippet"):
            line += f"\n   {r['snippet']}"
        lines.append(line)
    return "\n".join(lines)


def research_character(
    base_url: str, name: str, *, limit: int = 5, timeout: float = 8.0
) -> list[dict[str, str]]:
    """Find grounded background for a named character's speaking style.

    The configured SearXNG service remains the first choice. Public search
    engines sometimes suspend every SearXNG request at once, though, so an
    exact-title Wikipedia lookup is a narrow fallback. Requiring an exact
    title match prevents an original character with a short name from being
    mistaken for an unrelated public figure.
    """
    base_url = (base_url or "").strip()
    name = (name or "").strip()
    if not base_url or not name:
        return []
    query = f'"{name}" character personality speech patterns dialogue'
    results = search(base_url, query, limit=limit, timeout=timeout)
    if results:
        return results

    params = urllib.parse.urlencode({
        "action": "query",
        "generator": "search",
        "gsrsearch": f'"{name}"',
        "gsrlimit": min(limit, 5),
        "prop": "extracts|info",
        "exintro": "1",
        "explaintext": "1",
        "exchars": "2500",
        "inprop": "url",
        "format": "json",
    })
    try:
        req = urllib.request.Request(
            f"https://en.wikipedia.org/w/api.php?{params}",
            headers={"Accept": "application/json", "User-Agent": "Storyboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            doc: Any = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return []

    pages = ((doc.get("query") or {}).get("pages") or {}).values()
    exact = next(
        (page for page in pages
         if str(page.get("title") or "").strip().casefold() == name.casefold()),
        None,
    )
    if not exact or not exact.get("fullurl"):
        return []
    return [{
        "title": str(exact.get("title") or name),
        "url": str(exact["fullurl"]),
        "snippet": str(exact.get("extract") or "").strip(),
    }]
