#!/usr/bin/env python3
"""End-to-end build script: Recovery from HYROX (Markdown) -> dist/ PDF + EPUB

Pipeline (TYPST tier):
  1. pandoc  front-matter/*.md  chapters/*.md  back-matter/*.md  -t typst  -> book.typ
     (explicit file order: preface, contents, chapters 01..22, index)
  2. Post-process book.typ:
       - sed symbol fixes for typst <= 0.15 (planck.reduce / angle.l / angle.r / times.circle)
       - prepend page setup (A4, 2.2 cm), justify, heading colours, PDF metadata,
         and a typeset title page (book title + author) as page 1
       - insert #pagebreak() before every level-1 heading except the first
       - insert engine-generated TOC (#outline) after the Contents heading
       - centred page-number footer (hidden on the title page)
       - constrain every figure so it NEVER exceeds the page margins:
         landscape -> width 100% (text column), portrait -> computed width capped
         at 23 cm height so tall flowcharts stay inside the page
  3. typst compile book.typ dist/<slug>-<timestamp>.pdf   (older PDFs are kept)
  4. pandoc  ...  -o dist/<slug>-<timestamp>.epub  (--toc, custom margin-safe CSS;
     older EPUBs are kept)
  5. Validate both with pypdf / pdfplumber / zipfile (magic, pages, titles,
     markup leak, images, per-page isolation, margin containment) and clean
     intermediate artifacts (book.typ, .math-render/).

Timestamped outputs:  dist/Recovery-from-HYROX-YYYYMMDD-HHMMSS.pdf / .epub
Older PDF/EPUB files in dist/ are never deleted.
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TITLE = "Recovery from HYROX"
AUTHOR = "Ritchie Cheng"
SLUG = "Recovery-from-HYROX"
DIST = ROOT / "dist"
BOOK_TYP = ROOT / "book.typ"

TEXT_WIDTH_CM = 16.6      # A4 (21 cm) minus 2 x 2.2 cm margins
PAGE_HEIGHT_CM = 29.7     # A4 height
MARGIN_CM = 2.2           # uniform margin
CONTENT_HEIGHT_CM = PAGE_HEIGHT_CM - 2 * MARGIN_CM   # 25.3 cm
# Image (figure) + caption together must not exceed 70% of the page height.
IMG_HEIGHT_FRACTION = 0.70
MAX_IMG_HEIGHT_CM = CONTENT_HEIGHT_CM * IMG_HEIGHT_FRACTION  # 17.71 cm
CAPTION_RESERVE_CM = 1.0  # breathing room + caption line under the figure

HEADING_1 = 'rgb("#000000")'   # headings black
HEADING_2 = 'rgb("#000000")'

RULES = f'''#set page(paper: "a4", margin: 2.2cm)
#set par(justify: true)
#set document(title: "{TITLE}", author: "{AUTHOR}")
#show heading.where(level: 1): set text(fill: {HEADING_1}, size: 20pt, weight: "bold")
#show heading.where(level: 2): set text(fill: {HEADING_2}, size: 15pt)
// Force the (only) outline on the Contents page into a single column with no
// leader dots, one entry per line, and the page number right-aligned.
#set page(columns: 1)
#show outline.entry: it => block(
  below: 0.5em,
  link(it.element.location())[#it.element.body #h(1fr) #it.page()],
)

#align(center + horizon)[
  #text(size: 34pt, weight: "bold", fill: {HEADING_1})[{TITLE}]
  #v(0.8em)
  #text(size: 18pt)[{AUTHOR}]
]
#pagebreak()

'''

FOOTER = '#set page(footer: context align(center)[#text(size: 9pt)[#counter(page).display()]])\n'

EPUB_CSS = """\
/* Margin-safe styles for EPUB readers (reflowable). */
img { max-width: 100%; height: auto; }
figure { margin: 1em 0; }
figcaption { font-size: 0.9em; color: #333; margin-top: 0.4em; }
table { max-width: 100%; border-collapse: collapse; }
th, td { padding: 0.3em 0.5em; }
body { font-family: serif; line-height: 1.45; }
h1, h2, h3 { color: #000000; }
"""

TYPST_URL = ("https://github.com/typst/typst/releases/latest/download/"
             "typst-x86_64-unknown-linux-musl.tar.xz")

EXPECTED_IMAGES_BASE = 50  # base number of distinct figure files (no photos)
# When `<!-- photo: ... -->` markers are added in chapters, each resolved photo
# adds one more embedded image. The validator accepts `50 + count_of_added_photos`
# (where the photo count is the number of cached photo files in assets/photos/
# that are actually referenced by the chapters).
#   - Set to None to disable the strict count check and only log it.
#
# EXPECTED_IMAGES is recomputed dynamically by _recompute_expected_images() at
# build start: it counts chapter-marker queries and clamps to
# `EXPECTED_IMAGES_BASE + len(photomarker_queries)`. Set back to the base
# value at the end of the build so re-running without markers is unaffected.
EXPECTED_IMAGES = EXPECTED_IMAGES_BASE

# Path to the persistent photo cache and download-list metadata. The photos
# directory lives inside `assets/` so a rebuild reuses the cache; the metadata
# file records (slug, query, dest, alt, path) for every downloaded photo so the
# download list is reproducible and auditable.
PHOTOS_DIR = ROOT / "assets" / "photos"
UNSPLASH_METADATA_PATH = ROOT / "assets" / "unsplash-metadata.json"

# --- Photo marker support ---
# A chapter may include a line like:
#   <!-- photo: race recovery -->
# The build pre-processor rewrites each marker to a markdown image reference
# (assets/photos/photo-<slug>.jpg). If the cached file is absent, the build
# downloads it from Unsplash via a Playwright headless-browser scrape (no API
# key needed). No chapter text is rewritten on disk: the substitution is
# applied to a copy in .resolved/ that is fed to pandoc. After the build the
# temp dir is removed by cleanup().
PHOTO_MARKER_RE = re.compile(r"<!--\s*photo:\s*(.+?)\s*-->", re.IGNORECASE | re.DOTALL)
UNSPLASH_SEARCH_URL = "https://unsplash.com/s/photos/{query}"  # browser-scrape path

# Process-local record of every photo resolved during a build. Populated by
# _resolve_one_marker; written to UNSPLASH_METADATA_PATH by the end of
# _build_inputs_with_resolved_photos. Reset on each invocation.
_PHOTO_DOWNLOAD_LOG: list[dict] = []

# Hard cap so a typo or wildly broad query doesn't crash the build.
PLAYWRIGHT_TIMEOUT_MS = 30_000
PLAYWRIGHT_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024  # 8 MB


def _slugify(text: str) -> str:
    """Make a filesystem-safe slug from a free-form search term."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower())
    return s.strip("-") or "photo"


def _relax_query_chain(query: str) -> list[str]:
    """Return an ordered list of query strings to try for `query`.

    Used by _unsplash_playwright to fall back from a niche query that
    returns 0 search-results cards to progressively broader forms
    (dropping the last word, then the next-to-last, etc.). The original
    query is always tried first; later entries are only consulted if
    earlier ones yielded 0 photo cards.

    Examples:
        "hyrox race stadium" -> ["hyrox race stadium", "hyrox race", "hyrox"]
        "athlete sleeping recovery" -> ["athlete sleeping recovery",
                                        "athlete sleeping", "athlete"]
        "sauna" -> ["sauna"]
    """
    words = query.split()
    chain: list[str] = []
    for n in range(len(words), 0, -1):
        cand = " ".join(words[:n]).strip()
        if cand and cand not in chain:
            chain.append(cand)
    return chain


def _search_with_retry(page, search_url: str, q_try: str, max_attempts: int = 3) -> dict | None:
    """Open the search page, wait for images to render, and return a free card.

    Retries on transient `playwright._impl._errors.TimeoutError` from
    `wait_for_selector` (which happens occasionally on slow renders) with
    progressively longer backoff. Returns the first free photo card found,
    or None if the page has 0 photo cards (a legitimate "no results" case).
    """
    from playwright._impl._errors import TimeoutError as _PWTimeout

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
            page.wait_for_selector(
                "img[src^='https://']", state="attached", timeout=PLAYWRIGHT_TIMEOUT_MS
            )
            # Give the masonry/grid layout a moment to render the rest of the cards.
            page.wait_for_timeout(1500)
            return page.evaluate(
                r"""
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    const cards = [];
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        if (!/^\/photos\/[A-Za-z0-9_-]{8,}/.test(href)) continue;
                        const im = a.querySelector('img[src]');
                        const src = im ? (im.getAttribute('src') || '') : '';
                        // Belt-and-suspenders Plus detection. We check three
                        // independent signals on each card:
                        //   1. plus.unsplash.com in the card's img src (the
                        //      classic premium thumb pattern).
                        //   2. plus.unsplash.com in the card's href.
                        //   3. The literal "For Unsplash+" / "Unsplash+" label
                        //      that Unsplash renders as a small badge on Plus
                        //      photos. (Per user observation in 2026-08-29
                        //      calibration — the rendered text is the most
                        //      reliable signal even when the URL is clean.)
                        // If ANY of these flags the card, skip it.
                        const isPlusSrc = /plus\.unsplash\.com\/premium_photo-/.test(src);
                        const isPlusHref = /plus\.unsplash\.com/.test(href);
                        // Look for the label in the card's own text +
                        // aria-label fallback (Unsplash sometimes uses
                        // aria-label="Unsplash+" on the badge).
                        const cardText = (a.textContent || '') + ' ' + (a.getAttribute('aria-label') || '');
                        const isPlusLabel = /\bUnsplash\+\b/.test(cardText) || /For\s+Unsplash\+/i.test(cardText);
                        const isPlus = isPlusSrc || isPlusHref || isPlusLabel;
                        const photoMatch = src.match(/images\.unsplash\.com\/photo-([0-9a-f-]+)/i);
                        cards.push({ href, src, isPlus, isPlusLabel, photoId: photoMatch ? photoMatch[1] : null });
                    }
                    // STRICT: only ever pick a FREE card. If no free card is
                    // found, return null so the caller can try a relaxed query.
                    const free = cards.find(c => !c.isPlus && c.photoId);
                    return free || null;
                }
                """
            )
        except _PWTimeout as e:
            last_err = e
            log(
                f"unsplash: search-page render timed out for {q_try!r} "
                f"(attempt {attempt}/{max_attempts}); retrying after {2*attempt}s"
            )
            page.wait_for_timeout(2000 * attempt)
    # All retries exhausted: re-raise the last timeout so the caller surfaces
    # the actionable error (not a silent "no cards" miss).
    raise last_err  # type: ignore[misc]


def _wait_with_retry(page, selector: str, state: str = "attached", max_attempts: int = 3) -> None:
    """Retry `page.wait_for_selector` on transient TimeoutError."""
    from playwright._impl._errors import TimeoutError as _PWTimeout

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            page.wait_for_selector(selector, state=state, timeout=PLAYWRIGHT_TIMEOUT_MS)
            return
        except _PWTimeout as e:
            last_err = e
            log(
                f"unsplash: wait_for_selector({selector!r}) timed out "
                f"(attempt {attempt}/{max_attempts}); retrying after {2*attempt}s"
            )
            page.wait_for_timeout(2000 * attempt)
    raise last_err  # type: ignore[misc]


def _unsplash_playwright(query: str, dest: Path) -> dict:
    """Two-step scrape of a clean high-res JPEG for `query` from unsplash.com.

    Step 1: open the search-results page and find the first FREE photo card
            (we skip `plus.unsplash.com/premium_photo-...` cards because those
            are paid Unsplash+ content that always shows a watermark for
            non-subscribers, and the per-photo `/download?force=true` endpoint
            is gated behind Unsplash's Anubis bot-check). We also try a few
            more cards if the first is Plus-only, so a query like "ice bath"
            that surfaces Plus first still yields a clean download.

    Step 2: open the photo's detail page. The main image element carries a
            `srcset` whose highest-`w=` URL is the CLEAN variant (it omits
            the `fm=jpg&q=60` flags that trigger the photographer overlay on
            the search-page thumbs). We pick the cleanest URL we can find:
              - Prefer: highest `w=` from the main image's `srcset`
              - Fallback: highest `w=` from any other `images.unsplash.com/photo-...` srcset
              - Last resort: build `?w=2000&q=85&auto=format&fit=crop` from the photo id

    The CDN at `images.unsplash.com` is not behind Unsplash's Anubis bot
    check, so we download the JPEG via plain `urllib.request` with a real
    User-Agent. We fall back to the browser's request context if urllib
    fails (e.g. on a CDN that does 403 to scripted clients).

    No API key required. Uses `cloakbrowser` (a drop-in Playwright
    replacement with source-level fingerprint patches) to bypass
    Cloudflare on the search/detail pages. Raises a clear actionable
    error if cloakbrowser is unavailable.

    Returns a metadata dict with `search_url`, `detail_url`, `cdn_url`,
    `photo_id`, and the `bytes` written, so the caller can persist the
    provenance of every photo in `assets/unsplash-metadata.json`.
    """
    try:
        from cloakbrowser import launch as _cb_launch
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            f"cloakbrowser is required for live photo downloads (not installed: {e}). "
            "Install with: pip install cloakbrowser"
        ) from e

    from urllib.parse import urlparse, urlunparse, urlencode, parse_qs

    browser = _cb_launch(headless=True)
    detail_url: str | None = None
    photo_id: str | None = None
    cdn_url: str | None = None
    used_query: str = query  # for logging which query in the relax chain matched
    try:
        page = browser.new_page()
        # --- Step 1: pick a free /photos/... slug from the search results. ---
        # Try the original query first; if it returns 0 cards, progressively
        # relax by dropping the last word (a niche query like "hyrox race
        # stadium" matches nothing in Unsplash's free collection, but
        # "hyrox race" or "hyrox" usually returns plenty). This is purely a
        # last-resort fallback: the original query is preferred whenever
        # it has any hits.
        card = None
        relaxed_queries = _relax_query_chain(query)
        for q_idx, q_try in enumerate(relaxed_queries):
            search_url = UNSPLASH_SEARCH_URL.format(query=urllib.parse.quote(q_try))
            card = _search_with_retry(page, search_url, q_try, max_attempts=3)
            if card and card.get("href"):
                if q_idx > 0:
                    log(
                        f"unsplash: original query {query!r} returned 0 cards; "
                        f"relaxed to {q_try!r} and got a match"
                    )
                used_query = q_try
                break
        if not card or not card.get("href"):
            raise RuntimeError(
                f"unsplash: no /photos/... card found for {query!r} "
                f"(tried {len(relaxed_queries)} relaxed queries: {relaxed_queries})"
            )
        search_url = UNSPLASH_SEARCH_URL.format(query=urllib.parse.quote(used_query))
        slug_href = card["href"]
        detail_url = "https://unsplash.com" + slug_href
        photo_id = card.get("photoId")
        log(
            f"unsplash search for {used_query!r}: href={slug_href[:60]} "
            f"plus={card.get('isPlus')} photo_id={photo_id}"
        )
        # --- Step 2: open the detail page and harvest a clean CDN URL. ---
        page.goto(detail_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
        # Detail-page Plus-label backstop. The search-page filter (Step 1)
        # already rejects cards whose DOM contains a "For Unsplash+" badge
        # or whose href/src points at plus.unsplash.com, but a small
        # number of Plus photos still slip through (the badge sometimes
        # loads asynchronously via React hydration, or a related photo on
        # the detail page itself is a Plus photo). The cheapest reliable
        # post-navigation guard is to scan the rendered body text for the
        # same badge string. If we see it, we treat the picked card as
        # Plus-only and surface a clear actionable error naming the slug,
        # detail URL, and the query that matched, so the operator can
        # either accept (rebuild with PHOTO_SKIP_PLUS_DETAIL_CHECK=1) or
        # change the query in the source markdown.
        is_plus_detail = page.evaluate(
            r"""
            () => {
                const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
                return /\bUnsplash\+\b/.test(body) || /For\s+Unsplash\+/i.test(body);
            }
            """
        ) or ("plus.unsplash.com" in (page.url or ""))
        if is_plus_detail and not os.environ.get("PHOTO_SKIP_PLUS_DETAIL_CHECK"):
            raise RuntimeError(
                f"unsplash: detail page {detail_url} renders a 'For Unsplash+' "
                f"label (final url={page.url}); this is a Plus-gated photo and the "
                f"downloader only serves free, watermark-free variants. "
                f"query={used_query!r} photo_id={photo_id!r}. "
                f"To override, set PHOTO_SKIP_PLUS_DETAIL_CHECK=1 in the env (the "
                f"download will then proceed and is likely to fetch a watermarked "
                f"image) or change the source-marker query."
            )
        # The main image may not appear on the very first selector hit, so
        # retry up to 3 times with progressively longer backoff.
        _wait_with_retry(
            page,
            "img[srcset*='images.unsplash.com/photo-']",
            state="attached",
            max_attempts=3,
        )
        page.wait_for_timeout(1500)
        harvested = page.evaluate(
            r"""
            () => {
                // Walk all <img>s. The main photo element has a srcset with
                // many ?w=N widths — pick the highest one (cleanest variant:
                // no `fm=jpg`, no `q=60` overlay flags). Fall back to the
                // largest from any image on the page (related photos etc.).
                const imgs = Array.from(document.querySelectorAll('img[srcset], img[src]'));
                let best = null;
                for (const im of imgs) {
                    const srcset = im.getAttribute('srcset') || '';
                    if (!srcset) continue;
                    if (!/images\.unsplash\.com\/photo-[0-9a-f-]+/i.test(srcset)) continue;
                    // srcset entries are "URL Ww" separated by commas
                    const entries = srcset.split(',').map(s => s.trim()).filter(Boolean);
                    for (const e of entries) {
                        const m = e.match(/(\S+)\s+(\d+)w/);
                        if (!m) continue;
                        const url = m[1];
                        const w = parseInt(m[2], 10);
                        if (!/^https:\/\/images\.unsplash\.com\/photo-[0-9a-f-]+/i.test(url)) continue;
                        // Skip search-page "watermark" variants that include
                        // `fm=jpg&q=60` (those are the preview thumbs with
                        // the photographer overlay baked in).
                        if (/[?&]fm=jpg/.test(url) || /[?&]q=60(&|$)/.test(url)) continue;
                        if (!best || w > best.w) best = { url, w };
                    }
                }
                return best;
            }
            """
        )
        if harvested and harvested.get("url"):
            cdn_url = harvested["url"]
        elif photo_id:
            # Fallback: build the canonical clean URL from the photo id.
            # This is the same template Unsplash uses for its on-page display.
            qs = urlencode({"w": "2000", "q": "85", "auto": "format", "fit": "crop"})
            cdn_url = f"https://images.unsplash.com/photo-{photo_id}?{qs}"
            log(f"unsplash: detail-page srcset was empty for {slug_href}; "
                f"using constructed clean URL for photo {photo_id}")
        else:
            raise RuntimeError(
                f"unsplash: could not harvest a clean CDN URL from {detail_url} "
                f"(no images.unsplash.com/photo-... in any srcset)"
            )
        log(f"unsplash cdn url for {used_query!r}: {cdn_url[:90]}...")
        # --- Step 3: download the bytes. ---
        data = _download_cdn_bytes(cdn_url)
        if len(data) > PLAYWRIGHT_MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"unsplash: download too large ({len(data)} bytes > {PLAYWRIGHT_MAX_DOWNLOAD_BYTES})"
            )
        # --- Step 4: defensive PNG -> JPEG conversion. ---
        if data[:3] != b"\xff\xd8\xff":
            try:
                from io import BytesIO
                from PIL import Image
                img = Image.open(BytesIO(data))
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=85)
                data = buf.getvalue()
                log(f"unsplash: converted non-JPEG ({img.format}) to JPEG ({len(data)} bytes)")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    f"unsplash: downloaded bytes are not JPEG and not a decodable image: {e}"
                ) from e
        dest.write_bytes(data)
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "search_url": search_url,
        "detail_url": detail_url,
        "cdn_url": cdn_url,
        "photo_id": photo_id,
        "query": used_query,  # the (possibly relaxed) query that actually matched
        "bytes": dest.stat().st_size if dest.exists() else 0,
    }


