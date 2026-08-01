"""name_that_ui — the NameThatUI component dictionary for site_imitate.

Fetches https://namethatui.com/ once, extracts the element catalog embedded in
the page's Next.js RSC payload (there is no public API), and fuzzy-matches
sloppy UI descriptions to canonical names + per-framework API symbols
(shadcn/ui first) + paste-ready coding-agent prompts.

The helper is stdlib-only and fails soft: any network/parse error returns an
empty catalog so callers (site_imitate) proceed without the dictionary.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request

URL = "https://namethatui.com/"
USER_AGENT = "name_that_ui/1.0 (+site_imitate workflow)"
DEFAULT_CACHE = os.path.join(tempfile.gettempdir(), "namethatui_elements.json")
DEFAULT_TTL = 86400  # refresh the catalog daily


# --------------------------------------------------------------------------- #
# Catalog extraction (page-is-the-API)
# --------------------------------------------------------------------------- #
def js_unescape(s: str) -> str:
    """Decode one level of a JS double-quoted string literal body."""
    out: list[str] = []
    i = 0
    n = len(s)
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "u" and i + 6 <= n:
                try:
                    out.append(chr(int(s[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fetch_html() -> str:
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "ignore")


def fetch_catalog() -> list[dict]:
    """Fetch the homepage and pull every element record out of the RSC payload."""
    raw = _fetch_html()
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', raw, re.S)
    payload = "".join(js_unescape(c) for c in chunks)

    records: list[dict] = []
    pos = 0
    while True:
        i = payload.find('{"slug":', pos)
        if i < 0:
            break
        # brace-match the record (JSON-aware: backslash escapes inside strings)
        depth = 0
        in_str = False
        j = i
        end = None
        while j < len(payload):
            ch = payload[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            j += 1
        if end is None:
            break
        try:
            records.append(json.loads(payload[i:end]))
        except json.JSONDecodeError:
            pass
        pos = end
    return records


def load_catalog(cache_path: str | None = None, ttl: int = DEFAULT_TTL) -> list[dict]:
    """Return the catalog, from cache when fresh, otherwise fetched and cached.

    Fails soft: any error returns ``[]``.
    """
    cache_path = cache_path or DEFAULT_CACHE
    try:
        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < ttl:
                with open(cache_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list) and data:
                    return data
    except Exception:  # noqa: BLE001 - cache read must not break the workflow
        pass
    try:
        records = fetch_catalog()
        if records:
            try:
                with open(cache_path, "w", encoding="utf-8") as fh:
                    json.dump(records, fh, ensure_ascii=False)
            except Exception:  # noqa: BLE001 - caching is best-effort
                pass
        return records
    except Exception:  # noqa: BLE001 - network failure -> soft
        return []


# --------------------------------------------------------------------------- #
# Fuzzy lookup
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    s = (s or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def lookup(query: str, records: list[dict], top: int = 5) -> list[dict]:
    """Rank the catalog against a sloppy description, like the site's search."""
    q = _norm(query)
    qwords = set(q.split())
    scored: list[tuple[float, dict]] = []
    for r in records:
        hay = [r.get("name", ""), r.get("tagline", ""), r.get("description", "")]
        hay += list(r.get("aka") or [])
        hay += list(r.get("fuzzy") or [])
        blob = _norm(" ".join(hay))
        score = 0.0
        for field in list(r.get("fuzzy") or []) + list(r.get("aka") or []):
            if q and q in _norm(field):
                score += 3.0
        if q and q in _norm(r.get("name", "")):
            score += 3.0
        if q and q in _norm(r.get("description", "")):
            score += 2.0
        score += len(qwords & set(blob.split())) * 0.5
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:top]]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def shadcn_symbols(record: dict) -> list[str]:
    """Return the shadcn/ui symbols for a record, if any."""
    return [
        a.get("symbol")
        for a in (record.get("api") or [])
        if isinstance(a, dict) and "shadcn" in str(a.get("framework", "")).lower()
    ]


