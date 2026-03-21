"""
Черновая модель сущностей skill assessment (без привязки к БД ядра).

Уровень: оценка на уровне skill (не micro-skill). Триада доказательств
(кейс / руководитель / факт) — как виды вкладов, не обязательно все в MVP.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ProficiencyLevel(int, Enum):
    """Пример 4-уровневой шкалы (можно заменить на конфиг заказчика)."""

    NONE = 0
    PARTIAL = 1
    TYPICAL = 2
    ADVANCED = 3


class EvidenceKind(str, Enum):
    CASE = "case"  # кейс / миниэкзамен
    MANAGER = "manager"  # наблюдение / чек-лист руководителя
    METRIC = "metric"  # фактический показатель / след в данных


class AssessmentSessionStatus(str, Enum):
    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SkillDomain(BaseModel):
    """Укрупнённая область (аналог «домена» в таксономии)."""

    id: UUID
    code: str = Field(..., examples=["COMM", "LEAD"])
    title: str = Field(..., examples=["Коммуникации"])


class Skill(BaseModel):
    """Измеряемый навык внутри домена."""

    id: UUID
    domain_id: UUID
    code: str = Field(..., examples=["PRESENTATION"])
    title: str = Field(..., examples=["Презентация"])


class AssessmentSession(BaseModel):
    """Сессия оценки (один прогон инструмента для сотрудника/контекста)."""

    id: UUID
    client_id: str = Field(description="Организация в терминах ядра (clients.id)")
    employee_id: str | None = Field(default=None, description="Сотрудник в ядре, если есть")
    status: AssessmentSessionStatus = AssessmentSessionStatus.DRAFT
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SkillAssessmentResult(BaseModel):
    """Итог по одному skill в рамках сессии."""

    id: UUID
    session_id: UUID
    skill_id: UUID
    level: ProficiencyLevel
    evidence_notes: dict[EvidenceKind, str | None] = Field(
        default_factory=dict,
        description="Тексты/ссылки по типам доказательств; null — источник не использован",
    )
