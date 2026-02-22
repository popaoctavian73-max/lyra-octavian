import os
import time
import re
from datetime import datetime, date
from typing import Dict, List, Optional

import requests


# ============================
# High-level intent detection
# (General, abundant markers)
# ============================

def _is_project_query(q: str) -> bool:
    t = (q or "").lower()
    markers = (
        # Project / governance / constitution / normative
        "lyra", "algorahumani", "constitution", "constitutional", "preamble",
        "article", "articles", "art.", "art ", "glossary", "definition", "definitions",
        "governance", "institution", "institutions", "framework", "policy", "norm", "normative",
        "supremacy", "authority", "jurisdiction", "nullity", "void", "no legal effect",
        "amendment", "amendments", "ratify", "enforce", "obligation", "prohibition", "sanction",

        # Engineering keywords that may be part of project discussions
        "rag", "retrieval", "embedding", "vectorstore", "faiss", "chunk", "chunking",
        "rerank", "ollama", "fastapi", "admin", "ingest", "docs", "web search", "ddg",
        "system prompt", "prompt", "policy layer",
    )
    return any(m in t for m in markers)


def _is_volatile_query(q: str) -> bool:
    """
    Volatile = facts that can change quickly, where we must demand recency.
    This is broad by design.
    """
    t = (q or "").lower().strip()
    markers = (
        # Explicit recency cues
        "today", "now", "current", "latest", "recent", "live", "right now", "as of", "updated",

        # Time / date / timezones
        "time", "current time", "what time", "time in", "local time", "timezone", "utc", "gmt",
        "date", "today's date",

        # Weather
        "weather", "forecast", "temperature", "rain", "wind", "humidity", "precipitation", "snow", "storm",

        # Markets / prices / rates
        "price", "prices", "cost", "rate", "exchange rate", "fx", "currency", "curs",
        "stock", "stocks", "share price", "market cap", "crypto", "bitcoin", "eth", "ethereum",
        "inflation", "cpi", "interest rate", "yield", "bond", "gdp",

        # Sports / events
        "score", "scores", "result", "results", "standings", "table", "fixture", "fixtures",
        "schedule", "kickoff", "game time", "match time",

        # News / politics / breaking changes
        "news", "headline", "breaking", "election", "poll", "war", "conflict", "sanctions",
        "ceo", "president", "prime minister",

        # Availability / operational status
        "open now", "closing time", "hours", "status", "outage", "down", "incident",
        "availability", "traffic", "delay",
    )
    return any(m in t for m in markers)


# ============================
# Query rewriting (general)
# ============================

def _rewrite_query(q: str) -> str:
    """
    Rewrite only to improve freshness/precision WITHOUT assuming any location.
    - Project queries: keep intact (Web is comparative material).
    - Volatile queries: push recency signals.
    """
    qq = (q or "").strip()
    if not qq:
        return qq

    if _is_project_query(qq):
        return qq

    if _is_volatile_query(qq):
        return f"{qq} updated today now as of"

    return qq


# ============================
# Freshness helpers
# ============================

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_RE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_RE_DOTS = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
_RE_MDY = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(20\d{2})\b")
_RE_DMY = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})(?:,)?\s+(20\d{2})\b")


def _parse_date_from_text(s: str) -> Optional[date]:
    if not s:
        return None
    t = s.strip()

    m = _RE_ISO.search(t)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return date(y, mo, d)
        except Exception:
            return None

    m = _RE_DOTS.search(t)
    if m:
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return date(y, mo, d)
        except Exception:
            return None

    m = _RE_MDY.search(t)
    if m:
        mon = (m.group(1) or "").lower()
        try:
            mo = _MONTHS.get(mon)
            if not mo:
                return None
            d = int(m.group(2))
            y = int(m.group(3))
            return date(y, mo, d)
        except Exception:
            return None

    m = _RE_DMY.search(t)
    if m:
        mon = (m.group(2) or "").lower()
        try:
            mo = _MONTHS.get(mon)
            if not mo:
                return None
            d = int(m.group(1))
            y = int(m.group(3))
            return date(y, mo, d)
        except Exception:
            return None

    return None


def _has_recency_cue(snippet: str) -> bool:
    t = (snippet or "").lower()
    cues = (
        "today", "now", "current", "updated", "as of", "live", "just now", "minutes ago", "hours ago",
        "heute", "jetzt", "aktuell", "vor",
        "azi", "acum", "curent", "actualizat", "în urmă", "in urma",
    )
    return any(c in t for c in cues)


def _is_stale_for_volatile(snippet: str, today: date, max_age_days: int) -> bool:
    dt = _parse_date_from_text(snippet or "")
    if dt is None:
        return False
    try:
        delta = abs((today - dt).days)
        return delta > max_age_days
    except Exception:
        return False


