# route: /api/skill-assessment/* | file: skill_assessment/router.py
"""HTTP routes for skill assessment."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
    AssessmentSessionOut,
    CaseTextOut,
    ClassifierImportOut,
    ManagerRatingsBulk,
    Part1TurnOut,
    Part1TurnsAppend,
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

router = APIRouter(prefix="/skill-assessment", tags=["skill-assessment"])

_static = Path(__file__).resolve().parent / "static"

_HTML_NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


def _html_response(path: Path, missing_detail: str) -> FileResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail=missing_detail)
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_HTML_NO_CACHE)


@router.get("/health")
def skill_assessment_health() -> dict:
    """Liveness for the skill-assessment plugin."""
    return {"status": "ok", "module": "skill_assessment"}


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


@router.get("/sessions", response_model=list[AssessmentSessionOut])
def get_sessions(
    db: Annotated[Session, Depends(get_db)],
    client_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[AssessmentSessionOut]:
    return svc.list_sessions(db, client_id, limit)


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


@router.post("/sessions/{session_id}/start", response_model=AssessmentSessionOut)
def post_session_start(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.start_session(db, session_id)


@router.post("/sessions/{session_id}/complete", response_model=AssessmentSessionOut)
def post_session_complete(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    return svc.complete_session(db, session_id)


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