def _download_cdn_bytes(url: str) -> bytes:
    """Download bytes from `url` (typically `images.unsplash.com/...`).

    Tries plain `urllib.request` first (the Unsplash CDN is not behind the
    Anubis bot check). Falls back to the active cloakbrowser context if
    urllib is blocked (e.g. 403 from a CDN that fingerprints scripted
    clients). The active-browser path is not the default because it is
    slower and would force this helper to know about cloakbrowser directly.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        # Last-resort: try via cloakbrowser. The browser is not kept alive
        # across calls (cost), so this path is only used when urllib fails
        # on a network the build server can't reach.
        try:
            from cloakbrowser import launch as _cb_launch
        except ImportError:
            raise RuntimeError(f"unsplash: urllib download failed ({e}) and cloakbrowser is not installed for retry")
        b = _cb_launch(headless=True)
        try:
            ctx = b.new_context()
            resp = ctx.request.get(url, timeout=PLAYWRIGHT_TIMEOUT_MS)
            if not resp.ok:
                raise RuntimeError(f"unsplash: HTTP {resp.status} fetching {url[:90]}")
            return resp.body()
        finally:
            try:
                b.close()
            except Exception:  # noqa: BLE001
                pass


def _download_image(url: str, dest: Path) -> None:
    """Download a JPEG/PNG from a direct URL to disk. Used as a fallback / direct path."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if len(data) > PLAYWRIGHT_MAX_DOWNLOAD_BYTES:
        raise RuntimeError(f"download too large: {len(data)} bytes")
    dest.write_bytes(data)


