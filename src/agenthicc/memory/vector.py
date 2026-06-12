"""SemanticIndex — similarity search for session/project memory (PRD-05 G-03).

Wraps ``lauren_ai._memory._vector.InMemoryVectorStore`` (TF-IDF cosine
similarity, no external services) when ``lauren_ai`` is importable.  Its API
is a clean async ``upsert(content, id=..., embedding=...)`` /
``search(query, k=...)`` pair, which maps directly onto this class.

When ``lauren_ai`` is unavailable the index falls back to a minimal built-in
bag-of-words cosine-similarity store, keeping this module dependency-free.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = ["SemanticIndex"]

try:  # pragma: no cover - exercised implicitly depending on environment
    from lauren_ai._memory._vector import InMemoryVectorStore as _LaurenStore
except ImportError:  # pragma: no cover
    _LaurenStore = None

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _bag_of_words(text: str) -> dict[str, float]:
    """Trivial normalised bag-of-words embedding (hash-free sparse dict)."""
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return {}
    counts: dict[str, float] = {}
    for tok in tokens:
        counts[tok] = counts.get(tok, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in counts.values()))
    return {tok: v / norm for tok, v in counts.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(tok, 0.0) for tok, v in a.items())


class _FallbackStore:
    """Minimal cosine-similarity store used when lauren_ai is absent."""

    def __init__(self) -> None:
        # doc_id -> (text, sparse vector)
        self._docs: dict[str, tuple[str, dict[str, float]]] = {}

    async def upsert(
        self,
        content: str,
        *,
        id: str,
        metadata: dict[str, Any] | None = None,  # noqa: ARG002
        embedding: list[float] | None = None,
    ) -> str:
        if embedding is not None:
            norm = math.sqrt(sum(v * v for v in embedding)) or 1.0
            vec = {str(i): v / norm for i, v in enumerate(embedding) if v}
        else:
            vec = _bag_of_words(content)
        self._docs[id] = (content, vec)
        return id

    async def search(self, query: str, *, k: int = 5) -> list[tuple[str, float]]:
        qvec = _bag_of_words(query)
        scored = [
            (doc_id, _cosine(qvec, vec)) for doc_id, (_, vec) in self._docs.items()
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def __len__(self) -> int:
        return len(self._docs)


class SemanticIndex:
    """Similarity index over short text documents.

    :Example:

    .. code-block:: python

        index = SemanticIndex()
        await index.add("doc-1", "pytest failures in the auth module")
        results = await index.search("auth test failures", top_k=3)
        # -> [("doc-1", 0.83), ...]
    """

    def __init__(self) -> None:
        self._store = _LaurenStore() if _LaurenStore is not None else _FallbackStore()

    async def add(
        self,
        doc_id: str,
        text: str,
        embedding: list[float] | None = None,
    ) -> str:
        """Index ``text`` under ``doc_id``.  Re-adding the same id replaces it."""
        return await self._store.upsert(text, id=doc_id, embedding=embedding)

    async def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return up to ``top_k`` ``(doc_id, score)`` pairs, best first."""
        results = await self._store.search(query, k=top_k)
        if isinstance(self._store, _FallbackStore):
            return results  # already (doc_id, score) pairs
        return [(r.id, float(r.score)) for r in results]

    def __len__(self) -> int:
        return len(self._store)
