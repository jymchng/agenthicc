"""Case-insensitive mention matching engine (PRD-109).

All mention providers — files, directories, skills, future resources — use
``filter_and_rank()`` as the single matching implementation.  No provider
implements its own string comparison.

Ranking tiers (lower = better)
-------------------------------
0  RANK_EXACT            — casefolded query == casefolded filename or full path
1  RANK_FILENAME_PREFIX  — filename segment starts with casefolded query
2  RANK_SEGMENT_PREFIX   — any intermediate path segment starts with query
3  RANK_FILENAME_SUBSTR  — query appears anywhere in the filename
4  RANK_PATH_SUBSTR      — query appears anywhere in the full path
5  RANK_FUZZY            — all query characters appear in the filename in order

Within a tier, *boosted* values (recently mentioned or changed files) come
first; then results are sorted alphabetically by their casefolded label,
making ranking deterministic (identical input → identical ordering).

:func:`match_positions` returns the characters a query matched, for the
pickers' highlight (TUX-05).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.trigger import MatchItem

__all__ = [
    "RANK_EXACT",
    "RANK_FILENAME_PREFIX",
    "RANK_SEGMENT_PREFIX",
    "RANK_FILENAME_SUBSTR",
    "RANK_PATH_SUBSTR",
    "RANK_FUZZY",
    "rank_match",
    "filter_and_rank",
    "fuzzy_positions",
    "match_positions",
]

RANK_EXACT = 0
RANK_FILENAME_PREFIX = 1
RANK_SEGMENT_PREFIX = 2
RANK_FILENAME_SUBSTR = 3
RANK_PATH_SUBSTR = 4
RANK_FUZZY = 5


def rank_match(query: str, display: str) -> int | None:
    """Return the best match rank for *query* against *display*, or ``None``.

    Parameters
    ----------
    query:
        The fragment typed by the user (e.g. ``"read"``).  Must NOT be
        pre-casefolded — this function handles normalisation.
    display:
        The candidate display string (e.g. ``"docs/README.md"`` or
        ``"README.md"``).  Trailing ``"/"`` on directories is handled
        transparently.

    Returns
    -------
    int | None
        One of the ``RANK_*`` constants, or ``None`` when *display* does not
        match *query* under any tier.
    """
    if not query:
        return RANK_FILENAME_PREFIX  # empty query matches everything as prefix

    q = query.casefold()

    # Strip trailing "/" (directory marker) for path parsing. Plain string
    # operations: this runs once per candidate on every keystroke, and a
    # project can have tens of thousands of candidates.
    clean = display.rstrip("/")
    full = clean.casefold()  # full normalised path
    name = full.rpartition("/")[2]  # filename segment only

    # Tier 0 — exact match (filename or full path)
    if name == q or full == q:
        return RANK_EXACT

    # Tier 1 — filename prefix
    if name.startswith(q):
        return RANK_FILENAME_PREFIX

    # Tier 2 — any path segment starts with query
    if any(p.startswith(q) for p in full.split("/") if p != name):
        return RANK_SEGMENT_PREFIX

    # Tier 3 — query is a substring of the filename
    if q in name:
        return RANK_FILENAME_SUBSTR

    # Tier 4 — query is a substring of the full path
    if q in full:
        return RANK_PATH_SUBSTR

    # Tier 5 — fuzzy: all query characters appear in the filename in order
    if _fuzzy_match(q, name):
        return RANK_FUZZY

    return None


def filter_and_rank(
    query: str,
    items: list[MatchItem],
    *,
    boosted: Collection[str] = (),
) -> list[MatchItem]:
    """Filter *items* case-insensitively and return them ranked by match quality.

    Parameters
    ----------
    query:
        The fragment typed by the user.
    items:
        Candidate ``MatchItem`` objects.  ``item.label`` is used for
        matching; items are returned unchanged so original casing is always
        preserved.
    boosted:
        ``item.value`` strings ranked first within their tier (and, for an
        empty query, before every other item).

    Returns
    -------
    list[MatchItem]
        Filtered and sorted items.  Empty list when nothing matches.
    """
    preferred = set(boosted)
    if not query:
        return sorted(items, key=lambda item: item.value not in preferred)

    scored: list[tuple[int, bool, str, MatchItem]] = []
    for item in items:
        rank = rank_match(query, item.label)
        if rank is not None:
            scored.append((rank, item.value not in preferred, item.label.casefold(), item))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [item for _, _, _, item in scored]


def fuzzy_positions(query: str, text: str) -> tuple[int, ...] | None:
    """Indices of *text* that spell *query* in order (case-insensitive), or ``None``."""
    positions: list[int] = []
    lowered = text.casefold()
    start = 0
    for ch in query.casefold():
        index = lowered.find(ch, start)
        if index < 0:
            return None
        positions.append(index)
        start = index + 1
    return tuple(positions)


def match_positions(query: str, text: str) -> tuple[int, ...]:
    """The characters of *text* a picker highlights for *query*.

    A substring match highlights the substring (preferring the last path
    segment, then a word start); otherwise the in-order fuzzy characters.
    Nothing matches → ``()``.
    """
    if not query or not text:
        return ()
    q = query.casefold()
    lowered = text.casefold()
    candidates = [i for i in range(len(lowered)) if lowered.startswith(q, i)]
    if candidates:
        segment = lowered.rstrip("/").rfind("/") + 1
        word_starts = [i for i in candidates if i == 0 or not lowered[i - 1].isalnum()]
        in_name = [i for i in candidates if i >= segment]
        start = (in_name or word_starts or candidates)[0]
        return tuple(range(start, start + len(q)))
    return fuzzy_positions(query, text) or ()


# ── internal helpers ─────────────────────────────────────────────────────────


def _fuzzy_match(query: str, text: str) -> bool:
    """Return True when every character of *query* appears in *text* in order."""
    it = iter(text)
    return all(ch in it for ch in query)
