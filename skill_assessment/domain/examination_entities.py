# route: (domain examination) | file: skill_assessment/domain/examination_entities.py
"""
Сессия экзамена по регламентам / ДИ (отдельно от skill assessment Part1/2/3).

MVP: один сценарий `regulation_v1`, фиксированный список вопросов, протокол = текст ответов.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ExaminationSessionStatus(str, Enum):
    """Жизненный цикл назначения."""

    SCHEDULED = "scheduled"  # создано, сотрудник ещё не прошёл согласие
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ExaminationPhase(str, Enum):
    """Шаги сценария (веб / Telegram — одна машина состояний).

    Каналы: MVP не фиксирует «один канал на фазу»; веб и Telegram могут идти
    параллельно (один движок в API). Для UI: при открытии сессии по персональному
    токену веб-запросы используют ``session_id`` из ответа; Telegram — привязка
    chat_id к сотруднику.
    """

    CONSENT = "consent"  # первое обращение: согласие на ПДн / запись
    INTRO = "intro"  # вступление, «готовы начать?»
    QUESTIONS = "questions"  # фиксированные вопросы по порядку
    PROTOCOL = "protocol"  # просмотр/выдача протокола (PDF — позже)
    COMPLETED = "completed"
    BLOCKED_CONSENT = "blocked_consent"  # отказ от согласия — до действия HR


class ConsentStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class ExaminationQuestion(BaseModel):
    """Вопрос из банка сценария (фиксированный список)."""

    id: UUID
    scenario_id: str = Field(..., description="Идентификатор сценария, напр. regulation_v1")
    seq: int = Field(..., ge=0, description="Порядок в экзамене")
    text: str = Field(..., min_length=1)


class ExaminationAnswerRecord(BaseModel):
    """Ответ сотрудника (в протоколе — транскрипт/текст, без сырого аудио)."""

    question_id: UUID
    seq: int
    transcript_text: str
    created_at: datetime


class ExaminationProtocolItem(BaseModel):
    question_id: UUID
    seq: int
    question_text: str
    transcript_text: str


class ExaminationProtocol(BaseModel):
    """Сводка для выдачи сотруднику и хранения (баллы/% — позже)."""

    session_id: UUID
    scenario_id: str
    employee_id: str
    client_id: str
    items: list[ExaminationProtocolItem]
    completed_at: datetime | None = None
