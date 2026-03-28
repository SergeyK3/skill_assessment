# route: (examination) | file: skill_assessment/services/examination_protocol_scores.py
"""
Оценка ответов экзамена для протокола: шкала 1–4 и проценты.

При установленных ``sentence-transformers`` и эталонном тексте регламента (ядро HR) —
семантическая близость ответа к выдержке регламента и формулировке вопроса; иначе эвристика по объёму.
"""

from __future__ import annotations

from skill_assessment.domain.examination_scoring import score_answer_texts
from skill_assessment.services.examination_sentence_embedding import get_sentence_transformer_embedder

SCORING_NOTE_RU = (
    "Оценка по каждому ответу — автоматическая: при доступности sentence-transformers и тексте регламента в HR "
    "сравнивается смысловая близость ответа к регламенту и вопросу; иначе — эвристика по объёму текста. "
    "Баллы 1–4; проценты на шкале 50–100% (минимум 50% при балле 1); не заменяют экспертную проверку кадрами."
)


def semantic_or_heuristic_score_4(answer_text: str, reference_text: str) -> int:
    """
    Эталон (фрагмент регламента + вопрос) — ``reference_text``; при наличии эмбеддера — косинус → 1–4.
    """
    ref = (reference_text or "").strip()
    if not ref:
        return heuristic_score_4(answer_text)
    emb = get_sentence_transformer_embedder()
    if emb is None:
        return heuristic_score_4(answer_text)
    try:
        snap = score_answer_texts(ref, answer_text or "", emb)
    except Exception:
        return heuristic_score_4(answer_text)
    # rubric_0_3 в домене → 1–4 для протокола
    return max(1, min(4, snap.rubric_0_3 + 1))


def heuristic_score_4(answer_text: str) -> int:
    """Грубая шкала 1–4: чем содержательнее ответ, тем выше (до порогов можно подстроить)."""
    t = (answer_text or "").strip()
    if len(t) < 4:
        return 1
    words = len(t.split())
    if len(t) < 28 or words < 5:
        return 2
    if len(t) < 100 or words < 14:
        return 3
    return 4


def score_4_to_percent(score: int) -> float:
    """Отображение балла 1..4 на шкале 50..100 % (линейно: 1→50%, 4→100%)."""
    s = max(1, min(4, int(score)))
    return round(50.0 + ((s - 1) / 3.0) * 50.0, 1)


def average_scores(scores: list[int]) -> tuple[float, float]:
    """Средний балл (1..4) и средний процент на шкале 50..100%."""
    if not scores:
        return (0.0, 0.0)
    clean = [max(1, min(4, int(x))) for x in scores]
    avg = sum(clean) / len(clean)
    pct = 50.0 + ((avg - 1.0) / 3.0) * 50.0
    return (round(avg, 2), round(pct, 1))
