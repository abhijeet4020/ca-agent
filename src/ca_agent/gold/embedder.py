"""Purpose: turns chunk text into vectors, behind a protocol so the rest of Gold never depends
on which model produced them (SPEC-01 req 2). The backend is injected for the same reason the
vision client's transport is: sentence-transformers pulls in torch, and a test suite that has
to load a 2 GB model to check that vectors line up with their chunks is a test suite nobody
runs. The protocol also carries the model name and dimension, because req 2 requires both to
be persisted with the index - without them a stored index cannot be safely reloaded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    """The embedding backend could not produce vectors. Never silently degraded."""


@runtime_checkable
class Embedder(Protocol):
    """What Gold needs from an embedding model, and nothing more."""

    @property
    def model_name(self) -> str:
        """Identifier persisted with the index so a reload can verify compatibility."""

    @property
    def dimension(self) -> int:
        """Vector width, required by req 2 and by FAISS at index construction."""

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input in the same order."""


class SentenceTransformerEmbedder:
    """Local sentence-transformers backend, the provider the approved plan selected.

    The model is loaded lazily on first use: constructing this object must stay cheap so the
    CLI can report configuration without paying a model load, and so a dry run never does.
    """

    def __init__(self, model_name: str, *, normalise: bool = True) -> None:
        self._model_name = model_name
        self._normalise = normalise
        self._model = None
        self._dimension: int | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        self._load()
        if self._dimension is None:
            raise EmbeddingError(f"{self._model_name} did not report a vector dimension")
        return self._dimension

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        try:
            vectors = self._model.encode(  # type: ignore[union-attr]
                list(texts),
                normalize_embeddings=self._normalise,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except (RuntimeError, ValueError, OSError) as error:
            raise EmbeddingError(f"{self._model_name} failed to encode a batch: {error}") from error
        return [[float(value) for value in vector] for vector in vectors]

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise EmbeddingError(
                "sentence-transformers is not installed; install the gold extra to build indexes"
            ) from error
        try:
            self._model = SentenceTransformer(self._model_name)
            self._dimension = int(self._model.get_sentence_embedding_dimension())
        except (OSError, ValueError, RuntimeError) as error:
            raise EmbeddingError(f"could not load {self._model_name}: {error}") from error
