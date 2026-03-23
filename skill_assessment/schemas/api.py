# route: (API DTO) | file: skill_assessment/schemas/api.py
"""API DTO (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_assessment.domain.entities import (
    AssessmentSessionStatus,
    EvidenceKind,
    Part1TurnRole,
    ProficiencyLevel,
    SessionPhase,
)


class SessionCreate(BaseModel):
    client_id: str = Field(..., description="ID организации (clients.id в ядре)")
    employee_id: str | None = Field(default=None, description="Сотрудник, если есть")


class AssessmentSessionOut(BaseModel):
    id: str
    client_id: str
    employee_id: str | None
    status: AssessmentSessionStatus
    phase: SessionPhase
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: Part1 / Telegram: согласие на ПДн и слот опроса по документам (если колонки есть в БД).
    docs_survey_pd_consent_status: str | None = None
    docs_survey_pd_consent_at: datetime | None = None
    docs_survey_scheduled_at: datetime | None = None
    docs_survey_readiness_answer: str | None = None
    docs_survey_reminder_30m_sent_at: datetime | None = None

    model_config = {"from_attributes": False}


class AssessmentSessionListOut(BaseModel):
    """Список сессий с пагинацией (GET /sessions)."""

    items: list[AssessmentSessionOut]
    total: int


class DocsSurveyTelegramOut(BaseModel):
    """Результат отправки уведомления об опросе по документам (POST /sessions/{id}/start)."""

    sent: bool = False
    chat_id: str | None = None
    skipped_reason: str | None = Field(
        default=None,
        description="no_bot_token | session_not_found | no_chat_id | ошибка Telegram API",
    )


class AssessmentSessionStartOut(AssessmentSessionOut):
    """Ответ старта сессии: состояние + попытка уведомить сотрудника в Telegram."""

    docs_survey_telegram: DocsSurveyTelegramOut = Field(default_factory=DocsSurveyTelegramOut)


class SessionCancelBody(BaseModel):
    """Отмена назначения на оценку (HR): черновик или незавершённая сессия."""

    reason: str | None = Field(default=None, max_length=500, description="Необязательная причина (пока не сохраняется в БД)")


class SessionPhaseUpdate(BaseModel):
    phase: SessionPhase


class Part1TurnCreate(BaseModel):
    role: Part1TurnRole
    text: str = Field(..., min_length=1, max_length=32000)


class Part1TurnsAppend(BaseModel):
    turns: list[Part1TurnCreate] = Field(..., min_length=1)


class Part1TurnOut(BaseModel):
    id: str
    session_id: str
    seq: int
    role: Part1TurnRole
    text: str
    created_at: datetime


class CaseTextOut(BaseModel):
    session_id: str
    skill_id: str
    skill_code: str
    skill_title: str
    text: str
    source: str = Field(default="template", description="template | llm (позже)")


class ManagerRatingItem(BaseModel):
    skill_id: str
    level: ProficiencyLevel


class ManagerRatingsBulk(BaseModel):
    ratings: list[ManagerRatingItem] = Field(..., min_length=1)


class SkillDomainOut(BaseModel):
    id: str
    code: str
    title: str


class SkillOut(BaseModel):
    id: str
    domain_id: str
    code: str
    title: str


class SkillResultCreate(BaseModel):
    skill_id: str
    level: ProficiencyLevel
    evidence_notes: dict[EvidenceKind, str | None] = Field(default_factory=dict)


class SkillAssessmentResultOut(BaseModel):
    id: str
    session_id: str
    skill_id: str
    level: ProficiencyLevel
    evidence_notes: dict[EvidenceKind, str | None]
    created_at: datetime
    updated_at: datetime


class ClassifierImportOut(BaseModel):
    sheet_used: str
    domains_created: int
    skills_created: int
    skills_updated: int


class ReportSkillRow(BaseModel):
    skill_id: str
    skill_code: str
    skill_title: str
    domain_title: str
    level: int
    level_label_ru: str
    part1_level: int | None = None
    part2_level: int | None = None
    part3_level: int | None = None
    evidence_case: str | None = None
    evidence_manager: str | None = None
    evidence_metric: str | None = None


class SessionReportOut(BaseModel):
    session: AssessmentSessionOut
    generated_at: datetime
    employee_label: str | None = None
    part1_summary: str = "не проводилось (Part 1 — голос/STT позже)"
    part1_turns: list[Part1TurnOut] = Field(default_factory=list)
    part2_summary: str = "кейс: см. evidence_case или заглушку Part 2"
    rows: list[ReportSkillRow]