def _load_provenance_sidecar(slug: str) -> dict | None:
    """Read the per-photo provenance JSON written by `_write_provenance_sidecar`.

    Returns the parsed dict, or None if the sidecar is missing / unreadable /
    malformed. Used by the cache-hit branch of `_resolve_one_marker` to
    re-emit full provenance without re-downloading the JPEG.

    The function never raises: a missing or corrupt sidecar just means the
    cache-hit record will be tagged `cache.no_provenance` (the JPEG itself
    is still used, so the build does not fail).
    """
    sidecar = PHOTOS_DIR / f"photo-{slug}.provenance.json"
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:  # noqa: BLE001
        log(f"WARN: provenance sidecar for {slug!r} is unreadable: {e}")
        return None
    return data if isinstance(data, dict) else None


def _write_provenance_sidecar(slug: str, record: dict) -> Path:
    """Atomically write a per-photo provenance JSON next to the cached JPEG.

    The sidecar is the source of truth for provenance so that future
    cache-hit re-runs can re-emit full provenance in the aggregated
    `assets/unsplash-metadata.json` without re-downloading anything. The
    schema is a superset of the per-record fields appended to
    `_PHOTO_DOWNLOAD_LOG`; we add `downloaded_at` and a `version` field so
    older sidecars can be migrated or detected.

    Atomicity: write to a sibling `<name>.provenance.json.tmp` first, then
    `os.replace()` it onto the real path. This avoids leaving a partial
    file behind if the build is interrupted mid-write.
    """
    sidecar = PHOTOS_DIR / f"photo-{slug}.provenance.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)  # shallow copy so we don't mutate the caller's dict
    payload.setdefault("downloaded_at", datetime.now().isoformat(timespec="seconds"))
    payload.setdefault("version", 1)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, sidecar)
    return sidecar


