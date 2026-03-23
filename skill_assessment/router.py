# route: /api/skill-assessment/* | file: skill_assessment/router.py
"""HTTP routes for skill assessment."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from skill_assessment.domain.entities import (
    AssessmentSession,
    Skill,
    SkillAssessmentResult,
    SkillDomain,
)
from skill_assessment.schemas.api import (
    AssessmentSessionListOut,
    AssessmentSessionOut,
    AssessmentSessionStartOut,
    CaseTextOut,
    ClassifierImportOut,
    ManagerRatingsBulk,
    Part1TurnOut,
    Part1TurnsAppend,
    SessionCancelBody,
    SessionCreate,
    SessionPhaseUpdate,
    SessionReportOut,
    SkillAssessmentResultOut,
    SkillDomainOut,
    SkillOut,
    SkillResultCreate,
)
from skill_assessment.services import assessment_service as svc
from skill_assessment.services import classifier_import as classifier_import_svc
from skill_assessment.services import part1_service as part1_svc
from skill_assessment.services import report_service as report_svc
from skill_assessment.services import docs_survey_notify
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.schemas.examination_api import (
    ExaminationAnswerBody,
    ExaminationConsentBody,
    ExaminationIntroDoneBody,
    ExaminationProtocolOut,
    ExaminationQuestionOut,
    ExaminationSessionCreate,
    ExaminationSessionOut,
    TelegramBindingCreate,
    TelegramBindingOut,
)

router = APIRouter(prefix="/skill-assessment", tags=["skill-assessment"])

_static = Path(__file__).resolve().parent / "static"
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

_HTML_NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


def _html_response(path: Path, missing_detail: str) -> FileResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail=missing_detail)
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_HTML_NO_CACHE)


@router.get("/health")
def skill_assessment_health() -> dict:
    """Liveness for the skill-assessment plugin."""
    return {"status": "ok", "module": "skill_assessment"}


@router.get("/telegram/debug")
def skill_assessment_telegram_debug() -> dict[str, Any]:
    """Диагностика: прочитан ли .env, включён ли polling, не висит ли webhook (без вывода токена)."""
    import httpx

    from skill_assessment import telegram_runtime as tg_rt

    load_dotenv(_ENV_PATH, override=True)
    raw = os.getenv("TELEGRAM_ENABLE_POLLING", "")
    poll = raw.strip().lower() in ("1", "true", "yes")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    webhook: dict[str, Any] | None = None
    if token:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15.0)
        webhook = r.json()
    return {
        "env_path": str(_ENV_PATH),
        "env_file_exists": _ENV_PATH.is_file(),
        "TELEGRAM_ENABLE_POLLING_raw": raw,
        "telegram_enable_polling_parsed": poll,
        "telegram_token_configured": bool(len(token) > 15),
        "polling_task_started": tg_rt.polling_started,
        "getWebhookInfo": webhook,
    }


@router.get("/landing", include_in_schema=True)
def skill_assessment_landing() -> FileResponse:
    """Входная страница: контекст организации и ссылки в ядро."""
    return _html_response(_static / "index.html", "skill_assessment_static_missing")


@router.get("/workspace", include_in_schema=True)
def skill_assessment_workspace() -> FileResponse:
    """Рабочий UI: сотрудники, сессия, три этапа (документы / кейс / руководитель)."""
    return _html_response(_static / "part3_flow.html", "skill_assessment_part3_static_missing")


@router.get("/ui", include_in_schema=True)
def skill_assessment_ui_redirect(request: Request) -> RedirectResponse:
    """Совместимость и обход кэша: раньше /ui отдавал лендинг — редирект на /workspace."""
    q = request.url.query
    dest = "/api/skill-assessment/workspace" + ("?" + q if q else "")
    return RedirectResponse(url=dest, status_code=307)


@router.get("/demo/future-scenario", response_class=HTMLResponse, include_in_schema=False)
def demo_future_scenario_html() -> HTMLResponse:
    """Тот же превью-отчёт, что и GET /skill-assessment/future-demo (удобно проверять из Swagger)."""
    p = _static / "demo_future_scenario.html"
    if not p.exists():
        return HTMLResponse("<p>demo_future_scenario.html missing</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/domain/json-schema")
def domain_json_schema() -> dict:
    """JSON Schema черновых сущностей (контракт домена)."""
    return {
        "SkillDomain": SkillDomain.model_json_schema(),
        "Skill": Skill.model_json_schema(),
        "AssessmentSession": AssessmentSession.model_json_schema(),
        "SkillAssessmentResult": SkillAssessmentResult.model_json_schema(),
    }


@router.get("/taxonomy/domains", response_model=list[SkillDomainOut])
def get_domains(db: Annotated[Session, Depends(get_db)]) -> list[SkillDomainOut]:
    return svc.list_domains(db)


@router.get("/taxonomy/skills", response_model=list[SkillOut])
def get_skills(
    db: Annotated[Session, Depends(get_db)],
    domain_id: str | None = Query(default=None, description="Фильтр по домену"),
) -> list[SkillOut]:
    return svc.list_skills(db, domain_id)


@router.post("/taxonomy/import-classifier", response_model=ClassifierImportOut)
def post_taxonomy_import_classifier(
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(..., description="Excel: лист «Классификатор_навыков» (колонки skill_id, domain, skill_name)"),
) -> ClassifierImportOut:
    """Импорт доменов и навыков из файла классификатора (как scripts/build_classifier_management_sales_xlsx)."""
    raw = file.file.read()
    return classifier_import_svc.import_classifier_xlsx(db, raw)


@router.post("/sessions", response_model=AssessmentSessionOut)
def post_session(
    body: SessionCreate,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.create_session(db, body)


@router.get("/sessions", response_model=AssessmentSessionListOut)
def get_sessions(
    db: Annotated[Session, Depends(get_db)],
    client_id: str | None = Query(default=None, description="Организация (clients.id)"),
    employee_id: str | None = Query(default=None, description="Сотрудник"),
    phase: str | None = Query(default=None, description="Фаза: draft, part1, part2, …"),
    status: str | None = Query(default=None, description="draft | in_progress | completed | …"),
    created_from: datetime | None = Query(default=None, description="Нижняя граница created_at (ISO)"),
    created_to: datetime | None = Query(default=None, description="Верхняя граница created_at (ISO)"),
    pd_consent_status: str | None = Query(
        default=None,
        description="Согласие ПДн (статус) или __empty__ если не задано",
    ),
    q: str | None = Query(default=None, description="Поиск по client_id, id сессии, employee_id (подстрока)"),
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=50, ge=1, le=200),
) -> AssessmentSessionListOut:
    items, total = svc.list_sessions(
        db,
        client_id=client_id,
        employee_id=employee_id,
        phase=phase,
        status=status,
        created_from=created_from,
        created_to=created_to,
        pd_consent_status=pd_consent_status,
        q=q,
        offset=offset,
        limit=limit,
    )
    return AssessmentSessionListOut(items=items, total=total)


@router.get("/sessions/{session_id}", response_model=AssessmentSessionOut)
def get_session(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.get_session(db, session_id)


@router.post("/sessions/{session_id}/phase", response_model=AssessmentSessionOut)
def post_session_phase(
    session_id: str,
    body: SessionPhaseUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    """Зафиксировать фазу сценария (Part 1/2/3, отчёт, завершение)."""
    return svc.set_session_phase(db, session_id, body)


@router.get("/sessions/{session_id}/part1/turns", response_model=list[Part1TurnOut])
def get_part1_turns(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> list[Part1TurnOut]:
    """Part 1: реплики интервью (текст после STT для user, текст LLM для llm)."""
    return part1_svc.list_part1_turns(db, session_id)


@router.post("/sessions/{session_id}/part1/turns", response_model=list[Part1TurnOut])
def post_part1_turns(
    session_id: str,
    body: Part1TurnsAppend,
    db: Annotated[Session, Depends(get_db)],
) -> list[Part1TurnOut]:
    """Добавить одну или несколько реплик Part 1 (идут подряд с авто seq)."""
    return part1_svc.append_part1_turns(db, session_id, body)


@router.get("/sessions/{session_id}/case", response_model=CaseTextOut)
def get_session_case(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    skill_id: str = Query(..., description="Навык для генерации кейса (Part 2)"),
) -> CaseTextOut:
    """Текст кейса по сессии и навыку: сейчас шаблон; позже — LLM с теми же входами, что в демо."""
    return svc.get_case_stub(db, session_id, skill_id)


@router.post("/sessions/{session_id}/manager-ratings", response_model=list[SkillAssessmentResultOut])
def post_manager_ratings(
    session_id: str,
    body: ManagerRatingsBulk,
    db: Annotated[Session, Depends(get_db)],
) -> list[SkillAssessmentResultOut]:
    """Part 3: оценки руководителя по навыкам (upsert по skill_id, фиксируется evidence manager)."""
    return svc.save_manager_ratings(db, session_id, body)


@router.get("/ui/part1", include_in_schema=True)
def skill_assessment_ui_part1() -> FileResponse:
    """Черновая форма Part 1: ввод реплик (без аудио; имитация STT/LLM)."""
    return _html_response(_static / "part1_flow.html", "skill_assessment_part1_static_missing")


@router.get("/ui/part3", include_in_schema=True)
def skill_assessment_ui_part3() -> FileResponse:
    """Тот же HTML, что /workspace (сквозной Part 3)."""
    return _html_response(_static / "part3_flow.html", "skill_assessment_part3_static_missing")


@router.post("/sessions/{session_id}/start", response_model=AssessmentSessionStartOut)
def post_session_start(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionStartOut:
    svc.start_session(db, session_id)
    tg = docs_survey_notify.send_docs_survey_assignment_notice(db, session_id)
    # После уведомления в Telegram обновляются поля сессии (согласие, chat_id) — отдаём актуальное состояние.
    fresh = svc.get_session(db, session_id)
    return AssessmentSessionStartOut(**fresh.model_dump(), docs_survey_telegram=tg)


@router.post("/sessions/{session_id}/complete", response_model=AssessmentSessionOut)
def post_session_complete(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.complete_session(db, session_id)


@router.post("/sessions/{session_id}/cancel", response_model=AssessmentSessionOut)
def post_session_cancel(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    body: SessionCancelBody | None = None,
) -> AssessmentSessionOut:
    """Отменить назначение (черновик или незавершённая сессия), чтобы сотрудник не оставался «застрявшим»."""
    return svc.cancel_session(db, session_id, body)


@router.post("/sessions/{session_id}/results", response_model=SkillAssessmentResultOut)
def post_result(
    session_id: str,
    body: SkillResultCreate,
    db: Annotated[Session, Depends(get_db)],
) -> SkillAssessmentResultOut:
    return svc.add_result(db, session_id, body)


@router.get("/sessions/{session_id}/results", response_model=list[SkillAssessmentResultOut])
def get_results(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> list[SkillAssessmentResultOut]:
    return svc.list_results(db, session_id)


@router.get("/sessions/{session_id}/report", response_model=SessionReportOut)
def get_session_report(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> SessionReportOut:
    """JSON отчёта по сессии (для фронта / печати)."""
    return report_svc.build_session_report(db, session_id)


@router.get("/sessions/{session_id}/report/html", response_class=HTMLResponse)
def get_session_report_html(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """HTML отчёт, визуально близкий к demo/future-scenario."""
    rep = report_svc.build_session_report(db, session_id)
    return HTMLResponse(report_svc.render_session_report_html(rep))


# --- Экзамен по регламентам / ДИ (отдельная сессия, MVP: regulation_v1) ---


@router.post("/examination/sessions", response_model=ExaminationSessionOut)
def post_examination_session(
    body: ExaminationSessionCreate,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    """Создать назначение на экзамен (один сценарий, фиксированные вопросы)."""
    return examination_svc.create_examination_session(db, body)


@router.get("/examination/sessions", response_model=list[ExaminationSessionOut])
def get_examination_sessions(
    db: Annotated[Session, Depends(get_db)],
    client_id: str | None = Query(default=None),
    employee_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[ExaminationSessionOut]:
    return examination_svc.list_examination_sessions(db, client_id, employee_id, limit)


@router.get("/examination/sessions/by-access-token/{access_token}", response_model=ExaminationSessionOut)
def get_examination_session_by_access_token(
    access_token: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    """Персональная веб-ссылка: сессия по секрету (без входа в портал)."""
    return examination_svc.get_examination_session_by_access_token(db, access_token)


@router.get("/examination/sessions/{session_id}", response_model=ExaminationSessionOut)
def get_examination_session(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    return examination_svc.get_examination_session(db, session_id)


@router.get("/examination/scenarios/{scenario_id}/questions", response_model=list[ExaminationQuestionOut])
def get_examination_scenario_questions(
    scenario_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> list[ExaminationQuestionOut]:
    return examination_svc.list_scenario_questions(db, scenario_id)


@router.get("/examination/sessions/{session_id}/current-question", response_model=ExaminationQuestionOut | None)
def get_examination_current_question(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationQuestionOut | None:
    return examination_svc.get_current_question(db, session_id)


@router.post("/examination/sessions/{session_id}/consent", response_model=ExaminationSessionOut)
def post_examination_consent(
    session_id: str,
    body: ExaminationConsentBody,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    return examination_svc.post_consent(db, session_id, body)


@router.post("/examination/sessions/{session_id}/hr/release-consent-block", response_model=ExaminationSessionOut)
def post_examination_hr_release_consent(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    """Снятие блока после отказа от согласия (MVP: без проверки роли)."""
    return examination_svc.hr_release_consent_block(db, session_id)


@router.post("/examination/sessions/{session_id}/intro/done", response_model=ExaminationSessionOut)
def post_examination_intro_done(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    return examination_svc.post_intro_done(db, session_id, ExaminationIntroDoneBody())


@router.post("/examination/sessions/{session_id}/answer", response_model=ExaminationSessionOut)
def post_examination_answer(
    session_id: str,
    body: ExaminationAnswerBody,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    return examination_svc.post_answer(db, session_id, body)


@router.get("/examination/sessions/{session_id}/protocol", response_model=ExaminationProtocolOut)
def get_examination_protocol(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationProtocolOut:
    return examination_svc.build_protocol(db, session_id)


@router.post("/examination/sessions/{session_id}/complete", response_model=ExaminationSessionOut)
def post_examination_complete(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    return examination_svc.complete_examination_session(db, session_id)


@router.post("/examination/telegram/bindings", response_model=TelegramBindingOut)
def post_examination_telegram_binding(
    body: TelegramBindingCreate,
    db: Annotated[Session, Depends(get_db)],
) -> TelegramBindingOut:
    """Привязать chat_id Telegram к client_id + employee_id (HR / интеграция)."""
    d = examination_svc.upsert_telegram_binding(db, body.client_id, body.employee_id, body.telegram_chat_id)
    return TelegramBindingOut(**d)
