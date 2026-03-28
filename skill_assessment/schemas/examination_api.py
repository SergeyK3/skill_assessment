# route: (API DTO examination) | file: skill_assessment/schemas/examination_api.py

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_assessment.domain.examination_entities import (
    ConsentStatus,
    ExaminationPhase,
    ExaminationSessionStatus,
)


class ExaminationSessionCreate(BaseModel):
    client_id: str = Field(..., description="ID организации (clients.id в ядре)")
    employee_id: str = Field(..., min_length=1, description="Сотрудник в ядре")
    scenario_id: str = Field(
        default="regulation_v1",
        description="Один сценарий MVP; другие — позже",
    )
    access_window_starts_at: datetime | None = None
    access_window_ends_at: datetime | None = None


class ExaminationSessionOut(BaseModel):
    id: str
    client_id: str
    employee_id: str
    scenario_id: str
    status: ExaminationSessionStatus
    phase: ExaminationPhase
    consent_status: ConsentStatus
    needs_hr_release: bool = Field(description="Отказ от согласия — нужна отметка HR")
    needs_hr_regulation_release: bool = Field(
        default=False,
        description="Нет регламента/KPI для экзамена — нужны действия HR (снятие блока после загрузки регламента)",
    )
    current_question_index: int = Field(ge=0, description="Индекс текущего вопроса (0-based)")
    question_count: int = Field(ge=0, description="Число вопросов в сценарии")
    access_window_starts_at: datetime | None
    access_window_ends_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    access_token: str | None = Field(
        default=None,
        description="Секрет для персональной веб-ссылки; только при создании и GET .../by-access-token/...",
    )
    #: Для завершённых сессий совпадает с датой завершения (автооценка при завершении).
    evaluated_at: datetime | None = None
    #: Заполняются при GET /examination/sessions?enrich=1 (кадры).
    employee_display_name: str | None = None
    employee_position_label: str | None = None
    employee_department_label: str | None = None
    average_score_4: float | None = None
    average_score_percent: float | None = None


class ExaminationConsentBody(BaseModel):
    accepted: bool


class ExaminationIntroDoneBody(BaseModel):
    """Заглушка под «готов начать» — тело можно расширить."""

    ready: bool = True


class ExaminationAnswerBody(BaseModel):
    transcript_text: str = Field(..., min_length=1, max_length=32000, description="Текст ответа / транскрипт")


class ExaminationQuestionOut(BaseModel):
    id: str
    scenario_id: str
    seq: int
    text: str


class ExaminationProtocolItemOut(BaseModel):
    question_id: str
    seq: int
    question_text: str
    transcript_text: str
    score_4: int = Field(..., ge=1, le=4, description="Балл по вопросу, шкала 1–4 (эвристика MVP)")
    score_percent: float = Field(
        ...,
        ge=50,
        le=100,
        description="Тот же балл в процентах на шкале 50–100% (1→50%, 4→100%)",
    )


class ExaminationProtocolOut(BaseModel):
    session_id: str
    scenario_id: str
    employee_id: str
    client_id: str
    items: list[ExaminationProtocolItemOut]
    completed_at: datetime | None
    employee_last_name: str = Field(default="—", description="Фамилия (из HR или разбор ФИО)")
    employee_first_name: str = Field(default="—")
    employee_middle_name: str = Field(default="—")
    employee_position_label: str | None = None
    employee_department_label: str | None = None
    average_score_4: float | None = None
    average_score_percent: float | None = None
    #: Момент фиксации оценки: при завершённом экзамене = завершение сессии; иначе — время формирования черновика.
    evaluated_at: datetime | None = None
    evaluation_is_preliminary: bool = Field(
        default=False,
        description="True, если экзамен ещё не завершён — оценка предварительная",
    )
    scoring_note: str = Field(
        default="Оценка по каждому ответу — автоматическая (эвристика MVP), не заменяет экспертную проверку.",
    )


class TelegramBindingCreate(BaseModel):
    client_id: str = Field(..., min_length=1)
    employee_id: str = Field(..., min_length=1)
    telegram_chat_id: str = Field(..., min_length=1, description="chat.id из Telegram (строка)")


class TelegramBindingOut(BaseModel):
    id: str
    telegram_chat_id: str
    client_id: str
    employee_id: str
