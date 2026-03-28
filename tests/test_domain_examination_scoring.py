# route: (pytest) | file: tests/test_domain_examination_scoring.py
"""Юнит-тесты домена: векторы, косинус, проценты, баллы 0–3 без моделей."""

from __future__ import annotations

import math

from skill_assessment.domain.examination_scoring import (
    AnswerScoreSnapshot,
    FixedVectorEmbedder,
    cosine_similarity,
    cosine_to_percent,
    rubric_0_3_from_similarity,
    score_answer_texts,
    score_answer_vs_reference,
    session_mean_percent,
)


def test_cosine_identical_is_one() -> None:
    v = [1.0, 0.0, 0.0]
    assert cosine_similarity(v, v) == 1.0


def test_cosine_opposite_is_minus_one() -> None:
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(cosine_similarity(a, b) - (-1.0)) < 1e-9


def test_cosine_orthogonal_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_zero_vector_returns_zero() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_mismatch_raises() -> None:
    try:
        cosine_similarity([1.0], [1.0, 0.0])
        assert False
    except ValueError as e:
        assert "embedding_dim_mismatch" in str(e)


def test_cosine_to_percent_endpoints() -> None:
    assert cosine_to_percent(-1.0) == 0.0
    assert cosine_to_percent(1.0) == 100.0
    assert cosine_to_percent(0.0) == 50.0


def test_rubric_thresholds() -> None:
    assert rubric_0_3_from_similarity(1.0) == 3
    assert rubric_0_3_from_similarity(0.85) == 3
    assert rubric_0_3_from_similarity(0.84) == 2
    assert rubric_0_3_from_similarity(0.55) == 2
    assert rubric_0_3_from_similarity(0.25) == 1
    assert rubric_0_3_from_similarity(0.24) == 0


def test_score_answer_vs_reference_snapshot() -> None:
    ref = [1.0, 0.0, 0.0]
    ans = [1.0 / math.sqrt(2), 1.0 / math.sqrt(2), 0.0]
    snap = score_answer_vs_reference(ref, ans)
    assert isinstance(snap, AnswerScoreSnapshot)
    assert -1.0 <= snap.cosine_similarity <= 1.0
    assert 0.0 <= snap.percent <= 100.0
    assert snap.rubric_0_3 in (0, 1, 2, 3)


def test_fixed_vector_embedder_deterministic() -> None:
    emb = FixedVectorEmbedder(
        {
            "эталон": [1.0, 0.0, 0.0],
            "ответ": [1.0, 0.0, 0.0],
        }
    )
    s = score_answer_texts("эталон", "ответ", emb)
    assert s.cosine_similarity == 1.0
    assert s.percent == 100.0
    assert s.rubric_0_3 == 3


def test_session_mean_percent() -> None:
    assert session_mean_percent([80.0, 60.0]) == 70.0
    assert session_mean_percent([]) == 0.0