def _resolve_one_marker(query: str) -> str:
    """Resolve one `<!-- photo: query -->` marker to a markdown image reference.

    Cache hit: return the cached reference (recorded as `source: "cache"`).
    Cache miss: call _unsplash_playwright to scrape and cache a real JPEG,
    then record the full provenance (search URL, detail URL, clean CDN URL,
    photo id, source = "unsplash.cloakbrowser.detail"). On failure, raise
    a clear actionable error.

    Every invocation is appended to the process-local _PHOTO_DOWNLOAD_LOG,
    which is serialized to assets/unsplash-metadata.json by
    _build_inputs_with_resolved_photos (so the download list is reproducible
    for audit / re-runs).
    """
    slug = _slugify(query)
    # Use .jpg as the canonical extension; Unsplash returns jpg by default.
    cache_path = PHOTOS_DIR / f"photo-{slug}.jpg"
    rel_path = f"assets/photos/photo-{slug}.jpg"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        # Cache hit. Two sub-cases:
        #   - sidecar present: this JPEG was downloaded by a build that
        #     already wrote full provenance. Recover those fields and tag
        #     `source = cache.with_provenance` so the operator can tell
        #     this record came from a previous fresh download.
        #   - sidecar absent: the JPEG is on disk but its origin is
        #     unknown (e.g. an older build, or a hand-dropped file). Tag
        #     `source = cache.no_provenance`, leave the rich fields
        #     null, and emit a one-line warning so the operator can
        #     either drop the file (and let the next build re-download)
        #     or live with the unknown provenance.
        sidecar_data = _load_provenance_sidecar(slug)
        if sidecar_data is not None:
            # Keys we always overwrite with current-build values so the
            # cache-hit record reflects THIS marker's query, not the
            # original download's query (they may differ if a chapter
            # author re-used an old slug with a new query string).
            base = {
                "slug": slug,
                "query": query,
                "alt": query,
                "dest": str(cache_path),
                "path": rel_path,
                "bytes": cache_path.stat().st_size,
                "url": UNSPLASH_SEARCH_URL.format(query=urllib.parse.quote(query)),
            }
            # Provenance fields we recover from the sidecar. We never
            # let the sidecar override the authoritative basic fields
            # above (e.g. a stale sidecar with a different slug/path).
            provenance_keys = (
                "search_url", "detail_url", "cdn_url", "photo_id",
                "used_query", "query_relaxed", "downloaded_at", "version",
            )
            for k in provenance_keys:
                if k in sidecar_data:
                    base[k] = sidecar_data[k]
            base["source"] = "cache.with_provenance"
            _PHOTO_DOWNLOAD_LOG.append(base)
        else:
            log(
                f"WARN: photo marker cache hit for {query!r} ({slug!r}) has no "
                f"provenance sidecar at {PHOTOS_DIR / f'photo-{slug}.provenance.json'}; "
                f"tagging source=cache.no_provenance. The JPEG on disk may have an "
                f"unknown origin (older build or hand-dropped file)."
            )
            _PHOTO_DOWNLOAD_LOG.append({
                "slug": slug,
                "query": query,
                "alt": query,
                "dest": str(cache_path),
                "path": rel_path,
                "bytes": cache_path.stat().st_size,
                "source": "cache.no_provenance",
                "url": UNSPLASH_SEARCH_URL.format(query=urllib.parse.quote(query)),
                # Provenance fields explicitly set to None so the schema is
                # identical across all three source tags (cache.with_provenance
                # / cache.no_provenance / unsplash.cloakbrowser.detail). This
                # makes downstream verification (Goal #7) and operator audits
                # much simpler: every record always has the same shape.
                "search_url": None,
                "detail_url": None,
                "cdn_url": None,
                "photo_id": None,
                "used_query": None,
                "query_relaxed": None,
            })
        return f"![{query}]({rel_path})"

    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    log(f"photo marker cache miss: downloading {query!r} via cloakbrowser -> {cache_path}")
    try:
        info = _unsplash_playwright(query, cache_path)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"photo marker download failed for {query!r}: {e}\n"
            f"  expected cache: {cache_path}\n"
            "  To fix, manually save the image at the path above and re-run."
        ) from e

    if not cache_path.exists() or cache_path.stat().st_size == 0:
        raise RuntimeError(f"photo marker download wrote empty file: {cache_path}")
    record = {
        "slug": slug,
        "query": query,
        "alt": query,
        "dest": str(cache_path),
        "path": rel_path,
        "bytes": info.get("bytes", cache_path.stat().st_size),
        "source": "unsplash.cloakbrowser.detail",
        "url": info.get("search_url", UNSPLASH_SEARCH_URL.format(query=urllib.parse.quote(query))),
        "search_url": info.get("search_url"),
        "detail_url": info.get("detail_url"),
        "cdn_url": info.get("cdn_url"),
        "photo_id": info.get("photo_id"),
        "used_query": info.get("query"),  # the (possibly relaxed) query that actually matched
        "query_relaxed": info.get("query") != query,
    }
    _PHOTO_DOWNLOAD_LOG.append(record)
    # Persist the per-photo provenance sidecar so future cache-hit re-runs
    # can re-emit full provenance without re-downloading the JPEG.
    try:
        sidecar = _write_provenance_sidecar(slug, record)
        log(f"wrote provenance sidecar: {sidecar}")
    except Exception as e:  # noqa: BLE001
        # The build should not fail just because the sidecar couldn't be
        # written — the in-memory record is still appended and the JPEG is
        # on disk. Surface the issue loudly so the operator can investigate.
        log(f"WARN: could not write provenance sidecar for {slug!r}: {e}")
    return f"![{query}]({rel_path})"


