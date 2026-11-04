"""Turn text into vectors for the FAQ search. Free, local, no API key.

Choice: `fastembed` (ONNX runtime). It needs no PyTorch and no key, ships Windows wheels, and downloads one
small model file the first time it is used. The model id comes from `EMBEDDING_MODEL` in `.env`.
The model must produce `EMBEDDING_DIM` numbers per text, because the pgvector column is `vector(384)`
(`supabase/academy_faq_rag.sql`). Any 384-number model works with the same table.

The same embedder runs in two places: the laptop-only loader (embeds the FAQ chunks) and the agent
(embeds the parent's question). Both must use the same model, or the search compares unrelated vectors.

There is no retry here: nothing is rate-limited, the model runs on this computer. (The one network step,
the first model download, fails with a clear error and can simply be run again.)
"""

import os
import threading
from contextlib import contextmanager
from typing import Iterator, Protocol, Sequence

from email_assistant.academy import settings

EMBEDDING_DIM = 384  # keep equal to vector(384) in supabase/academy_faq_rag.sql


class EmbeddingError(RuntimeError):
    """The embedding model could not be loaded or gave the wrong shape. The message says what to do."""


class Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """Local ONNX embeddings through `fastembed`. The model is loaded on first use."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from fastembed import TextEmbedding

                    cache = os.environ.get(settings.EMBEDDING_CACHE_VAR, "").strip() or None
                    self._model = TextEmbedding(model_name=self.model_name, cache_dir=cache)
                except ValueError as exc:  # fastembed: model name not in its supported list
                    raise EmbeddingError(
                        f"{settings.EMBEDDING_MODEL_VAR}={self.model_name!r} is not a model fastembed supports. "
                        "List them with: uv run python -c \"from fastembed import TextEmbedding as T; "
                        "print([m['model'] for m in T.list_supported_models()])\""
                    ) from exc
                except Exception as exc:  # noqa: BLE001 - usually the first download failing
                    raise EmbeddingError(
                        f"Could not load the embedding model {self.model_name!r} ({type(exc).__name__}). "
                        "The first use downloads it from the internet; check the connection and run again."
                    ) from exc
        return self._model

    def embed_query(self, text: str) -> list[float]:
        return _floats(next(iter(self._load().query_embed([text]))))

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_floats(v) for v in self._load().passage_embed(list(texts))]


def _floats(vector) -> list[float]:
    return [float(x) for x in vector]


def check_dimension(vector: Sequence[float]) -> None:
    """Raise if a vector does not have the length the database column expects."""
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"The embedding model gives {len(vector)} numbers per text, but the database column holds {EMBEDDING_DIM}. "
            f"Choose a {EMBEDDING_DIM}-dimension model in {settings.EMBEDDING_MODEL_VAR}, or change the SQL and EMBEDDING_DIM together."
        )


_override: Embedder | None = None
_cached: Embedder | None = None
_lock = threading.Lock()


def get_embedder() -> Embedder:
    """The embedder to use: a test's if one is set, else the one named by EMBEDDING_MODEL (built once)."""
    global _cached
    if _override is not None:
        return _override
    with _lock:
        name = settings.require_env(settings.EMBEDDING_MODEL_VAR)
        if _cached is None or getattr(_cached, "model_name", name) != name:
            _cached = FastEmbedEmbedder(name)
        return _cached


@contextmanager
def use_embedder(embedder: Embedder) -> Iterator[Embedder]:
    """Temporarily use another embedder (tests). A plain module variable, so MCP worker threads see it too."""
    global _override
    previous, _override = _override, embedder
    try:
        yield embedder
    finally:
        _override = previous
