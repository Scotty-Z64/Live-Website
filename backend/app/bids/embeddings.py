"""1536-dim embeddings for catalog ↔ BoQ matching.

Production: OpenAI ``text-embedding-3-small`` (1536 dims) as specified in the blueprint.
Development fallback (no OPENAI_API_KEY): deterministic feature-hashing embedding of word
uni/bi-grams and character trigrams — lexical rather than semantic, but stable and offline.
Switching provider requires ``python -m app.cli reindex-catalog`` so vectors stay comparable.
"""
from __future__ import annotations

import hashlib
import math
import re

from ..config import get_settings

DIM = 1536


def _hash_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    words = re.findall(r"[a-z0-9]+", text.lower())
    feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
    for w in words:
        padded = f"#{w}#"
        feats += [padded[i : i + 3] for i in range(len(padded) - 2)]
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        vec[h % DIM] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def provider() -> str:
    return "openai" if get_settings().openai_api_key else "hashing-dev"


def embed_many(texts: list[str]) -> list[list[float]]:
    s = get_settings()
    if not texts:
        return []
    if s.openai_api_key:
        from openai import OpenAI

        client = OpenAI(api_key=s.openai_api_key)
        out: list[list[float]] = []
        for i in range(0, len(texts), 256):
            resp = client.embeddings.create(model=s.openai_embedding_model, input=texts[i : i + 256])
            out += [d.embedding for d in resp.data]
        return out
    return [_hash_embed(t) for t in texts]


def embed(text: str) -> list[float]:
    return embed_many([text])[0]