def _env_int(name: str, default: int) -> int:
    try:
        v = (os.getenv(name, "") or "").strip()
        return int(v) if v else default
    except Exception:
        return default


# ============================
# Output caps (CPU-friendly)
# ============================

def _cap_web_results(results: List[Dict[str, str]], max_total_chars: int, max_chars_per_source: int) -> List[Dict[str, str]]:
    if max_total_chars <= 0 and max_chars_per_source <= 0:
        return results

    out: List[Dict[str, str]] = []
    used = 0

    for item in results or []:
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        snippet = (item.get("snippet") or "").strip()

        if max_chars_per_source > 0 and snippet and len(snippet) > max_chars_per_source:
            snippet = snippet[:max_chars_per_source].rstrip() + "…"

        contrib = len(title) + len(url) + len(snippet) + 12

        if max_total_chars > 0 and (used + contrib) > max_total_chars:
            if not out and snippet and max_total_chars > 0:
                budget = max(0, max_total_chars - (len(title) + len(url) + 12))
                if budget > 0 and len(snippet) > budget:
                    snippet = snippet[:budget].rstrip() + "…"
                out.append({"title": title, "url": url, "snippet": snippet})
            break

        out.append({"title": title, "url": url, "snippet": snippet})
        used += contrib

        if max_total_chars > 0 and used >= max_total_chars:
            break

    return out


# ============================
# Providers
# ============================

def _search_ddg(q2: str, max_results: int, timeout: int, region: str, timelimit: str) -> List[Dict[str, str]]:
    # Compatible import across duckduckgo_search versions
    DDGS = None
    try:
        from duckduckgo_search import DDGS as _DDGS  # type: ignore
        DDGS = _DDGS
    except Exception:
        try:
            from ddgs import DDGS as _DDGS  # type: ignore
            DDGS = _DDGS
        except Exception:
            DDGS = None

    if DDGS is None:
        return []

    out: List[Dict[str, str]] = []
    with DDGS(timeout=timeout) as ddgs:
        for r in ddgs.text(
            q2,
            region=region,
            safesearch="off",
            timelimit=timelimit,
            max_results=max_results,
        ):
            title = (r.get("title") or "").strip()
            url = (r.get("href") or "").strip()
            snippet = (r.get("body") or "").strip()
            if title or url or snippet:
                out.append({"title": title, "url": url, "snippet": snippet})
    return out


def _search_brave(q2: str, max_results: int, timeout: int, region: str, timelimit: str) -> List[Dict[str, str]]:
    """
    Brave Web Search API.
    Env:
      - LYRA_BRAVE_API_KEY (or BRAVE_API_KEY)
      - LYRA_BRAVE_ENDPOINT (optional; default Brave endpoint)
    """
    api_key = (os.getenv("LYRA_BRAVE_API_KEY") or os.getenv("BRAVE_API_KEY") or "").strip()
    if not api_key:
        return []

    endpoint = (os.getenv("LYRA_BRAVE_ENDPOINT") or "https://api.search.brave.com/res/v1/web/search").strip()
    count = max(1, min(int(max_results), 20))

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
        "Accept-Language": region,
    }

    params = {
        "q": q2,
        "count": count,
        "offset": 0,
        "safesearch": "off",
    }

    t = (max(2, min(timeout, 20)), max(5, min(timeout, 30)))
    r = requests.get(endpoint, headers=headers, params=params, timeout=t)
    r.raise_for_status()
    data = r.json() if r.content else {}

    results: List[Dict[str, str]] = []
    web = data.get("web") or {}
    items = web.get("results") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        snippet = (it.get("description") or it.get("snippet") or "").strip()
        if title or url or snippet:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _search_searxng(q2: str, max_results: int, timeout: int, region: str, timelimit: str) -> List[Dict[str, str]]:
    base = (os.getenv("LYRA_SEARXNG_URL") or "http://127.0.0.1:8080").strip().rstrip("/")
    url = f"{base}/search"

    params = {
        "q": q2,
        "format": "json",
        "categories": "general",
        "language": region.split("-")[0] if "-" in region else region,
    }

    t = (max(2, min(timeout, 20)), max(5, min(timeout, 30)))
    r = requests.get(url, params=params, timeout=t)
    r.raise_for_status()
    data = r.json() if r.content else {}
    items = data.get("results") or []

    out: List[Dict[str, str]] = []
    for it in items[:max_results]:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        link = (it.get("url") or it.get("link") or "").strip()
        snippet = (it.get("content") or it.get("snippet") or "").strip()
        if title or link or snippet:
            out.append({"title": title, "url": link, "snippet": snippet})
    return out


