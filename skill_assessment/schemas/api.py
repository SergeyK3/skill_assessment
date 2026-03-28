# route: (API DTO) | file: skill_assessment/schemas/api.py
"""API DTO (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_serializer

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
    #: IANA-зона для подписей локального времени (как в ``DOCS_SURVEY_LOCAL_TIMEZONE``).
    docs_survey_local_timezone: str | None = None
    #: За сколько минут до слота уходит напоминание Telegram (готовность).
    docs_survey_reminder_minutes_before: int | None = None
    #: Слот опроса по документам — подпись в локальном времени, например «27.03.2026 16:00 (Asia/Almaty)».
    docs_survey_slot_local_label: str | None = None
    #: Когда придёт напоминание (если ещё не отправлено) или когда уже отправлено — то же в локальном времени.
    docs_survey_reminder_telegram_local_label: str | None = None
    #: Текст для HR: когда ждать сообщение в Telegram относительно слота.
    docs_survey_telegram_schedule_hint: str | None = None
    #: Минут до напоминания «готов/не готов» (если ещё не отправлено и слот задан); отрицательно — время прошло.
    docs_survey_minutes_until_reminder: int | None = None
    #: Минут до времени слота опроса по документам; отрицательно — слот в прошлом.
    docs_survey_minutes_until_slot: int | None = None
    #: Слот в зоне ``DOCS_SURVEY_LOCAL_TIMEZONE``: дата ``YYYY-MM-DD`` (для ручного редактирования в HR UI).
    docs_survey_slot_local_date: str | None = None
    #: Слот в той же зоне: время ``HH:MM``.
    docs_survey_slot_local_time: str | None = None
    #: Чек-лист «опрос по должностным документам» (Part 1) отмечен завершённым.
    part1_docs_checklist_completed: bool = False
    #: Токен личной страницы чек-листа для сотрудника (без входа в HR); URL: ``/api/skill-assessment/ui/part1-docs-checklist?token=…``
    part1_docs_checklist_token: str | None = None
    #: Метка «неявка» для HR: таймаут ПДн или пропущенный слот Part 1, если сотрудник не дошёл до экзамена.
    hr_no_show: bool = False
    #: Короткий статус связанного экзамена по регламентам для HR-таблицы.
    exam_status_label: str | None = None
    #: Токен персональной страницы руководителя (Part 3), если этап уже открыт.
    manager_assessment_token: str | None = None
    #: Дедлайн оценки руководителем (UTC, сериализация с ``Z``).
    manager_assessment_deadline_at: datetime | None = None
    #: Тот же дедлайн в локальной зоне для UI/Telegram.
    manager_assessment_deadline_label: str | None = None
    #: Когда ссылка на оценку руководителю уже ушла в Telegram.
    manager_assessment_notified_at: datetime | None = None

    model_config = {"from_attributes": False}

    @field_serializer(
        "created_at",
        "updated_at",
        "docs_survey_pd_consent_at",
        "manager_assessment_deadline_at",
        "manager_assessment_notified_at",
    )
    def _serialize_naive_utc_iso(self, v: datetime | None) -> str | None:
        """В JSON — ISO-8601 с Z, чтобы фронт не путал наивный UTC с локальным временем браузера."""
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Part1DocsQuestionOut(BaseModel):
    id: str
    text: str
    hint: str = ""


class Part1DocsChecklistOut(BaseModel):
    version: str
    questions: list[Part1DocsQuestionOut]
    answers: dict[str, str] = Field(default_factory=dict)
    completed: bool = False
    completed_at: str | None = None
    phase: SessionPhase


class DocsSurveySlotManualUpdate(BaseModel):
    """Ручная установка слота опроса по документам (локальное время в зоне ``DOCS_SURVEY_LOCAL_TIMEZONE``)."""

    local_date: str = Field(..., min_length=10, max_length=10, description="YYYY-MM-DD")
    local_time: str = Field(..., min_length=4, max_length=8, description="HH:MM, 24ч")


class Part1DocsChecklistSave(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict, description="Ключ — id вопроса, значение: yes | no | partial")
    complete: bool = Field(default=False, description="Завершить опрос и перейти к part2 (кейс)")


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


class Part2SkillRefOut(BaseModel):
    skill_id: str
    skill_code: str
    skill_title: str


class Part2SkillEvaluationOut(BaseModel):
    skill_id: str
    skill_code: str
    skill_title: str
    level_0_3: int | None = None
    pct_0_100: int | None = None
    evidence: str | None = None
    gaps: str | None = None


class Part2AiCommissionConsensusOut(BaseModel):
    overall_level_0_3: int | None = None
    overall_pct_0_100: int | None = None
    summary: str | None = None
    recommendation: str | None = None
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Part2LlmCostStepOut(BaseModel):
    step: str
    label: str
    model: str | None = None
    calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_rub: float = 0.0


class Part2LlmCostsOut(BaseModel):
    currency: str = "USD/RUB"
    usd_to_rub_rate: float = 0.0
    steps: list[Part2LlmCostStepOut] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_cost_rub: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0


class Part2CaseItemOut(BaseModel):
    case_id: str
    skill_id: str
    skill_code: str
    skill_title: str
    covered_skills: list[Part2SkillRefOut] = Field(default_factory=list)
    text: str
    source: str = Field(default="template", description="template | llm")
    answer: str = ""
    passed: bool | None = None
    case_level_0_3: int | None = None
    case_pct_0_100: int | None = None
    evaluation_note: str | None = None
    skill_evaluations: list[Part2SkillEvaluationOut] = Field(default_factory=list)


class Part2CasesPublicOut(BaseModel):
    session_id: str
    phase: SessionPhase
    case_count: int
    allotted_minutes: int
    completed: bool = False
    completed_at: datetime | None = None
    solved_cases: int = 0
    overall_pct: int = 0
    covered_skills: list[Part2SkillRefOut] = Field(default_factory=list)
    remaining_skills: list[Part2SkillRefOut] = Field(default_factory=list)
    can_offer_additional_cases: bool = False
    ai_commission_consensus: Part2AiCommissionConsensusOut | None = None
    cases: list[Part2CaseItemOut] = Field(default_factory=list)


class Part2CasesHrOut(Part2CasesPublicOut):
    llm_costs: Part2LlmCostsOut | None = None


Part2CasesOut = Part2CasesHrOut


class Part2CaseAnswerIn(BaseModel):
    case_id: str = Field(..., min_length=1, max_length=64)
    answer: str = Field(..., min_length=1, max_length=32000)


class Part2CasesSubmit(BaseModel):
    answers: list[Part2CaseAnswerIn] = Field(..., min_length=1)


class Part2AdditionalCasesRequest(BaseModel):
    skill_ids: list[str] = Field(default_factory=list)


class ManagerRatingItem(BaseModel):
    skill_id: str
    level: ProficiencyLevel
    #: Комментарий руководителя по навыку (попадает в отчёт; пусто — в отчёте прочерк).
    comment: str | None = Field(default=None, max_length=2000)


class ManagerRatingsBulk(BaseModel):
    ratings: list[ManagerRatingItem] = Field(default_factory=list)


class ManagerAssessmentSkillOut(BaseModel):
    skill_id: str
    skill_code: str
    skill_title: str
    is_active: bool = True
    current_level: ProficiencyLevel | None = None
    #: Сохранённый комментарий руководителя (без служебных заглушек).
    manager_comment: str | None = None
    kpi_hint: str | None = None


class ManagerAssessmentPageOut(BaseModel):
    session_id: str
    employee_label: str | None = None
    employee_position_label: str | None = None
    stage_title: str = "оценка руководителем"
    deadline_at: datetime | None = None
    deadline_label: str | None = None
    part2_summary: str | None = None
    kpi_summary: str | None = None
    can_submit: bool = True
    skills: list[ManagerAssessmentSkillOut] = Field(default_factory=list)

    @field_serializer("deadline_at")
    def _serialize_deadline(self, v: datetime | None) -> str | None:
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ManagerAssessmentSubmitOut(BaseModel):
    saved_count: int
    status: AssessmentSessionStatus
    phase: SessionPhase
    completed_at: datetime | None = None

    @field_serializer("completed_at")
    def _serialize_completed_at(self, v: datetime | None) -> str | None:
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


class SkillEvaluationReferenceRowOut(BaseModel):
    """Строка сводной таблицы: навык × сессия, итог без оценки руководителя (Part 1 + Part 2)."""

    session_id: str
    client_id: str
    employee_id: str | None = None
    employee_label: str | None = None
    position_label: str | None = None
    skill_id: str
    skill_code: str
    skill_title: str
    domain_title: str = ""
    #: Среднее по заполненным Part 1 и Part 2, шкала 0–3 (четыре уровня: 0…3).
    aggregate_level_0_3: float
    #: Тот же итог в процентах от максимума 3.
    aggregate_pct_0_100: int


class SkillEvaluationReferenceListOut(BaseModel):
    rows: list[SkillEvaluationReferenceRowOut] = Field(default_factory=list)
    total_sessions: int = 0
    session_limit: int = 100
    session_offset: int = 0


class SkillDevelopmentRecommendationOut(BaseModel):
    skill_code: str
    skill_title: str
    current_level: int | None = None
    reason: str | None = None
    actions: list[str] = Field(default_factory=list)


class PublicReportSessionOut(BaseModel):
    id: str
    client_id: str
    employee_id: str | None = None
    status: AssessmentSessionStatus
    phase: SessionPhase
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at", "started_at", "completed_at")
    def _serialize_public_session_datetimes(self, v: datetime | None) -> str | None:
        if v is None:
            return None
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ReportEmployeeHeaderOut(BaseModel):
    """Срез сотрудника для шапки отчёта (ФИО, должность, подразделение, коды из ядра HR)."""

    fio: str | None = None
    position_label: str | None = None
    department_label: str | None = None
    position_code: str | None = None
    department_code: str | None = None


class SessionReportPublicOut(BaseModel):
    session: PublicReportSessionOut
    generated_at: datetime
    employee_label: str | None = None
    employee_header: ReportEmployeeHeaderOut | None = None
    #: Пояснение по распознаванию речи Part 1 (переменная среды и краткая подпись).
    report_part1_stt_note: str = ""
    #: Коды навыков из матрицы/результатов сессии (уникальные, по алфавиту).
    report_matrix_skill_codes: list[str] = Field(default_factory=list)
    #: Метки KPI из ядра HR (регламент / экзаменационные вопросы); при отсутствии ядра — пусто.
    report_examination_kpi_codes: list[str] = Field(default_factory=list)
    part1_summary: str = "не проводилось (Part 1 — голос/STT позже)"
    part1_overall_level: int | None = None
    part1_overall_pct: int | None = None
    part1_turns: list[Part1TurnOut] = Field(default_factory=list)
    part2_summary: str = "кейс: см. evidence_case или заглушку Part 2"
    part2_case_count: int = 0
    part2_allotted_minutes: int = 0
    part2_solved_cases: int = 0
    part2_overall_pct: int = 0
    part2_overall_level: int | None = None
    part2_completed_at: datetime | None = None
    part2_ai_commission_consensus: Part2AiCommissionConsensusOut | None = None
    part2_cases: list[Part2CaseItemOut] = Field(default_factory=list)
    development_recommendations: list[SkillDevelopmentRecommendationOut] = Field(default_factory=list)
    rows: list[ReportSkillRow]


class SessionReportHrOut(SessionReportPublicOut):
    session: AssessmentSessionOut
    part2_llm_costs: Part2LlmCostsOut | None = None


SessionReportOut = SessionReportHrOut


class LlmPostSttBlacklistCreate(BaseModel):
    """Правило: если транскрипт/текст ``user`` совпадает — сохранение реплики и дальнейший LLM блокируются (422)."""

    pattern: str = Field(..., min_length=1, max_length=32000)
    match_mode: str = Field(default="substring", description="substring | regex")
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool = True


class LlmPostSttBlacklistPatch(BaseModel):
    is_active: bool


class LlmPostSttBlacklistOut(BaseModel):
    id: str
    pattern: str
    match_mode: str
    is_active: bool
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
