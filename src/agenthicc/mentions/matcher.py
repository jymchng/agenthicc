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

Within a tier results are sorted alphabetically by their casefolded display
string, making ranking deterministic (identical input → identical ordering).
"""
from __future__ import annotations

from pathlib import PurePosixPath
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
]

RANK_EXACT           = 0
RANK_FILENAME_PREFIX = 1
RANK_SEGMENT_PREFIX  = 2
RANK_FILENAME_SUBSTR = 3
RANK_PATH_SUBSTR     = 4
RANK_FUZZY           = 5


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
        return RANK_FILENAME_PREFIX   # empty query matches everything as prefix

    q = query.casefold()

    # Strip trailing "/" (directory marker) for path parsing
    clean = display.rstrip("/")
    name  = PurePosixPath(clean).name.casefold()   # filename segment only
    full  = clean.casefold()                        # full normalised path

    # Tier 0 — exact match (filename or full path)
    if name == q or full == q:
        return RANK_EXACT

    # Tier 1 — filename prefix
    if name.startswith(q):
        return RANK_FILENAME_PREFIX

    # Tier 2 — any path segment starts with query
    parts = [p.casefold() for p in PurePosixPath(clean).parts]
    if any(p.startswith(q) for p in parts if p != name):
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


def filter_and_rank(query: str, items: list[MatchItem]) -> list[MatchItem]:
    """Filter *items* case-insensitively and return them ranked by match quality.

    Parameters
    ----------
    query:
        The fragment typed by the user.
    items:
        Candidate ``MatchItem`` objects.  ``item.display`` is used for
        matching; ``item.value`` and ``item.display`` are returned unchanged
        so original casing is always preserved.

    Returns
    -------
    list[MatchItem]
        Filtered and sorted items.  Empty list when nothing matches.
    """
    if not query:
        return list(items)

    scored: list[tuple[int, str, MatchItem]] = []
    for item in items:
        rank = rank_match(query, item.display)
        if rank is not None:
            scored.append((rank, item.display.casefold(), item))

    scored.sort(key=lambda x: (x[0], x[1]))
    return [item for _, _, item in scored]


# ── internal helpers ─────────────────────────────────────────────────────────

def _fuzzy_match(query: str, text: str) -> bool:
    """Return True when every character of *query* appears in *text* in order."""
    it = iter(text)
    return all(ch in it for ch in query)
