"""AtMentionTrigger — file/directory mention handler for the '@' character.

Migrated from the inline ``_get_matches`` logic in ``mention_input.py``
(PRD-39).  Behaviour is identical to the original implementation: directories
are listed before files, hidden entries are skipped, and matching directories
also expose their immediate children inline.
"""
from __future__ import annotations

from pathlib import Path

from agenthicc.tui.trigger import MatchItem, TriggerContext


class AtMentionTrigger:
    """File/directory mention trigger for the '@' character."""

    char = "@"

    # ------------------------------------------------------------------
    # Internal helpers (mirrors the original _get_matches / _iter_dir)
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
        subdirectory (classic prefix-filter).  Without "/", matching top-level
        entries are listed; matching directories also expand their immediate
        children inline so the user can navigate without typing the separator.

        Hidden entries (names starting with '.') are always skipped.
        Directory display paths carry a trailing "/" suffix.
        """
        cwd = ctx.cwd

        # ── navigating into a subdirectory ──────────────────────────────────
        if "/" in fragment:
            dir_part, file_prefix = fragment.rsplit("/", 1)
            search_dir = cwd / dir_part
            if not search_dir.is_dir():
                return []
            results: list[MatchItem] = []
            for entry in self._iter_dir(search_dir):
                if entry.name.startswith("."):
                    continue
                if not entry.name.startswith(file_prefix):
                    continue
                suffix = "/" if entry.is_dir() else ""
                display_path = f"{dir_part}/{entry.name}{suffix}"
                results.append(MatchItem(display=display_path, value=display_path))
            return results

        # ── top-level search: match + expand matching directories ────────────
        if not cwd.is_dir():
            return []

        results = []
        for entry in self._iter_dir(cwd):
            if entry.name.startswith("."):
                continue
            if not entry.name.startswith(fragment):
                continue
            suffix = "/" if entry.is_dir() else ""
            display_path = f"{entry.name}{suffix}"
            results.append(MatchItem(display=display_path, value=display_path))
            if entry.is_dir():
                # Also list immediate children of matching directories.
                for child in self._iter_dir(entry):
                    if child.name.startswith("."):
                        continue
                    child_suffix = "/" if child.is_dir() else ""
                    child_path = f"{entry.name}/{child.name}{child_suffix}"
                    results.append(MatchItem(display=child_path, value=child_path))
        return results

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> list[str]:
        """Insert the selected path (with leading '@') into *buf*.

        When there are no matches (*item* is None) the literal ``@fragment``
        text is restored unchanged.
        """
        if item is None:
            return buf + ["@"] + list(fragment)
        return buf + list("@" + item.value)

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        """Restore the literal ``@fragment`` text into *buf* on ESC."""
        return buf + ["@"] + list(fragment)

    def get_hint(self, item: MatchItem | None) -> str | None:
        """No hint for file mentions."""
        return None