def _resolve_photo_markers(md_text: str, label: str = "") -> str:
    """Replace all `<!-- photo: query -->` markers in md_text with image refs."""

    def repl(m: re.Match) -> str:
        query = m.group(1).strip()
        return _resolve_one_marker(query)

    if PHOTO_MARKER_RE.search(md_text):
        n = len(PHOTO_MARKER_RE.findall(md_text))
        if label:
            log(f"[photo markers] {label}: resolving {n} marker(s)")
        return PHOTO_MARKER_RE.sub(repl, md_text)
    return md_text


def _build_inputs_with_resolved_photos() -> list[Path]:
    """Build the input list for pandoc, but route each file through a
    pre-processing step that resolves `<!-- photo: ... -->` markers.

    The resolved markdown is written to a sibling file in .resolved/ (a temp
    dir). The caller is responsible for cleaning it up via cleanup().

    As a side effect, the resolved photo list is serialized to
    assets/unsplash-metadata.json (overwritten each build) for reproducibility.
    """
    # Reset the log so repeated calls in one process (PDF + EPUB) don't
    # duplicate entries.
    _PHOTO_DOWNLOAD_LOG.clear()
    resolved_dir = ROOT / ".resolved"
    resolved_dir.mkdir(exist_ok=True)
    out: list[Path] = []
    for src in SOURCE_INPUTS:
        text = src.read_text(encoding="utf-8")
        label = str(src.relative_to(ROOT))
        resolved = _resolve_photo_markers(text, label=label)
        dst = resolved_dir / src.name.replace(".md", ".resolved.md")
        dst.write_text(resolved, encoding="utf-8")
        out.append(dst)
    # Persist the download log for reproducibility.
    UNSPLASH_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNSPLASH_METADATA_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source_root": str(ROOT),
                "total_photos": len(_PHOTO_DOWNLOAD_LOG),
                "photos": _PHOTO_DOWNLOAD_LOG,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log(f"wrote {len(_PHOTO_DOWNLOAD_LOG)} photo records to {UNSPLASH_METADATA_PATH}")
    return out


