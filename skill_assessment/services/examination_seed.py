# route: (startup seed examination) | file: skill_assessment/services/examination_seed.py
"""Фиксированные вопросы сценария regulation_v1 (MVP)."""

from __future__ import annotations

import uuid

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
        "Какие нормативные требования и внутренние регламенты в зоне вашей профессиональной ответственности "
        "(по должности и подразделению) вы обязаны учитывать в работе? Приведите пример.",
    ),
    (
        2,
        "Опишите один ключевой KPI (показатель эффективности), заданный для вашей должности или подразделения "
        "в плане целей, и как вы отслеживаете его достижение (источники данных, периодичность).",
    ),
    (
        3,
        "Где вы находите актуальные версии нормативных документов организации и как проверяете, что версия действующая?",
    ),
    (
        4,
        "Каково назначение или цель вашей должности в организации и подразделении? Сформулируйте своими словами.",
    ),
]


def ensure_examination_questions(db: Session) -> None:
    """Создать недостающие строки и обновить тексты (в т.ч. добавить 5-й вопрос к старым БД с четырьмя)."""
    for seq, text in REGULATION_V1_QUESTION_TEXTS:
        qid = _qid(seq)
        row = db.get(ExaminationQuestionRow, qid)
        if row is None:
            db.add(
                ExaminationQuestionRow(
                    id=qid,
                    scenario_id=SCENARIO_REGULATION_V1,
                    seq=seq,
                    text=text,
                )
            )
        elif row.text.strip() != text.strip():
            row.text = text
    db.commit()
