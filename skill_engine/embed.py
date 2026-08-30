"""Optional dense embeddings for semantic recall.

BM25 alone is a genuinely strong baseline for this corpus — skill descriptions
are short, keyword-dense, and written to be matched — so embeddings are opt-in
rather than assumed. When they help is when an agent queries in its own words
("read a spreadsheet and chart it") against a skill that says "xlsx analysis
and visualisation".

Anthropic does not serve an embeddings endpoint; Voyage AI is the recommended
provider, and a local sentence-transformers model costs nothing to run. Both
are wired here behind one interface, along with a zero-dependency hashing
vectoriser useful for testing the plumbing without spending anything.
"""

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
    model: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


class HashingEmbedder:
    """Deterministic, dependency-free, free to run.

    A hashed bag-of-words with sublinear term weighting. It is *not* semantic —
    it will not connect "spreadsheet" to "xlsx" — but it exercises the whole
    vector path end to end, which makes it the right default for tests and for
    proving out the pipeline before paying for a real model.
    """

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
    """Voyage AI — the embeddings provider Anthropic recommends alongside Claude."""

    def __init__(self, model: str = "voyage-3.5", api_key: str | None = None) -> None:
        import voyageai  # optional dependency

        self.client = voyageai.Client(api_key=api_key or os.getenv("VOYAGE_API_KEY"))
        self.model = model
        self.dim = 1024

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 128):  # Voyage caps batch size
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
    """sentence-transformers on the CPU: no API key, no per-call cost."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # optional dependency

        self.st = SentenceTransformer(model)
        self.model = model
        self.dim = self.st.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.st.encode(texts, normalize_embeddings=True)]


def build(name: str) -> Embedder | None:
    name = (name or "none").lower()
    if name in ("none", "off", ""):
        return None
    if name == "hashing":
        return HashingEmbedder()
    if name == "voyage":
        return VoyageEmbedder()
    if name in ("local", "minilm", "sentence-transformers"):
        return LocalEmbedder()
    raise ValueError(f"unknown embedder: {name}")


def embed_text(name: str, description: str, body: str, repo: str) -> str:
    """What actually gets embedded.

    Weighted towards the fields that describe intent. The body is truncated
    because a skill's opening paragraphs carry its purpose; the rest is
    procedure, which dilutes the vector.
    """
    return "\n".join([
        f"{name}",
        f"{description}",
        f"repository: {repo}",
        body[:1500],
    ])
