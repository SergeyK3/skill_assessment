# route: (ORM / tables sa_*) | file: skill_assessment/infrastructure/db_models.py
"""
ORM-модели skill assessment на общем Base ядра (один SQLite с typical_infrastructure).

Импортируйте этот модуль до загрузки app.main, чтобы create_all увидел таблицы.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skill_assessment.bootstrap import ensure_typical_infrastructure_on_path

ensure_typical_infrastructure_on_path()

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SkillDomainRow(Base, TimestampMixin):
    __tablename__ = "sa_skill_domains"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    skills: Mapped[list[SkillRow]] = relationship(back_populates="domain", cascade="all, delete-orphan")


class SkillRow(Base, TimestampMixin):
    __tablename__ = "sa_skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    domain_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_skill_domains.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    domain: Mapped[SkillDomainRow] = relationship(back_populates="skills")


class AssessmentSessionRow(Base, TimestampMixin):
    __tablename__ = "sa_assessment_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    employee_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Куда ушло первое уведомление Part1 (для проверки callback inline-календаря без обязательной POST-привязки).
    docs_survey_notify_chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Выбранный в боте слот опроса по документам (дата+время, локальное время без TZ).
    docs_survey_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Согласие на ПДн для Part1: ``awaiting_first`` / ``accepted`` / ``declined`` / ``timed_out`` / ``None``.
    docs_survey_pd_consent_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    docs_survey_pd_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Когда отправлено первое сообщение с запросом согласия (для таймаута 10 мин).
    docs_survey_consent_prompt_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Когда уже уведомили HR об отказе/молчании (чтобы не слать повторно).
    docs_survey_hr_notified_no_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Напоминание за N минут до слота (готовность) уже отправлено.
    docs_survey_reminder_30m_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Ответ на напоминание готовности: ``ready`` / ``not_ready`` (заглушка под будущую логику).
    docs_survey_readiness_answer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Ожидаем текстовый ответ на «время пришло, готовы к вопросам по регламентам?» (перед экзаменом).
    docs_survey_exam_gate_awaiting: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: JSON: чек-лист «опрос по служебным документам» (Part 1).
    part1_docs_checklist_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Токен для личной страницы чек-листа (сотрудник без входа в HR).
    part1_docs_access_token: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True, index=True)
    #: JSON: набор кейсов Part 2, ответы сотрудника и результат оценки ИИ.
    part2_cases_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Токен персональной страницы оценки руководителя (Part 3).
    manager_access_token: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True, index=True)
    #: Когда руководителю уже отправили ссылку на оценку в Telegram.
    manager_assessment_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Общий комментарий руководителя по сотруднику/сессии (Part 3).
    manager_overall_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list[SkillAssessmentResultRow]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    part1_turns: Mapped[list["SessionPart1TurnRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="SessionPart1TurnRow.seq"
    )


class SessionPart1TurnRow(Base):
    """Реплики Part 1 (устовое интервью): храним текст; аудио — внешний слой STT/TTS."""

    __tablename__ = "sa_session_part1_turns"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_sa_part1_session_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_assessment_sessions.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    session: Mapped[AssessmentSessionRow] = relationship(back_populates="part1_turns")


class LlmPostSttBlacklistRow(Base, TimestampMixin):
    """Чёрный список: если транскрипт после STT совпадает — дальнейший вызов LLM (оценка и т.д.) не выполняется."""

    __tablename__ = "sa_llm_post_stt_blacklist"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: Подстрока (без учёта регистра) или выражение при ``match_mode=regex``.
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``substring`` | ``regex``
    match_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="substring", index=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class SkillAssessmentResultRow(Base, TimestampMixin):
    __tablename__ = "sa_skill_assessment_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_assessment_sessions.id"), nullable=False, index=True)
    skill_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_skills.id"), nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[AssessmentSessionRow] = relationship(back_populates="results")


class ExaminationQuestionRow(Base, TimestampMixin):
    """Фиксированные вопросы сценария экзамена (MVP: regulation_v1)."""

    __tablename__ = "sa_examination_questions"
    __table_args__ = (UniqueConstraint("scenario_id", "seq", name="uq_sa_exam_question_scenario_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class ExaminationSessionRow(Base, TimestampMixin):
    """Сессия экзамена по регламентам (отдельно от sa_assessment_sessions)."""

    __tablename__ = "sa_examination_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="consent", index=True)
    consent_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    needs_hr_release: Mapped[bool] = mapped_column(default=False, nullable=False)
    current_question_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_window_starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_window_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Непредсказуемый токен для веба (персональная ссылка без входа в портал); выдаётся при создании.
    access_token: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True, index=True)
    #: Если задано — вопросы берутся из ``sa_examination_questions`` с ``scenario_id == question_scenario_id``
    #: (обычно совпадает с id сессии при подборе из ядра по KPI/должности).
    question_scenario_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    answers: Mapped[list["ExaminationAnswerRow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ExaminationAnswerRow.created_at"
    )


class ExaminationTelegramBindingRow(Base, TimestampMixin):
    """Привязка Telegram chat_id к сотруднику в ядре (client_id + employee_id)."""

    __tablename__ = "sa_examination_telegram_bindings"
    __table_args__ = (UniqueConstraint("telegram_chat_id", name="uq_sa_exam_tg_chat"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class TelegramProcessContextRow(Base, TimestampMixin):
    """
    Контекст оркестрации Telegram: какой бизнес-поток сейчас активен для chat_id.

    Нужен, чтобы сообщения не «перехватывались» соседним сценарием при параллельных этапах.
    """

    __tablename__ = "sa_telegram_process_context"
    __table_args__ = (UniqueConstraint("telegram_chat_id", name="uq_sa_tg_process_chat"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    client_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    employee_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: exam | part2_cases | idle
    active_flow: Mapped[str] = mapped_column(String(32), nullable=False, default="idle", index=True)
    active_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ExaminationAnswerRow(Base):
    """Ответ по вопросу (в протоколе — текст / транскрипт)."""

    __tablename__ = "sa_examination_answers"
    __table_args__ = (UniqueConstraint("session_id", "question_id", name="uq_sa_exam_answer_session_question"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_examination_sessions.id"), nullable=False, index=True)
    question_id: Mapped[str] = mapped_column(String(36), ForeignKey("sa_examination_questions.id"), nullable=False, index=True)
    transcript_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    session: Mapped[ExaminationSessionRow] = relationship(back_populates="answers")


class CompetencyCatalogVersionRow(Base, TimestampMixin):
    """Версия матрицы компетенций (набор связей должность + подразделение + навыки)."""

    __tablename__ = "sa_competency_catalog_versions"
    __table_args__ = (UniqueConstraint("version_code", name="uq_sa_ccv_version_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: Мультитенантность (nullable = общая матрица по умолчанию).
    client_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    version_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    #: draft | active | archived
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Якорь к глобальному регламенту (опционально): трассировка «матрица из регламента v2».
    source_regulation_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_regulation_version_no: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Предыдущая версия каталога (после publish/activate).
    replaces_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sa_competency_catalog_versions.id"), nullable=True, index=True
    )
    #: Когда версия опубликована (переведена в active).
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    matrix_rows: Mapped[list["CompetencyMatrixRow"]] = relationship(
        back_populates="catalog_version", cascade="all, delete-orphan"
    )


class CompetencySkillDefinitionRow(Base, TimestampMixin):
    """Справочник формулировок навыков (глобальный каталог для матрицы)."""

    __tablename__ = "sa_competency_skill_definitions"
    __table_args__ = (UniqueConstraint("skill_code", name="uq_sa_csd_skill_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: Короткий устойчивый код (например хеш от названия).
    skill_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title_ru: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    matrix_rows: Mapped[list["CompetencyMatrixRow"]] = relationship(back_populates="skill_definition")


class CompetencyMatrixRow(Base, TimestampMixin):
    """
    Связь: версия × должность × тип подразделения (функция) × навык.
    «Домен» для строки матрицы задаётся парой position_code + department_code.
    """

    __tablename__ = "sa_competency_matrix"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "position_code",
            "department_code",
            "skill_rank",
            name="uq_sa_cm_version_pos_dept_rank",
        ),
        Index("ix_sa_cm_version_pos_dept", "version_id", "position_code", "department_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sa_competency_catalog_versions.id"), nullable=False, index=True
    )
    #: Код должности из глобального каталога (position_catalog.position_code).
    position_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Код функции / типа подразделения (как function_code в position_catalog: ACC, HR, SALES, …).
    department_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    skill_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sa_competency_skill_definitions.id"), nullable=False, index=True
    )
    #: Порядок навыка в рамках (версия, должность, подразделение): 1, 2, 3, …
    skill_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    catalog_version: Mapped[CompetencyCatalogVersionRow] = relationship(back_populates="matrix_rows")
    skill_definition: Mapped[CompetencySkillDefinitionRow] = relationship(back_populates="matrix_rows")


class KpiCatalogVersionRow(Base, TimestampMixin):
    """Версия каталога KPI (матрица по должности и подразделению с приоритетами)."""

    __tablename__ = "sa_kpi_catalog_versions"
    __table_args__ = (UniqueConstraint("version_code", name="uq_sa_kcv_version_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    version_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_regulation_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_regulation_version_no: Mapped[str | None] = mapped_column(String(16), nullable=True)
    replaces_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sa_kpi_catalog_versions.id"), nullable=True, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    matrix_rows: Mapped[list["KpiMatrixRow"]] = relationship(
        back_populates="catalog_version", cascade="all, delete-orphan"
    )


class KpiDefinitionRow(Base, TimestampMixin):
    """Справочник KPI (код и параметры показателя)."""

    __tablename__ = "sa_kpi_definitions"
    __table_args__ = (UniqueConstraint("kpi_code", name="uq_sa_kd_kpi_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kpi_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title_ru: Mapped[str] = mapped_column(String(512), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    period_type: Mapped[str] = mapped_column(String(16), nullable=False)
    default_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    matrix_rows: Mapped[list["KpiMatrixRow"]] = relationship(back_populates="kpi_definition")


class KpiMatrixRow(Base, TimestampMixin):
    """
    Связь: версия × должность × подразделение (функция) × KPI.
    ``kpi_rank`` = приоритет: 1 — наиболее важный, далее по убыванию.
    """

    __tablename__ = "sa_kpi_matrix"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "position_code",
            "department_code",
            "kpi_rank",
            name="uq_sa_km_version_pos_dept_rank",
        ),
        Index("ix_sa_km_version_pos_dept", "version_id", "position_code", "department_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sa_kpi_catalog_versions.id"), nullable=False, index=True
    )
    position_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    department_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    kpi_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sa_kpi_definitions.id"), nullable=False, index=True
    )
    kpi_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    catalog_version: Mapped[KpiCatalogVersionRow] = relationship(back_populates="matrix_rows")
    kpi_definition: Mapped[KpiDefinitionRow] = relationship(back_populates="matrix_rows")
