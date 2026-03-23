from __future__ import annotations

import hashlib
import logging
import math
import os
from dataclasses import dataclass
from datetime import timedelta

from openai import OpenAI

from monitor_agent.core.models import FilteringConfig, LLMConfig, Signal
from monitor_agent.core.utils import tokenize, utc_now

logger = logging.getLogger(__name__)


@dataclass
class CandidateMatch:
    signal: Signal
    similarity: float


class VectorIndex:
    """Lightweight vector index (FAISS-like retrieval behavior)."""

    def __init__(self) -> None:
        self._rows: list[tuple[Signal, list[float]]] = []

    def add(self, signal: Signal) -> None:
        if signal.embedding:
            self._rows.append((signal, signal.embedding))

    def query(self, query_vector: list[float], top_k: int) -> list[CandidateMatch]:
        if not query_vector or not self._rows or top_k <= 0:
            return []

        scored: list[CandidateMatch] = []
        for signal, vector in self._rows:
            similarity = _cosine_similarity(query_vector, vector)
            scored.append(CandidateMatch(signal=signal, similarity=similarity))
        scored.sort(key=lambda row: row.similarity, reverse=True)
        return scored[:top_k]

    def __len__(self) -> int:
        return len(self._rows)


class CandidateRetrievalEngine:
    def __init__(self, llm_config: LLMConfig, filtering_config: FilteringConfig) -> None:
        self.llm_config = llm_config
        self.filtering_config = filtering_config

        api_key = os.getenv("OPENAI_API_KEY")
        if llm_config.base_url and not api_key:
            api_key = "dummy"
        self.client = OpenAI(api_key=api_key, base_url=llm_config.base_url) if api_key else None

        embedding_base_url = llm_config.embedding_base_url or llm_config.base_url
        if llm_config.embedding_base_url:
            embedding_key = os.getenv("EMBEDDING_API_KEY") or "dummy"
        else:
            embedding_key = api_key
        self.embedding_client = (
            OpenAI(api_key=embedding_key, base_url=embedding_base_url)
            if embedding_key and embedding_base_url
            else (OpenAI(api_key=embedding_key) if embedding_key else None)
        )
        self._embed_cache: dict[str, list[float]] = {}

    def build_recent_index(self, history: list[Signal]) -> tuple[VectorIndex, list[str]]:
        cutoff = utc_now() - timedelta(days=self.filtering_config.event_candidate_lookback_days)
        candidates = [s for s in history if s.source != "user" and s.extracted_at >= cutoff]
        errors = self.ensure_embeddings(candidates)

        index = VectorIndex()
        for signal in candidates:
            index.add(signal)
        return index, errors

    def ensure_embeddings(self, signals: list[Signal]) -> list[str]:
        missing = [signal for signal in signals if not signal.embedding]
        if not missing:
            return []

        texts = [self._embedding_text(signal) for signal in missing]
        errors: list[str] = []

        if self.embedding_client is None:
            for signal, text in zip(missing, texts):
                signal.embedding = self._fallback_embedding(text)
            return errors

        try:
            response = self.embedding_client.embeddings.create(
                model=self.llm_config.embedding_model,
                input=texts,
            )
            for signal, row in zip(missing, response.data):
                signal.embedding = _normalize_vector([float(x) for x in row.embedding])
        except Exception as exc:
            msg = f"Embedding generation failed; using fallback embeddings: {exc}"
            logger.warning(msg)
            errors.append(msg)
            for signal, text in zip(missing, texts):
                signal.embedding = self._fallback_embedding(text)
        return errors

    def retrieve(self, signal: Signal, index: VectorIndex) -> list[CandidateMatch]:
        if not signal.embedding:
            self.ensure_embeddings([signal])

        matches = index.query(
            signal.embedding,
            top_k=self.filtering_config.event_candidate_top_k,
        )
        return [m for m in matches if m.signal.id != signal.id]

    def should_call_llm(self, matches: list[CandidateMatch]) -> bool:
        if not matches:
            return False
        return matches[0].similarity >= self.filtering_config.event_similarity_threshold

    def _fallback_embedding(self, text: str, dims: int = 96) -> list[float]:
        key = f"{dims}:{text}"
        cached = self._embed_cache.get(key)
        if cached is not None:
            return cached

        vector = [0.0] * dims
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            bucket = int(digest[:8], 16) % dims
            sign = -1.0 if int(digest[8:10], 16) % 2 else 1.0
            vector[bucket] += sign

        normalized = _normalize_vector(vector)
        self._embed_cache[key] = normalized
        return normalized

    @staticmethod
    def _embedding_text(signal: Signal) -> str:
        return " | ".join(
            [
                signal.title.strip(),
                signal.summary.strip(),
                " ".join(signal.tags),
                " ".join(signal.evidence),
                " ".join(signal.source_urls),
            ]
        )


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    return sum(left[i] * right[i] for i in range(length))