# ============================
# Public entrypoint
# ============================

def ddg_search(q: str, max_results: Optional[int] = None, timeout: Optional[int] = None) -> List[Dict[str, str]]:
    """
    Backward compatible function name (ddg_search),
    but now acts as a general Web search wrapper with provider selection.

    Provider selection:
      - LYRA_WEB_PROVIDER = brave | searxng | ddg
      Default: ddg
    """
    q = (q or "").strip()
    if not q:
        return []

    if max_results is None:
        max_results = _env_int("LYRA_WEB_MAX_RESULTS", 3)
    else:
        try:
            max_results = int(max_results)
        except Exception:
            max_results = _env_int("LYRA_WEB_MAX_RESULTS", 3)

    if timeout is None:
        timeout = _env_int("LYRA_WEB_TIMEOUT", 8)
    else:
        try:
            timeout = int(timeout)
        except Exception:
            timeout = _env_int("LYRA_WEB_TIMEOUT", 8)

    if timeout <= 0:
        timeout = _env_int("LYRA_WEB_TIMEOUT", 8)

    HARD_MAX_RESULTS = 20
    if max_results <= 0:
        max_results = HARD_MAX_RESULTS
    else:
        max_results = min(max_results, HARD_MAX_RESULTS)

    provider = (os.getenv("LYRA_WEB_PROVIDER") or "ddg").strip().lower()
    region = (os.getenv("LYRA_WEB_REGION") or "de-de").strip()

    q2 = _rewrite_query(q)
    is_project = _is_project_query(q)
    is_volatile = _is_volatile_query(q)

    timelimit = "m"
    if is_volatile:
        timelimit = "d"
    elif is_project:
        timelimit = "y"

    max_age_days = _env_int("LYRA_WEB_VOLATILE_MAX_AGE_DAYS", 1)
    today = datetime.utcnow().date()

    max_total_chars = _env_int("LYRA_WEB_MAX_TOTAL_CHARS", 1500)
    max_chars_per_source = _env_int("LYRA_WEB_MAX_CHARS_PER_SOURCE", 900)

    t0 = time.perf_counter()
    raw: List[Dict[str, str]] = []
    used_provider = provider

    try:
        if provider == "brave":
            raw = _search_brave(q2, max_results=max_results, timeout=timeout, region=region, timelimit=timelimit)
            if not raw:
                used_provider = "ddg"
                raw = _search_ddg(q2, max_results=max_results, timeout=timeout, region=region, timelimit=timelimit)

        elif provider == "searxng":
            raw = _search_searxng(q2, max_results=max_results, timeout=timeout, region=region, timelimit=timelimit)
            if not raw:
                used_provider = "ddg"
                raw = _search_ddg(q2, max_results=max_results, timeout=timeout, region=region, timelimit=timelimit)

        else:
            used_provider = "ddg"
            raw = _search_ddg(q2, max_results=max_results, timeout=timeout, region=region, timelimit=timelimit)

    except Exception:
        try:
            used_provider = "ddg"
            raw = _search_ddg(q2, max_results=max_results, timeout=timeout, region=region, timelimit=timelimit)
        except Exception:
            raw = []

    out = raw
    filtered = 0
    marked_uncertain = 0

    if is_volatile and raw:
        tmp: List[Dict[str, str]] = []
        for item in raw:
            sn = (item.get("snippet") or "").strip()

            if sn and _is_stale_for_volatile(sn, today=today, max_age_days=max_age_days):
                filtered += 1
                continue

            if sn and (not _has_recency_cue(sn)) and (_parse_date_from_text(sn) is None):
                item = dict(item)
                item["snippet"] = "UNCERTAIN_TIMESTAMP: " + sn
                marked_uncertain += 1

            tmp.append(item)

        if tmp:
            out = tmp
        else:
            out = []
            for item in raw:
                sn = (item.get("snippet") or "").strip()
                if sn:
                    item = dict(item)
                    item["snippet"] = "UNCERTAIN_TIMESTAMP: " + sn
                    marked_uncertain += 1
                out.append(item)

    out = _cap_web_results(out, max_total_chars=max_total_chars, max_chars_per_source=max_chars_per_source)

    ms = int((time.perf_counter() - t0) * 1000)
    print(
        f"[WEB] search ok provider={used_provider} q='{q[:60]}' q2='{q2[:60]}' "
        f"results={len(out)} raw={len(raw)} filtered={filtered} uncertain={marked_uncertain} "
        f"volatile={is_volatile} project={is_project} timelimit={timelimit} region={region} ms={ms} "
        f"max_results={max_results} timeout={timeout} cap_total={max_total_chars} cap_per={max_chars_per_source}"
    )
    return out