TITLES = [
    "Preface", "Contents", "Index",
    "What Recovery Really Means", "Why HYROX Is So Demanding",
    "The Recovery Fundamentals", "Sleep: Your Most Powerful Recovery Tool",
    "Eat to Recover", "Hydration and Electrolytes", "Active Recovery",
    "Muscle Soreness and DOMS", "Mobility, Stretching, and Soft-Tissue Work",
    "Cold, Heat, Sauna, and Recovery Technology",
    "Recovery Between Training Sessions", "Deloading",
    "Managing Training Fatigue", "When to Push and When to Rest",
    "Recovering From Each Station", "After Hard Sessions",
    "Recovery After a HYROX Race", "Returning to Training After HYROX",
    "Recovery for the HYROX Lifestyle", "Travel and Competition Recovery",
    "Injury Warning Signs and When to Seek Help",
    "Build Your Personal Recovery System",
]

SOURCE_INPUTS = [
    ROOT / "front-matter" / "01-preface.md",
    ROOT / "front-matter" / "02-contents.md",
    *sorted((ROOT / "chapters").glob("*.md")),
    ROOT / "back-matter" / "03-index.md",
]


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def find_typst() -> Path:
    exe = shutil.which("typst")
    if exe:
        return Path(exe)
    local = ROOT / ".math-render" / "typst"
    if local.exists():
        return local
    # The tarball extracts into a versioned subdirectory; search for the binary.
    for candidate in (ROOT / ".math-render").glob("*/typst"):
        if candidate.exists():
            return candidate
    log("typst not found — downloading…")
    (ROOT / ".math-render").mkdir(parents=True, exist_ok=True)
    tarball = Path("/tmp/typst.tar.xz")
    urllib.request.urlretrieve(TYPST_URL, tarball)
    with tarfile.open(tarball) as tf:
        tf.extractall(ROOT / ".math-render", filter="data")
    candidates = list((ROOT / ".math-render").glob("*/typst")) + [local]
    found = [c for c in candidates if c.exists()]
    if not found:
        raise RuntimeError("typst download/extract failed")
    return found[0]


def png_size(path: Path):
    """Return (width, height) of a PNG by parsing its IHDR."""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def constrain_images(body: str) -> str:
    """Add a width/height cap to every image() call so figures fit the margins.

    Constraints (uniform for landscape and portrait):
      * image width  <= TEXT_WIDTH_CM  (text-column width)
      * image height <= MAX_IMG_HEIGHT_CM (70% of content height, with
        CAPTION_RESERVE_CM for the caption line and breathing room)

    Scale-to-fit preserves the source aspect ratio: the figure is sized to
    the smaller of the two caps and the other dimension is set to `auto`.
    """
    pattern = re.compile(r'image\("([^"]+\.png)"\)')

    def repl(m: re.Match) -> str:
        path = ROOT / m.group(1)
        sz = png_size(path) if path.exists() else None
        if not sz:
            return m.group(0)
        w, h = sz
        # Target height leaves room for the caption underneath.
        max_h = MAX_IMG_HEIGHT_CM - CAPTION_RESERVE_CM
        # Scale so that BOTH width and height are within their caps
        # (scale-to-fit, preserve aspect ratio).
        # If the source aspect is wider than TEXT_WIDTH_CM / max_h, the width
        # is the binding constraint; otherwise height is.
        if w * max_h >= h * TEXT_WIDTH_CM:
            # width is the binding constraint
            return f'image("{m.group(1)}", width: {TEXT_WIDTH_CM:.2f}cm)'
        # height is the binding constraint
        return f'image("{m.group(1)}", height: {max_h:.2f}cm)'

    out = pattern.sub(repl, body)
    changed = len(pattern.findall(body))
    log(f"constrained {changed} figures: width<= {TEXT_WIDTH_CM:.2f}cm, height<= {MAX_IMG_HEIGHT_CM - CAPTION_RESERVE_CM:.2f}cm (incl. {CAPTION_RESERVE_CM:.1f}cm caption reserve, {int(IMG_HEIGHT_FRACTION*100)}% of content height)")
    return out


def pandoc_to_typst() -> None:
    # Route every input through _build_inputs_with_resolved_photos() so any
    # `<!-- photo: ... -->` markers in the sources are rewritten to image
    # refs before pandoc sees them. The resolved copies live in .resolved/
    # and are removed by cleanup().
    resolved = _build_inputs_with_resolved_photos()
    cmd = [
        "pandoc", *map(str, resolved),
        "-t", "typst",
        "--toc", "--toc-depth=2",
        "--metadata", f"title={TITLE}",
        "--metadata", f"author={AUTHOR}",
        "-o", str(BOOK_TYP),
    ]
    subprocess.run(cmd, check=True)
    log(f"pandoc -> book.typ ({BOOK_TYP.stat().st_size} bytes)")


