# route: (startup seed examination) | file: skill_assessment/services/examination_seed.py
"""Фиксированные вопросы сценария regulation_v1 (MVP)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import ExaminationQuestionRow

SCENARIO_REGULATION_V1 = "regulation_v1"

# Детерминированные id для стабильных тестов (uuid5).
_NS = uuid.UUID("018f0884-7b6a-7f3a-9b2c-111111111101")


def _qid(seq: int) -> str:
    return str(uuid.uuid5(_NS, f"{SCENARIO_REGULATION_V1}:q{seq}"))


REGULATION_V1_QUESTION_TEXTS: list[tuple[int, str]] = [
    (
        0,
        "Назовите основные документы внутренних регламентов, которые вы обязаны знать по своей должности.",
    ),
    (
        1,
        "Как вы действуете при выявлении несоответствия требованиям охраны труда на рабочем месте?",
    ),
    (
        2,
        "Опишите один ключевой KPI вашей должности и как вы его отслеживаете.",
    ),
    (
        3,
        "Где вы находите актуальные версии нормативных документов организации и как проверяете, что версия действующая?",
    ),
]


def ensure_examination_questions(db: Session) -> None:
    if db.scalar(
        select(ExaminationQuestionRow.id).where(ExaminationQuestionRow.scenario_id == SCENARIO_REGULATION_V1).limit(1)
    ) is not None:
        return
    for seq, text in REGULATION_V1_QUESTION_TEXTS:
        db.add(
            ExaminationQuestionRow(
                id=_qid(seq),
                scenario_id=SCENARIO_REGULATION_V1,
                seq=seq,
                text=text,
            )
        )
