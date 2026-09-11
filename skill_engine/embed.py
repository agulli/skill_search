"""Dense embedding backends for semantic recall and hybrid search."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import struct
from typing import Protocol

log = logging.getLogger("skill_engine.embed")

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]{1,}")


class Embedder(Protocol):
    """Protocol interface for embedding models."""

    model: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def pack(vec: list[float]) -> bytes:
    """Packs a floating-point vector into binary format."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    """Unpacks binary data into a list of floats."""
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Computes cosine similarity between two float vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Float cosine similarity between -1.0 and 1.0.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


class HashingEmbedder:
    """Deterministic hashing-based bag-of-words vectorizer for testing and pipelines."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.model = f"hashing-{dim}"

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            counts: dict[str, int] = {}
            for tok in TOKEN_RE.findall(text.lower()):
                counts[tok] = counts.get(tok, 0) + 1
            for tok, count in counts.items():
                idx = int.from_bytes(
                    hashlib.blake2b(tok.encode(), digest_size=4).digest(), "little"
                ) % self.dim
                sign = 1.0 if idx % 2 == 0 else -1.0
                vec[idx] += sign * (1.0 + math.log(count))
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class VoyageEmbedder:
    """Voyage AI cloud embedding backend."""

    def __init__(self, model: str = "voyage-3.5", api_key: str | None = None) -> None:
        import voyageai  # Optional dependency

        self.client = voyageai.Client(api_key=api_key or os.getenv("VOYAGE_API_KEY"))
        self.model = model
        self.dim = 1024

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 128):
            chunk = texts[i : i + 128]
            resp = self.client.embed(chunk, model=self.model, input_type="document")
            vectors.extend(resp.embeddings)
        if vectors:
            self.dim = len(vectors[0])
        return vectors

    def encode_query(self, text: str) -> list[float]:
        resp = self.client.embed([text], model=self.model, input_type="query")
        return resp.embeddings[0]


class LocalEmbedder:
    """Local CPU sentence-transformers embedding backend."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # Optional dependency

        self.st = SentenceTransformer(model)
        self.model = model
        self.dim = self.st.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.st.encode(texts, normalize_embeddings=True)]


def build(name: str) -> Embedder | None:
    """Builds an Embedder instance by configuration name.

    Args:
        name: Name of embedder ("none", "hashing", "local", "voyage").

    Returns:
        Embedder instance or None if disabled.
    """
    name = (name or "none").lower()
    if name in ("none", "off", ""):
        return None
    if name == "hashing":
        return HashingEmbedder()
    if name == "voyage":
        return VoyageEmbedder()
    if name in ("local", "minilm", "sentence-transformers"):
        return LocalEmbedder()
    raise ValueError(f"Unknown embedder: {name}")


def embed_text(name: str, description: str, body: str, repo: str) -> str:
    """Constructs representative text representation for semantic embedding."""
    return "\n".join([
        f"{name}",
        f"{description}",
        f"repository: {repo}",
        body[:1500],
    ])