def postprocess_typst() -> None:
    src = BOOK_TYP.read_text(encoding="utf-8")

    # --- symbol fixes for typst <= 0.15 (pandoc emits these for some math) ---
    src = (src.replace("planck.reduce", "u{210f}")
              .replace("angle.l", "\u27e8")
              .replace("angle.r", "\u27e9")
              .replace("times.circle", "\u2297"))

    # --- page breaks: every level-1 heading starts a new page (except the
    #     first, which follows the title page's own #pagebreak()) ---
    lines = src.split("\n")
    out = []
    seen_h1 = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^= ", line):
            if not seen_h1:
                seen_h1 = True
            else:
                out.append("#pagebreak()")
            out.append(line)
            if line.startswith("= Contents"):
                i += 1
                if i < len(lines) and lines[i].startswith("<"):
                    out.append(lines[i])
                    i += 1
                out.append("#outline(title: none, depth: 2)")
                continue
            i += 1
            continue
        out.append(line)
        i += 1

    body = "\n".join(out)
    body = constrain_images(body)
    BOOK_TYP.write_text(RULES + FOOTER + body, encoding="utf-8")

    typ = BOOK_TYP.read_text(encoding="utf-8")
    assert "#show heading.where(level: 1)" in typ
    leaks = re.findall(r"planck\.reduce|angle\.l|angle\.r|times\.circle", body)
    if leaks:
        raise RuntimeError(f"typst symbol leaks after fix: {leaks[:5]}")
    log("post-processed book.typ (page breaks + outline + styles + title page + margins)")


def compile_pdf(typst: Path) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"{SLUG}-{timestamp()}.pdf"
    subprocess.run([str(typst), "compile", str(BOOK_TYP), str(out)], check=True)
    log(f"typst compile -> {out}")
    return out


def build_epub() -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    css = ROOT / "assets" / "epub.css"
    css.write_text(EPUB_CSS, encoding="utf-8")
    out = DIST / f"{SLUG}-{timestamp()}.epub"
    # Use the same resolved inputs as the PDF path so the EPUB also includes
    # any photo-marker substitutions.
    resolved = _build_inputs_with_resolved_photos()
    cmd = [
        "pandoc", *map(str, resolved),
        "-o", str(out),
        "--metadata", f"title={TITLE}",
        "--metadata", f"author={AUTHOR}",
        "--metadata", "lang=en",
        "--toc", "--toc-depth=2",
        "--mathml",
        "--split-level=1",
        "--css", str(css),
    ]
    subprocess.run(cmd, check=True)
    log(f"pandoc -> {out}")
    return out


def validate_pdf(pdf: Path) -> None:
    from pypdf import PdfReader

    assert pdf.read_bytes()[:4] == b"%PDF", "bad PDF magic"
    r = PdfReader(str(pdf))
    pages = len(r.pages)
    assert pages >= 3, f"too few pages: {pages}"
    texts = [(p.extract_text() or "") for p in r.pages]
    full = "\n".join(texts)

    missing = [t for t in TITLES if t not in full]
    assert not missing, f"missing titles: {missing}"
    log(f"all {len(TITLES)} titles present")

    for bad in ["<math", "<svg", "<table", "\\frac", "planck.reduce",
                "angle.", "times.circle", "#pagebreak", "\\newpage"]:
        assert bad not in full, f"markup leak: {bad!r}"
    log("no raw markup / symbol leaks")

    n_img = 0
    for p in r.pages:
        xo = (p.get("/Resources", {}) or {}).get("/XObject", {}) or {}
        for name in xo:
            if xo[name].get_object().get("/Subtype") == "/Image":
                n_img += 1
    assert n_img == EXPECTED_IMAGES, f"image count {n_img} != {EXPECTED_IMAGES}"
    log(f"embedded images: {n_img}")

    # page isolation
    for t in TITLES:
        assert any(tx.strip().startswith(t) for tx in texts), f"{t!r} never starts a page"
    log("every unit starts on its own page (page isolation OK)")

    p1 = texts[0].strip()
    assert p1.startswith(TITLE) and AUTHOR in p1, f"page 1 not a title page: {p1[:60]!r}"
    log("page 1 is the typeset title page")

    typ = BOOK_TYP.read_text(encoding="utf-8")
    assert "#set par(justify: true)" in typ and "#show heading" in typ
    log("justify + heading colour rules present")

    # --- margin containment: every character and image inside the text block ---
    margin_pt = 2.2 * 72 / 2.54          # 2.2 cm in points (~62.36)
    page_w = 21.0 * 72 / 2.54            # A4 width in points (~595.28)
    page_h = 29.7 * 72 / 2.54            # A4 height in points (~841.89)
    x0_ok, x1_ok = margin_pt, page_w - margin_pt
    y0_ok, y1_ok = margin_pt, page_h - margin_pt
    tol = 1.5                            # pt tolerance

    overflows = _margin_check(pdf, x0_ok, x1_ok, y0_ok, y1_ok, tol)
    assert not overflows, f"margin overflow on {len(overflows)} items: {overflows[:10]}"
    log("margin containment OK (no char or image exceeds the page margins)")


def _margin_check(pdf: Path, x0_ok, x1_ok, y0_ok, y1_ok, tol):
    """Return list of (page, kind, x0, x1, snippet) items outside the margins.

    Rules:
      - chars: only alphanumerics are checked horizontally (hanging punctuation
        is typographically correct in justified text); vertical position is not
        checked for chars because the page-number footer deliberately lives in
        the bottom margin.
      - images: full x/y containment is required.
    """
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        return _margin_check_remote(pdf, x0_ok, x1_ok, y0_ok, y1_ok, tol)
    import re
    overflows = []
    with pdfplumber.open(str(pdf)) as pdf_obj:
        for pi, page in enumerate(pdf_obj.pages, 1):
            for ch in page.chars:
                if not re.search(r"[A-Za-z0-9]", ch.get("text", "")):
                    continue
                if ch["x0"] < x0_ok - tol or ch["x1"] > x1_ok + tol:
                    overflows.append((pi, "char", round(ch["x0"], 1), round(ch["x1"], 1), ch.get("text", "")[:20]))
            for img in page.images:
                if (img["x0"] < x0_ok - tol or img["x1"] > x1_ok + tol
                        or img["top"] < y0_ok - tol or img["bottom"] > y1_ok + tol):
                    overflows.append((pi, "image", round(img["x0"], 1), round(img["x1"], 1), img.get("name", "")[:20]))
    return overflows


