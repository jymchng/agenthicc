"""AtMentionTrigger — file/directory mention handler for the '@' character.

Migrated from the inline ``_get_matches`` logic in ``mention_input.py``
(PRD-39).  Updated in PRD-109 to use the centralised case-insensitive
matching engine (``mentions.matcher``).

Matching behaviour
------------------
* Case-insensitive prefix, substring, and fuzzy matching via
  ``mentions.matcher.filter_and_rank()``.
* Path-segment matching: ``@read`` matches ``docs/README.md`` because
  ``README.md`` is a path segment.
* Ranking: exact → filename prefix → path-segment prefix → filename
  substring → path substring → fuzzy.
* Display and insertion always use the original filesystem casing.
* Hidden entries (names starting with ``'.'``) are always skipped.
* Directory display paths carry a trailing ``"/"`` suffix.
"""

from __future__ import annotations

from pathlib import Path

from agenthicc.mentions.matcher import filter_and_rank
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerResult


class AtMentionTrigger(TriggerHandlerBase):
    """File/directory mention trigger for the '@' character."""

    char = "@"
    label = "Mention File"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_dir(path: Path) -> list[Path]:
        """Return entries of *path* sorted dirs-first, files-second, by name."""
        try:
            return sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return []

    # ------------------------------------------------------------------
    # TriggerHandler protocol
    # ------------------------------------------------------------------

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        """Return filesystem matches for *fragment* relative to *ctx.cwd*.

        When *fragment* contains "/" the search is scoped to the indicated
        subdirectory.  Without "/", both top-level entries **and** their
        immediate children are added to the candidate pool before filtering,
        enabling path-segment matching (FR-4: ``@read`` matches
        ``docs/README.md``).

        Filtering is fully case-insensitive and supports prefix, substring,
        and fuzzy matching via ``mentions.matcher.filter_and_rank()``.
        """
        cwd = ctx.cwd

        # ── navigating into a subdirectory ──────────────────────────────────
        if "/" in fragment:
            dir_part, file_prefix = fragment.rsplit("/", 1)
            search_dir = cwd / dir_part
            if not search_dir.is_dir():
                return []
            # Subdirectory navigation uses strict case-insensitive PREFIX matching
            # only — NOT substring or fuzzy.  The user has already chosen the
            # directory; the suffix is the start of the filename they want.
            # Using filter_and_rank here would make "." match every file that has
            # an extension (substring hit), so "@docs/." would populate the picker
            # with all docs instead of returning no results.
            prefix_cf = file_prefix.casefold()
            results: list[MatchItem] = []
            for entry in self._iter_dir(search_dir):
                if entry.name.startswith("."):
                    continue
                if prefix_cf and not entry.name.casefold().startswith(prefix_cf):
                    continue
                suffix = "/" if entry.is_dir() else ""
                display_path = f"{dir_part}/{entry.name}{suffix}"
                results.append(MatchItem(display=display_path, value=display_path))
            return results

        # ── top-level search: build candidate pool then rank ─────────────────
        if not cwd.is_dir():
            return []

        candidates = []
        for entry in self._iter_dir(cwd):
            if entry.name.startswith("."):
                continue
            suffix = "/" if entry.is_dir() else ""
            display_path = f"{entry.name}{suffix}"
            candidates.append(MatchItem(display=display_path, value=display_path))
            # Always include immediate children so path-segment matching works:
            # e.g. @read → docs/README.md even when "docs" doesn't start with "read".
            if entry.is_dir():
                for child in self._iter_dir(entry):
                    if child.name.startswith("."):
                        continue
                    child_suffix = "/" if child.is_dir() else ""
                    child_path = f"{entry.name}/{child.name}{child_suffix}"
                    candidates.append(MatchItem(display=child_path, value=child_path))

        return filter_and_rank(fragment, candidates)

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> TriggerResult:
        """Insert the selected path (with leading '@') into *buf*."""
        if item is None:
            return TriggerResult(buffer=buf + ["@"] + list(fragment))
        return TriggerResult(buffer=buf + list("@" + item.value))

    def can_activate(self, buf: list[str]) -> bool:
        # Activate at position 0 or immediately after whitespace.
        # Prevents '@' mid-word (e.g. in an email address) from opening the picker.
        return not buf or buf[-1].isspace()

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        """Restore the literal ``@fragment`` text into *buf* on ESC."""
        return buf + ["@"] + list(fragment)

    # get_hint → inherited from TriggerHandlerBase (returns None)
