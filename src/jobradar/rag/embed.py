"""Embedding backends.

* `FastEmbedder` — local ONNX model via fastembed (default `BAAI/bge-small-en-v1.5`, 384-d).
  Text never leaves the machine. The model (~130 MB) downloads on first use.
* `HashEmbedder` — deterministic hashing-trick vectors for tests and the offline demo.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    # Cosine score below which a hit is treated as "no evidence" (model-specific scale).
    min_relevance: float

    @property
    def model_id(self) -> str: ...  # name@version, stored next to every vector

    def embed(self, texts: list[str]) -> np.ndarray: ...  # (n, d) float32, L2-normalized


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


class HashEmbedder:
    min_relevance = 0.1

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    @property
    def model_id(self) -> str:
        return f"hash-bow@{self.dim}"

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            tokens = re.findall(r"[a-z0-9+#.]+", t.lower())
            grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
                out[i, h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return _normalize(out)


class FastEmbedder:
    # bge-small scores unrelated English text around 0.4-0.45; relevant text ~0.55+.
    min_relevance = 0.5

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5",
                 cache_dir: str | None = None) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None

    @property
    def model_id(self) -> str:
        import fastembed

        return f"fastembed:{self.model_name}@{fastembed.__version__}"

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            # The hf_xet download backend fails in some sandboxed/locked cache dirs.
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        vecs = np.array(list(self._model.embed(texts)), dtype=np.float32)
        return _normalize(vecs)


def make_embedder(backend: str, model_name: str, cache_dir: str | None = None) -> Embedder:
    if backend == "hash":
        return HashEmbedder()
    return FastEmbedder(model_name, cache_dir)