def match_brief(record: dict) -> dict[str, object]:
    """Compact, JSON-safe summary of a match for tool results."""
    apis = [
        {"framework": a.get("framework"), "symbol": a.get("symbol")}
        for a in (record.get("api") or [])
        if isinstance(a, dict)
    ]
    return {
        "name": record.get("name"),
        "platform": record.get("platform"),
        "api": apis,
        "shadcn": shadcn_symbols(record),
        "prompt": (record.get("prompt") or "")[:600],
    }


def format_matches(matches: list[dict]) -> str:
    """Human-readable match list for the agent's context."""
    lines = []
    for r in matches:
        name = r.get("name") or "?"
        platform = r.get("platform") or "?"
        symbols = ", ".join(
            str(a.get("symbol"))
            for a in (r.get("api") or [])
            if isinstance(a, dict) and a.get("symbol")
        )
        shad = ", ".join(shadcn_symbols(r)) or "-"
        prompt = (r.get("prompt") or "").strip() or "-"
        lines.append(
            f"* {name} ({platform})\n"
            f"  api symbols: {symbols or '-'}\n"
            f"  shadcn/ui  : {shad}\n"
            f"  prompt     : {prompt[:300]}"
        )
    return "\n".join(lines)


def inventory_line(record: dict) -> str:
    """One compact inventory line: observed -> Canonical Name (symbol)."""
    shad = shadcn_symbols(record)
    symbol = shad[0] if shad else None
    if not symbol:
        for a in record.get("api") or []:
            if isinstance(a, dict) and a.get("symbol") and "ARIA" in str(a.get("framework", "")):
                symbol = a.get("symbol")
                break
    suffix = f" ({symbol})" if symbol else ""
    return f"{record.get('name')}{suffix}"


# --------------------------------------------------------------------------- #
# Catalog listing
# --------------------------------------------------------------------------- #
def list_names(
    records: list[dict],
    platform: str = "",
    query: str = "",
    top: int | None = None,
) -> list[dict]:
    """Return a compact, sorted list of catalog component names.

    Args:
        records: The catalog (list of element records).
        platform: Optional filter - "web" or "macos" (case-insensitive).
        query: Optional keyword filter, matched against name, aliases, and
            fuzzy descriptions.
        top: Optional cap on how many names to return.
    """
    platform = (platform or "").strip().lower()
    q = _norm(query)
    out: list[dict] = []
    for r in records:
        name = r.get("name")
        if not isinstance(name, str) or not name:
            continue
        rp = str(r.get("platform") or "").lower()
        if platform and rp != platform:
            continue
        if q:
            hay = _norm(
                " ".join(
                    [name]
                    + [str(r.get("tagline") or "")]
                    + [str(r.get("description") or "")]
                    + [str(a) for a in (r.get("aka") or [])]
                    + [str(f) for f in (r.get("fuzzy") or [])]
                )
            )
            if q not in hay and not any(q in _norm(w) for w in [name]):
                continue
        out.append(
            {
                "name": name,
                "platform": r.get("platform") or "web",
                "shadcn": shadcn_symbols(r),
            }
        )
    out.sort(key=lambda x: (str(x["platform"]), str(x["name"]).lower()))
    if top is not None and top > 0:
        out = out[:top]
    return out


def format_name_list(names: list[dict]) -> str:
    """Human-readable, platform-grouped listing of component names."""
    if not names:
        return "(no components match the filter)"
    by_platform: dict[str, list[str]] = {}
    for n in names:
        p = str(n.get("platform") or "web")
        by_platform.setdefault(p, []).append(str(n.get("name")))
    lines = []
    for p in sorted(by_platform):
        entries = by_platform[p]
        lines.append(f"[{p}] ({len(entries)}):")
        lines.append(", ".join(entries))
    return "\n".join(lines)