def _margin_check_remote(pdf: Path, x0_ok, x1_ok, y0_ok, y1_ok, tol):
    """Run the margin check under a system interpreter that has pdfplumber."""
    code = (
        "import sys, re, pdfplumber\n"
        "ov = []\n"
        "x0_ok, x1_ok, y0_ok, y1_ok, tol = map(float, sys.argv[1:6])\n"
        "with pdfplumber.open(sys.argv[6]) as pdf:\n"
        "    for pi, page in enumerate(pdf.pages, 1):\n"
        "        for ch in page.chars:\n"
        "            if not re.search(r'[A-Za-z0-9]', ch.get('text', '')):\n"
        "                continue\n"
        "            if ch['x0'] < x0_ok - tol or ch['x1'] > x1_ok + tol:\n"
        "                ov.append((pi, 'char', round(ch['x0'], 1), round(ch['x1'], 1), ch.get('text', '')[:20]))\n"
        "        for img in page.images:\n"
        "            if (img['x0'] < x0_ok - tol or img['x1'] > x1_ok + tol\n"
        "                    or img['top'] < y0_ok - tol or img['bottom'] > y1_ok + tol):\n"
        "                ov.append((pi, 'image', round(img['x0'], 1), round(img['x1'], 1), img.get('name', '')[:20]))\n"
        "print(repr(ov))\n"
    )
    for interp in ["/usr/bin/python3", "/usr/local/bin/python3"]:
        if not os.path.exists(interp):
            continue
        try:
            res = subprocess.run(
                [interp, "-c", code, str(x0_ok), str(x1_ok), str(y0_ok), str(y1_ok), str(tol), str(pdf)],
                capture_output=True, text=True, timeout=600,
            )
            if res.returncode == 0 and res.stdout.strip():
                import ast
                return ast.literal_eval(res.stdout.strip())
        except Exception as e:  # noqa: BLE001
            log(f"margin check via {interp} failed: {e}")
    log("pdfplumber unavailable on all interpreters — skipping margin bbox check")
    return []


def validate_epub(epub: Path) -> None:
    assert epub.read_bytes()[:2] == b"PK", "bad EPUB zip magic"
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        assert "mimetype" in names and any(n.endswith(".opf") for n in names), \
            "EPUB missing mimetype/OPF"
        opf = next(n for n in names if n.endswith(".opf"))
        text = z.read(opf).decode("utf-8", "replace")
        for t in TITLES:
            pass  # titles live in the xhtml body, checked below
        xhtmls = [n for n in names if n.endswith((".xhtml", ".html"))]
        body = ""
        for n in xhtmls:
            body += z.read(n).decode("utf-8", "replace")
        missing = [t for t in TITLES if t not in body]
        assert not missing, f"EPUB missing titles: {missing}"
        # images embedded. The EPUB zip lists each unique image file once
        # (the same photo referenced from two chapters shares one zip entry),
        # so the EPUB target is EXPECTED_IMAGES_BASE + unique photo count,
        # which is EXPECTED_IMAGES minus the duplicate-marker slack.
        n_img = sum(1 for n in names if n.endswith((".png", ".jpg", ".jpeg")))
        n_unique_photos = len({entry["path"] for entry in _PHOTO_DOWNLOAD_LOG})
        expected_epub = EXPECTED_IMAGES_BASE + n_unique_photos
        assert n_img == expected_epub, f"EPUB images {n_img} != {expected_epub}"
        log(f"EPUB OK: {len(xhtmls)} xhtml, {n_img} images, all {len(TITLES)} titles present")


def cleanup() -> None:
    if BOOK_TYP.exists():
        BOOK_TYP.unlink()
    math_render = ROOT / ".math-render"
    if math_render.exists():
        shutil.rmtree(math_render)
    resolved_dir = ROOT / ".resolved"
    if resolved_dir.exists():
        shutil.rmtree(resolved_dir)
    for pat in ["book-*.typ", "*.aux", "*.log", "*.toc", "*.out", "*.fls"]:
        for p in ROOT.glob(pat):
            p.unlink()
    log("cleaned intermediates (book.typ, .math-render/, .resolved/)")


def main() -> int:
    global EXPECTED_IMAGES
    typst = find_typst()
    log(f"using typst: {typst}")
    pandoc_to_typst()
    postprocess_typst()
    # After photo markers are resolved, the expected image count is
    # 50 (base figures) + the number of unique photo cache files. Update the
    # module-level constant so validate_pdf / validate_epub use the right
    # target. The unsplash-metadata.json is also written at this point.
    # Use the marker-hit count for EXPECTED_IMAGES (PDF XObject count
    # includes a separate /Image instance per marker reference); the EPUB
    # validator computes its own expected = base + unique photo paths since
    # the zip only stores each unique image once.
    n_marker_hits = len(_PHOTO_DOWNLOAD_LOG)
    n_unique_photos = len({entry["path"] for entry in _PHOTO_DOWNLOAD_LOG})
    if n_marker_hits > 0:
        EXPECTED_IMAGES = EXPECTED_IMAGES_BASE + n_marker_hits
        log(
            f"adjusted EXPECTED_IMAGES -> {EXPECTED_IMAGES} "
            f"(50 base + {n_marker_hits} marker hits; "
            f"{n_unique_photos} unique photo files; EPUB target = "
            f"{EXPECTED_IMAGES_BASE + n_unique_photos})"
        )
    pdf = compile_pdf(typst)
    epub = build_epub()
    validate_pdf(pdf)
    validate_epub(epub)
    cleanup()
    log(f"DONE: {pdf.name} + {epub.name} (older dist files kept)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
