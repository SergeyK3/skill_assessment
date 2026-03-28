# route: (domain scoring) | file: skill_assessment/domain/examination_scoring.py
"""
Чистая доменная логика: протокол, баллы, проценты, косинус по векторам.

Эмбеддинги не считаются здесь — передаются снаружи (модель или фиксированные векторы в тестах).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Косинусная близость в [-1, 1]; при нулевых векторах возвращает 0.0."""
    if len(a) != len(b):
        raise ValueError("embedding_dim_mismatch")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def cosine_to_percent(sim: float) -> float:
    """Линейное отображение [-1, 1] → [0, 100] для отображения «процента близости»."""
    return max(0.0, min(100.0, (sim + 1.0) * 50.0))


def rubric_0_3_from_similarity(sim: float) -> int:
    """Грубая шкала 0–3 по косинусу (MVP; пороги можно вынести в конфиг)."""
    if sim >= 0.85:
        return 3
    if sim >= 0.55:
        return 2
    if sim >= 0.25:
        return 1
    return 0


@dataclass(frozen=True)
class AnswerScoreSnapshot:
    """Снимок оценки одного ответа (для протокола / отчёта)."""

    cosine_similarity: float
    percent: float
    rubric_0_3: int


def score_answer_vs_reference(
    reference_embedding: list[float],
    answer_embedding: list[float],
) -> AnswerScoreSnapshot:
    sim = cosine_similarity(reference_embedding, answer_embedding)
    return AnswerScoreSnapshot(
        cosine_similarity=sim,
        percent=cosine_to_percent(sim),
        rubric_0_3=rubric_0_3_from_similarity(sim),
    )


@runtime_checkable
class TextEmbedder(Protocol):
    """Порта для подстановки: реальная модель или фиксированные векторы в тестах."""

    def embed(self, text: str) -> list[float]: ...


class FixedVectorEmbedder:
    """Тестовый эмбеддер: возвращает заранее заданный вектор по ключу (нормализованный текст)."""

    def __init__(self, vectors: dict[str, list[float]], *, default: list[float] | None = None) -> None:
        self._vectors = {k.strip().lower(): v for k, v in vectors.items()}
        self._default = default if default is not None else [0.0, 0.0, 1.0]

    def embed(self, text: str) -> list[float]:
        key = (text or "").strip().lower()
        return list(self._vectors.get(key, self._default))


def score_answer_texts(
    reference_text: str,
    answer_text: str,
    embedder: TextEmbedder,
) -> AnswerScoreSnapshot:
    """Сквозная оценка по двум строкам через переданный embedder (без сети внутри функции)."""
    return score_answer_vs_reference(embedder.embed(reference_text), embedder.embed(answer_text))


def session_mean_percent(per_item_percents: list[float]) -> float:
    """Средний процент по вопросам (пустой список → 0)."""
    if not per_item_percents:
        return 0.0
    return sum(per_item_percents) / len(per_item_percents)
