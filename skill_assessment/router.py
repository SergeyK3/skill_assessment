# route: /api/skill-assessment/* | file: skill_assessment/router.py
"""HTTP routes for skill assessment."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.excel_export import xlsx_file_response
from skill_assessment.domain.entities import (
    AssessmentSession,
    AssessmentSessionStatus,
    Part1TurnRole,
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
    DocsSurveySlotManualUpdate,
    DocsSurveyTelegramOut,
    ManagerAssessmentPageOut,
    ManagerAssessmentSubmitOut,
    ManagerRatingsBulk,
    Part2AdditionalCasesRequest,
    Part2CasesHrOut,
    Part2CasesPublicOut,
    Part2CasesSubmit,
    Part1DocsChecklistOut,
    Part1DocsChecklistSave,
    Part1TurnCreate,
    LlmPostSttBlacklistCreate,
    LlmPostSttBlacklistOut,
    LlmPostSttBlacklistPatch,
    Part1TurnOut,
    Part1TurnsAppend,
    SessionCancelBody,
    SessionCreate,
    SessionPhaseUpdate,
    SessionReportHrOut,
    SessionReportPublicOut,
    SkillEvaluationReferenceListOut,
    SkillAssessmentResultOut,
    SkillDomainOut,
    SkillOut,
    SkillResultCreate,
)
from skill_assessment.services import assessment_service as svc
from skill_assessment.services import catalog_views as catalog_views_svc
from skill_assessment.services import catalog_version_publish as cv_publish_svc
from skill_assessment.services import regulation_catalog_build as reg_cat_build_svc
from skill_assessment.services import classifier_import as classifier_import_svc
from skill_assessment.services import manager_assessment as manager_assessment_svc
from skill_assessment.services import part1_docs_checklist as part1_docs_svc
from skill_assessment.services import part2_case as part2_case_svc
from skill_assessment.services import part1_service as part1_svc
from skill_assessment.services import evaluation_reference_table as eval_ref_svc
from skill_assessment.services import report_service as report_svc
from skill_assessment.services import stt_service as stt_svc
from skill_assessment.services import llm_post_stt_blacklist as llm_bl_svc
from skill_assessment.services import docs_survey_notify
from skill_assessment.services import examination_service as examination_svc
from skill_assessment.env import PLUGIN_ENV_FILE, load_plugin_env
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
from skill_assessment.infrastructure.db_models import (
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    CompetencySkillDefinitionRow,
    KpiCatalogVersionRow,
    KpiDefinitionRow,
    KpiMatrixRow,
)

router = APIRouter(prefix="/skill-assessment", tags=["skill-assessment"])

_static = Path(__file__).resolve().parent / "static"

_HTML_NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


class CompetencyMatrixPatchBody(BaseModel):
    position_code: str | None = Field(default=None, min_length=1, max_length=64)
    department_code: str | None = Field(default=None, min_length=1, max_length=32)
    skill_rank: int | None = Field(default=None, ge=1)
    skill_code: str | None = Field(default=None, min_length=1, max_length=64)
    is_active: bool | None = None


class KpiMatrixPatchBody(BaseModel):
    position_code: str | None = Field(default=None, min_length=1, max_length=64)
    department_code: str | None = Field(default=None, min_length=1, max_length=32)
    kpi_rank: int | None = Field(default=None, ge=1)
    kpi_code: str | None = Field(default=None, min_length=1, max_length=64)
    is_active: bool | None = None


class ActivateGlobalCatalogBody(BaseModel):
    effective_from: date | None = Field(default=None, description="Дата начала действия новой версии (по умолчанию сегодня)")
    set_replaces_link: bool = True


class BuildCatalogFromRegulationsBody(BaseModel):
    """Сборка новой глобальной версии из актуальных регламентов (черновик по умолчанию)."""

    status: str = Field(default="draft", description="draft | active | archived (лучше draft, затем activate-global)")
    version_code: str | None = Field(default=None, max_length=64, description="Если не задан — сгенерировать уникальный")


class CatalogVersionMetaPatchBody(BaseModel):
    title: str | None = Field(default=None, max_length=512)
    notes: str | None = None
    source_regulation_code: str | None = Field(default=None, max_length=64)
    source_regulation_version_no: str | None = Field(default=None, max_length=16)
    status: str | None = Field(default=None, description="draft | active | archived")


def _html_response(path: Path, missing_detail: str) -> FileResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail=missing_detail)
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=_HTML_NO_CACHE)


def _ensure_global_competency_row(db: Session, row_id: str) -> CompetencyMatrixRow:
    row = db.get(CompetencyMatrixRow, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="competency_matrix_row_not_found")
    version = db.get(CompetencyCatalogVersionRow, row.version_id)
    if version is None or version.client_id is not None:
        raise HTTPException(status_code=400, detail="competency_matrix_row_not_global")
    return row


def _ensure_global_kpi_row(db: Session, row_id: str) -> KpiMatrixRow:
    row = db.get(KpiMatrixRow, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="kpi_matrix_row_not_found")
    version = db.get(KpiCatalogVersionRow, row.version_id)
    if version is None or version.client_id is not None:
        raise HTTPException(status_code=400, detail="kpi_matrix_row_not_global")
    return row


def _ensure_client_competency_row(db: Session, row_id: str, client_id: str) -> CompetencyMatrixRow:
    row = db.get(CompetencyMatrixRow, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="competency_matrix_row_not_found")
    version = db.get(CompetencyCatalogVersionRow, row.version_id)
    if version is None or version.client_id != client_id:
        raise HTTPException(status_code=400, detail="competency_matrix_row_not_for_client")
    return row


def _ensure_client_kpi_row(db: Session, row_id: str, client_id: str) -> KpiMatrixRow:
    row = db.get(KpiMatrixRow, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="kpi_matrix_row_not_found")
    version = db.get(KpiCatalogVersionRow, row.version_id)
    if version is None or version.client_id != client_id:
        raise HTTPException(status_code=400, detail="kpi_matrix_row_not_for_client")
    return row


def _contains_filter(value: Any, needle: str) -> bool:
    if not needle:
        return True
    return needle in str(value or "").strip().lower()


def _filter_competency_export_rows(
    rows: list[dict[str, Any]],
    *,
    version: str = "",
    position: str = "",
    department: str = "",
    rank: str = "",
    skill_code: str = "",
    title: str = "",
    active: bool | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _contains_filter(row.get("version_code"), version):
            continue
        if not _contains_filter(row.get("position_code"), position):
            continue
        if not _contains_filter(row.get("department_code"), department):
            continue
        if not _contains_filter(row.get("skill_rank"), rank):
            continue
        if not _contains_filter(row.get("skill_code"), skill_code):
            continue
        if not _contains_filter(row.get("skill_title_ru"), title):
            continue
        if active is not None and bool(row.get("is_active")) is not active:
            continue
        out.append(row)
    return out


def _filter_kpi_export_rows(
    rows: list[dict[str, Any]],
    *,
    version: str = "",
    position: str = "",
    department: str = "",
    rank: str = "",
    kpi_code: str = "",
    title: str = "",
    unit: str = "",
    period: str = "",
    target: str = "",
    active: bool | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _contains_filter(row.get("version_code"), version):
            continue
        if not _contains_filter(row.get("position_code"), position):
            continue
        if not _contains_filter(row.get("department_code"), department):
            continue
        if not _contains_filter(row.get("kpi_rank"), rank):
            continue
        if not _contains_filter(row.get("kpi_code"), kpi_code):
            continue
        if not _contains_filter(row.get("kpi_title_ru"), title):
            continue
        if not _contains_filter(row.get("unit"), unit):
            continue
        if not _contains_filter(row.get("period_type"), period):
            continue
        if not _contains_filter(row.get("default_target"), target):
            continue
        if active is not None and bool(row.get("is_active")) is not active:
            continue
        out.append(row)
    return out


@router.get("/health")
def skill_assessment_health() -> dict:
    """Liveness for the skill-assessment plugin."""
    return {"status": "ok", "module": "skill_assessment"}


@router.get("/admin/llm-post-stt-blacklist", response_model=list[LlmPostSttBlacklistOut])
def list_llm_post_stt_blacklist(
    db: Annotated[Session, Depends(get_db)],
) -> list[LlmPostSttBlacklistOut]:
    """Список правил чёрного списка после STT (управление без отдельной админ-роли — при необходимости закрыть прокси/ACL)."""
    rows = llm_bl_svc.list_blacklist(db)
    return [LlmPostSttBlacklistOut.model_validate(r) for r in rows]


@router.post("/admin/llm-post-stt-blacklist", response_model=LlmPostSttBlacklistOut)
def create_llm_post_stt_blacklist(
    body: LlmPostSttBlacklistCreate,
    db: Annotated[Session, Depends(get_db)],
) -> LlmPostSttBlacklistOut:
    row = llm_bl_svc.create_blacklist_row(
        db,
        pattern=body.pattern,
        match_mode=body.match_mode,
        description=body.description,
        is_active=body.is_active,
    )
    return LlmPostSttBlacklistOut.model_validate(row)


@router.patch("/admin/llm-post-stt-blacklist/{row_id}", response_model=LlmPostSttBlacklistOut)
def patch_llm_post_stt_blacklist(
    row_id: str,
    body: LlmPostSttBlacklistPatch,
    db: Annotated[Session, Depends(get_db)],
) -> LlmPostSttBlacklistOut:
    row = llm_bl_svc.set_blacklist_active(db, row_id, body.is_active)
    return LlmPostSttBlacklistOut.model_validate(row)


@router.delete("/admin/llm-post-stt-blacklist/{row_id}")
def delete_llm_post_stt_blacklist(
    row_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    llm_bl_svc.delete_blacklist_row(db, row_id)
    return {"ok": True}


@router.get("/telegram/debug")
def skill_assessment_telegram_debug() -> dict[str, Any]:
    """Диагностика: прочитан ли .env, включён ли polling, не висит ли webhook (без вывода токена)."""
    import httpx

    from skill_assessment import telegram_runtime as tg_rt

    load_plugin_env(override=False)
    raw = os.getenv("TELEGRAM_ENABLE_POLLING", "")
    poll = raw.strip().lower() in ("1", "true", "yes")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    webhook: dict[str, Any] | None = None
    if token:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=15.0)
        webhook = r.json()
    return {
        "env_path": str(PLUGIN_ENV_FILE),
        "env_file_exists": PLUGIN_ENV_FILE.is_file(),
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


def _catalog_version_statuses(include_archived_versions: bool) -> tuple[str, ...] | None:
    """None = без фильтра по статусу версии каталога; иначе только перечисленные."""
    return None if include_archived_versions else ("active",)


@router.get("/catalog/versions/competency", include_in_schema=True)
def list_competency_catalog_versions(
    db: Annotated[Session, Depends(get_db)],
    global_only: Annotated[bool, Query(description="Только глобальные версии (client_id IS NULL)")] = False,
    client_id: Annotated[str | None, Query(description="Версии локального каталога организации")] = None,
    status: Annotated[str | None, Query(description="Фильтр: active, archived, draft (одно значение)")] = None,
) -> list[dict[str, Any]]:
    st: tuple[str, ...] | None = (status,) if (status or "").strip() else None
    return cv_publish_svc.list_competency_catalog_versions(
        db, global_only=global_only, client_id=client_id, status_in=st
    )


@router.get("/catalog/versions/kpi", include_in_schema=True)
def list_kpi_catalog_versions(
    db: Annotated[Session, Depends(get_db)],
    global_only: Annotated[bool, Query()] = False,
    client_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    st: tuple[str, ...] | None = (status,) if (status or "").strip() else None
    return cv_publish_svc.list_kpi_catalog_versions(
        db, global_only=global_only, client_id=client_id, status_in=st
    )


@router.patch("/catalog/versions/competency/{version_id}", include_in_schema=True)
def patch_competency_catalog_version(
    version_id: str,
    body: CatalogVersionMetaPatchBody,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    v = db.get(CompetencyCatalogVersionRow, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="competency_catalog_version_not_found")
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        v.title = data["title"]
    if "notes" in data:
        v.notes = data["notes"]
    if "source_regulation_code" in data:
        v.source_regulation_code = data["source_regulation_code"]
    if "source_regulation_version_no" in data:
        v.source_regulation_version_no = data["source_regulation_version_no"]
    if "status" in data and data["status"] is not None:
        if data["status"] not in ("draft", "active", "archived"):
            raise HTTPException(status_code=422, detail="invalid_catalog_status")
        v.status = data["status"]
    db.commit()
    db.refresh(v)
    return cv_publish_svc.competency_catalog_version_to_dict(v)


@router.patch("/catalog/versions/kpi/{version_id}", include_in_schema=True)
def patch_kpi_catalog_version(
    version_id: str,
    body: CatalogVersionMetaPatchBody,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    v = db.get(KpiCatalogVersionRow, version_id)
    if v is None:
        raise HTTPException(status_code=404, detail="kpi_catalog_version_not_found")
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        v.title = data["title"]
    if "notes" in data:
        v.notes = data["notes"]
    if "source_regulation_code" in data:
        v.source_regulation_code = data["source_regulation_code"]
    if "source_regulation_version_no" in data:
        v.source_regulation_version_no = data["source_regulation_version_no"]
    if "status" in data and data["status"] is not None:
        if data["status"] not in ("draft", "active", "archived"):
            raise HTTPException(status_code=422, detail="invalid_catalog_status")
        v.status = data["status"]
    db.commit()
    db.refresh(v)
    return cv_publish_svc.kpi_catalog_version_to_dict(v)


@router.post("/catalog/versions/competency/{version_id}/activate-global", include_in_schema=True)
def activate_global_competency_catalog(
    version_id: str,
    body: ActivateGlobalCatalogBody,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    try:
        out = cv_publish_svc.activate_global_competency_catalog_version(
            db,
            version_id,
            effective_from=body.effective_from,
            set_replaces_link=body.set_replaces_link,
        )
        db.commit()
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/catalog/versions/kpi/{version_id}/activate-global", include_in_schema=True)
def activate_global_kpi_catalog(
    version_id: str,
    body: ActivateGlobalCatalogBody,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    try:
        out = cv_publish_svc.activate_global_kpi_catalog_version(
            db,
            version_id,
            effective_from=body.effective_from,
            set_replaces_link=body.set_replaces_link,
        )
        db.commit()
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/catalog/build-from-regulations/competency", include_in_schema=True)
def post_build_competency_catalog_from_regulations(
    body: BuildCatalogFromRegulationsBody,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """
    Новая глобальная версия матрицы навыков: строки из markdown-таблиц в тексте
    актуальных глобальных регламентов (поля ckp_full, goal_summary, notes).
    """
    try:
        out = reg_cat_build_svc.build_global_competency_catalog_from_regulations(
            db,
            status=body.status,
            version_code=body.version_code,
        )
        db.commit()
        return out
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/catalog/build-from-regulations/kpi", include_in_schema=True)
def post_build_kpi_catalog_from_regulations(
    body: BuildCatalogFromRegulationsBody,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Новая глобальная версия матрицы KPI из связей regulation_kpis + kpi_templates."""
    try:
        out = reg_cat_build_svc.build_global_kpi_catalog_from_regulations(
            db,
            status=body.status,
            version_code=body.version_code,
        )
        db.commit()
        return out
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/catalog/competency-matrix", include_in_schema=True)
def get_catalog_competency_matrix(
    db: Annotated[Session, Depends(get_db)],
    global_only: Annotated[
        bool,
        Query(
            description="Только глобальные шаблоны (версия каталога без client_id), без копий организаций",
        ),
    ] = False,
    client_id: Annotated[
        str | None,
        Query(
            description="Только строки каталога выбранной организации (локальная копия матрицы)",
        ),
    ] = None,
    include_archived_versions: Annotated[
        bool,
        Query(
            description="Включить строки из draft/archived версий каталога (по умолчанию только active)",
        ),
    ] = False,
) -> list[dict[str, Any]]:
    """Плоская матрица ключевых навыков: версия × должность × подразделение × ранг."""
    cid = (client_id or "").strip() or None
    return catalog_views_svc.list_competency_matrix_rows(
        db,
        global_only=global_only,
        client_id=cid,
        version_statuses=_catalog_version_statuses(include_archived_versions),
    )


@router.get("/catalog/competency-matrix/export/excel", include_in_schema=True)
def export_catalog_competency_matrix_excel(
    db: Annotated[Session, Depends(get_db)],
    version: str = "",
    position: str = "",
    department: str = "",
    rank: str = "",
    skill_code: str = "",
    title: str = "",
    active: bool | None = None,
    client_id: Annotated[
        str | None,
        Query(description="Выгрузка локальной матрицы организации; без параметра — глобальная"),
    ] = None,
    include_archived_versions: Annotated[bool, Query()] = False,
):
    cid = (client_id or "").strip() or None
    source = catalog_views_svc.list_competency_matrix_rows(
        db,
        global_only=not bool(cid),
        client_id=cid,
        version_statuses=_catalog_version_statuses(include_archived_versions),
    )
    rows = _filter_competency_export_rows(
        source,
        version=version.strip().lower(),
        position=position.strip().lower(),
        department=department.strip().lower(),
        rank=rank.strip().lower(),
        skill_code=skill_code.strip().lower(),
        title=title.strip().lower(),
        active=active,
    )
    fn = "local_competency_matrix.xlsx" if cid else "global_competency_matrix.xlsx"
    return xlsx_file_response(
        download_name=fn,
        sheet_title="skills_matrix",
        headers=[
            "version_code",
            "catalog_status",
            "catalog_effective_from",
            "catalog_effective_to",
            "source_regulation_code",
            "position_code",
            "department_code",
            "skill_rank",
            "skill_code",
            "skill_title_ru",
            "is_active",
        ],
        rows=[
            [
                row.get("version_code"),
                row.get("catalog_status"),
                row.get("catalog_effective_from"),
                row.get("catalog_effective_to"),
                row.get("source_regulation_code"),
                row.get("position_code"),
                row.get("department_code"),
                row.get("skill_rank"),
                row.get("skill_code"),
                row.get("skill_title_ru"),
                row.get("is_active"),
            ]
            for row in rows
        ],
    )


@router.patch("/catalog/competency-matrix/{row_id}", include_in_schema=True)
def patch_catalog_competency_matrix_row(
    row_id: str,
    body: CompetencyMatrixPatchBody,
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[
        str | None,
        Query(description="Если задан — правка строки локального каталога этой организации"),
    ] = None,
) -> dict[str, Any]:
    cid = (client_id or "").strip() or None
    row = _ensure_client_competency_row(db, row_id, cid) if cid else _ensure_global_competency_row(db, row_id)
    data = body.model_dump(exclude_unset=True)
    if "position_code" in data:
        row.position_code = data["position_code"]
    if "department_code" in data:
        row.department_code = data["department_code"]
    if "skill_rank" in data:
        row.skill_rank = data["skill_rank"]
    if "is_active" in data:
        row.is_active = data["is_active"]
    if "skill_code" in data:
        skill = db.scalar(
            select(CompetencySkillDefinitionRow).where(
                CompetencySkillDefinitionRow.skill_code == data["skill_code"]
            )
        )
        if skill is None:
            raise HTTPException(status_code=404, detail="competency_skill_definition_not_found")
        row.skill_definition_id = skill.id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="competency_matrix_row_conflict")
    db.refresh(row)
    return catalog_views_svc.competency_matrix_row_to_dict(row)


@router.delete("/catalog/competency-matrix/{row_id}", status_code=204, include_in_schema=True)
def delete_catalog_competency_matrix_row(
    row_id: str,
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[
        str | None,
        Query(description="Если задан — удаление строки локального каталога этой организации"),
    ] = None,
) -> None:
    cid = (client_id or "").strip() or None
    row = _ensure_client_competency_row(db, row_id, cid) if cid else _ensure_global_competency_row(db, row_id)
    db.delete(row)
    db.commit()


@router.get("/catalog/kpi-matrix", include_in_schema=True)
def get_catalog_kpi_matrix(
    db: Annotated[Session, Depends(get_db)],
    global_only: Annotated[
        bool,
        Query(
            description="Только глобальные шаблоны (версия каталога без client_id), без копий организаций",
        ),
    ] = False,
    client_id: Annotated[
        str | None,
        Query(
            description="Только строки каталога выбранной организации (локальная копия матрицы KPI)",
        ),
    ] = None,
    include_archived_versions: Annotated[bool, Query()] = False,
) -> list[dict[str, Any]]:
    """Плоская матрица KPI: версия × должность × подразделение × приоритет (kpi_rank)."""
    cid = (client_id or "").strip() or None
    return catalog_views_svc.list_kpi_matrix_rows(
        db,
        global_only=global_only,
        client_id=cid,
        version_statuses=_catalog_version_statuses(include_archived_versions),
    )


@router.get("/catalog/kpi-matrix/export/excel", include_in_schema=True)
def export_catalog_kpi_matrix_excel(
    db: Annotated[Session, Depends(get_db)],
    version: str = "",
    position: str = "",
    department: str = "",
    rank: str = "",
    kpi_code: str = "",
    title: str = "",
    unit: str = "",
    period: str = "",
    target: str = "",
    active: bool | None = None,
    client_id: Annotated[
        str | None,
        Query(description="Выгрузка локальной матрицы KPI организации; без параметра — глобальная"),
    ] = None,
    include_archived_versions: Annotated[bool, Query()] = False,
):
    cid = (client_id or "").strip() or None
    source = catalog_views_svc.list_kpi_matrix_rows(
        db,
        global_only=not bool(cid),
        client_id=cid,
        version_statuses=_catalog_version_statuses(include_archived_versions),
    )
    rows = _filter_kpi_export_rows(
        source,
        version=version.strip().lower(),
        position=position.strip().lower(),
        department=department.strip().lower(),
        rank=rank.strip().lower(),
        kpi_code=kpi_code.strip().lower(),
        title=title.strip().lower(),
        unit=unit.strip().lower(),
        period=period.strip().lower(),
        target=target.strip().lower(),
        active=active,
    )
    fn = "local_kpi_matrix.xlsx" if cid else "global_kpi_matrix.xlsx"
    return xlsx_file_response(
        download_name=fn,
        sheet_title="kpi_matrix",
        headers=[
            "version_code",
            "catalog_status",
            "catalog_effective_from",
            "catalog_effective_to",
            "source_regulation_code",
            "position_code",
            "department_code",
            "kpi_rank",
            "kpi_code",
            "kpi_title_ru",
            "unit",
            "period_type",
            "default_target",
            "is_active",
        ],
        rows=[
            [
                row.get("version_code"),
                row.get("catalog_status"),
                row.get("catalog_effective_from"),
                row.get("catalog_effective_to"),
                row.get("source_regulation_code"),
                row.get("position_code"),
                row.get("department_code"),
                row.get("kpi_rank"),
                row.get("kpi_code"),
                row.get("kpi_title_ru"),
                row.get("unit"),
                row.get("period_type"),
                row.get("default_target"),
                row.get("is_active"),
            ]
            for row in rows
        ],
    )


@router.patch("/catalog/kpi-matrix/{row_id}", include_in_schema=True)
def patch_catalog_kpi_matrix_row(
    row_id: str,
    body: KpiMatrixPatchBody,
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[
        str | None,
        Query(description="Если задан — правка строки локального каталога KPI этой организации"),
    ] = None,
) -> dict[str, Any]:
    cid = (client_id or "").strip() or None
    row = _ensure_client_kpi_row(db, row_id, cid) if cid else _ensure_global_kpi_row(db, row_id)
    data = body.model_dump(exclude_unset=True)
    if "position_code" in data:
        row.position_code = data["position_code"]
    if "department_code" in data:
        row.department_code = data["department_code"]
    if "kpi_rank" in data:
        row.kpi_rank = data["kpi_rank"]
    if "is_active" in data:
        row.is_active = data["is_active"]
    if "kpi_code" in data:
        kpi = db.scalar(
            select(KpiDefinitionRow).where(
                KpiDefinitionRow.kpi_code == data["kpi_code"]
            )
        )
        if kpi is None:
            raise HTTPException(status_code=404, detail="kpi_definition_not_found")
        row.kpi_definition_id = kpi.id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="kpi_matrix_row_conflict")
    db.refresh(row)
    return catalog_views_svc.kpi_matrix_row_to_dict(row)


@router.delete("/catalog/kpi-matrix/{row_id}", status_code=204, include_in_schema=True)
def delete_catalog_kpi_matrix_row(
    row_id: str,
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[
        str | None,
        Query(description="Если задан — удаление строки локального каталога KPI этой организации"),
    ] = None,
) -> None:
    cid = (client_id or "").strip() or None
    row = _ensure_client_kpi_row(db, row_id, cid) if cid else _ensure_global_kpi_row(db, row_id)
    db.delete(row)
    db.commit()


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
    docs_survey_slot_filter: str | None = Query(
        default=None,
        description="Слот опроса по документам (Telegram): today | upcoming | has_slot",
    ),
    q: str | None = Query(default=None, description="Поиск по client_id, id сессии, employee_id (подстрока)"),
    offset: int = Query(default=0, ge=0, le=100_000),
    limit: int = Query(default=50, ge=1, le=500),
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
        docs_survey_slot_filter=docs_survey_slot_filter,
        q=q,
        offset=offset,
        limit=limit,
    )
    return AssessmentSessionListOut(items=items, total=total)


@router.get("/sessions/skill-evaluations-reference", response_model=SkillEvaluationReferenceListOut)
def get_skill_evaluations_reference(
    db: Annotated[Session, Depends(get_db)],
    client_id: str = Query(..., min_length=1, description="Организация (clients.id)"),
    session_limit: int = Query(100, ge=1, le=300, description="Сколько последних сессий развернуть в строки навыков"),
    session_offset: int = Query(0, ge=0, le=100_000),
) -> SkillEvaluationReferenceListOut:
    """Сводная таблица: по каждому навыку в сессии — должность, сотрудник, итог Part1+Part2 (0–3 и %), без Part3."""
    return eval_ref_svc.list_skill_evaluation_reference(
        db,
        client_id,
        session_limit=session_limit,
        session_offset=session_offset,
    )


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


@router.patch("/sessions/{session_id}/docs-survey-slot", response_model=AssessmentSessionOut)
def patch_docs_survey_slot(
    session_id: str,
    body: DocsSurveySlotManualUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> AssessmentSessionOut:
    """Вручную задать дату и время слота опроса по документам (локальное время в зоне ``DOCS_SURVEY_LOCAL_TIMEZONE``)."""
    return svc.set_docs_survey_slot_manual(db, session_id, body)


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


@router.post("/sessions/{session_id}/part1/audio", response_model=list[Part1TurnOut])
async def post_part1_audio(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(..., description="Аудио (например webm из MediaRecorder)"),
) -> list[Part1TurnOut]:
    """STT → одна реплика ``user`` в Part 1 (тот же контракт, что POST …/part1/turns)."""
    raw = await file.read()
    fname = file.filename or "audio.webm"
    try:
        text = stt_svc.transcribe_audio_bytes(raw, filename=fname, content_type=file.content_type)
    except ValueError as exc:
        code = str(exc)
        if code == "empty_audio":
            raise HTTPException(status_code=400, detail="part1_empty_audio") from exc
        if code == "audio_too_large":
            raise HTTPException(status_code=413, detail="part1_audio_too_large") from exc
        raise
    except stt_svc.SttConfigurationError as exc:
        raise HTTPException(status_code=503, detail="part1_stt_not_configured") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="part1_stt_failed") from exc

    llm_bl_svc.assert_user_text_allowed_after_stt(db, text)

    body = Part1TurnsAppend(
        turns=[Part1TurnCreate(role=Part1TurnRole.USER, text=text)],
    )
    return part1_svc.append_part1_turns(db, session_id, body)


@router.get("/sessions/{session_id}/part1/docs-checklist", response_model=Part1DocsChecklistOut)
def get_part1_docs_checklist(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> Part1DocsChecklistOut:
    """Чек-лист опроса по служебным / должностным документам (Part 1)."""
    return part1_docs_svc.get_part1_docs_checklist(db, session_id)


@router.post("/sessions/{session_id}/part1/docs-checklist", response_model=Part1DocsChecklistOut)
def post_part1_docs_checklist(
    session_id: str,
    body: Part1DocsChecklistSave,
    db: Annotated[Session, Depends(get_db)],
) -> Part1DocsChecklistOut:
    """Сохранить ответы; при complete=True — завершить и перейти к part2 (если фаза была part1)."""
    return part1_docs_svc.save_part1_docs_checklist(db, session_id, body)


@router.get("/public/part1-docs-checklist", response_model=Part1DocsChecklistOut)
def public_get_part1_docs_checklist(
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен из ссылки для сотрудника"),
) -> Part1DocsChecklistOut:
    """Чек-лист по токену сессии (без авторизации HR): для личной страницы сотрудника."""
    return part1_docs_svc.get_part1_docs_checklist_by_token(db, token)


@router.post("/public/part1-docs-checklist", response_model=Part1DocsChecklistOut)
def public_post_part1_docs_checklist(
    body: Part1DocsChecklistSave,
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен из ссылки для сотрудника"),
) -> Part1DocsChecklistOut:
    """Сохранить ответы по токену (личная страница сотрудника)."""
    return part1_docs_svc.save_part1_docs_checklist_by_token(db, token, body)


@router.get("/sessions/{session_id}/case", response_model=CaseTextOut)
def get_session_case(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    skill_id: str = Query(..., description="Навык для генерации кейса (Part 2)"),
) -> CaseTextOut:
    """Текст кейса по сессии и навыку: LLM по ответам Part 1, с шаблонным fallback."""
    return part2_case_svc.get_session_case(db, session_id, skill_id)


@router.get("/public/part2-case", response_model=CaseTextOut)
def public_get_part2_case(
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен личной страницы сотрудника"),
    skill_id: str | None = Query(default=None, description="Навык для генерации кейса (если не задан — первый навык)"),
) -> CaseTextOut:
    """Публичный текст кейса для сотрудника по токену сессии."""
    return part2_case_svc.get_public_case(db, token, skill_id)


@router.get("/sessions/{session_id}/part2-cases", response_model=Part2CasesHrOut)
def get_session_part2_cases(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> Part2CasesHrOut:
    """Набор кейсов Part 2, сгенерированный для общей сессии оценки."""
    return part2_case_svc.get_session_cases(db, session_id)


@router.post("/sessions/{session_id}/part2-cases", response_model=Part2CasesHrOut)
def post_session_part2_cases(
    session_id: str,
    body: Part2CasesSubmit,
    db: Annotated[Session, Depends(get_db)],
) -> Part2CasesHrOut:
    """Сохранить ответы на все кейсы Part 2 и выполнить оценку ИИ."""
    return part2_case_svc.submit_session_cases(db, session_id, body)


@router.post("/sessions/{session_id}/part2-cases/additional", response_model=Part2CasesHrOut)
def post_session_part2_additional_cases(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    body: Part2AdditionalCasesRequest | None = None,
) -> Part2CasesHrOut:
    """Добавить дополнительные кейсы по непокрытым навыкам для выбранной сессии."""
    return part2_case_svc.offer_additional_session_cases(db, session_id, body)


@router.get("/public/part2-cases", response_model=Part2CasesPublicOut)
def public_get_part2_cases(
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен личной страницы сотрудника"),
) -> Part2CasesPublicOut:
    """Публичный набор кейсов Part 2 для сотрудника по токену."""
    return part2_case_svc.get_public_cases(db, token)


@router.post("/public/part2-cases", response_model=Part2CasesPublicOut)
def public_post_part2_cases(
    body: Part2CasesSubmit,
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен личной страницы сотрудника"),
) -> Part2CasesPublicOut:
    """Сохранить ответы сотрудника на все кейсы Part 2 по токену и выполнить оценку ИИ."""
    return part2_case_svc.submit_public_cases(db, token, body)


@router.get("/public/manager-assessment", response_model=ManagerAssessmentPageOut)
def public_get_manager_assessment(
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен персональной страницы руководителя"),
) -> ManagerAssessmentPageOut:
    """Публичная страница оценки руководителя по токену."""
    return manager_assessment_svc.get_manager_assessment_page(db, token)


@router.post("/public/manager-assessment", response_model=ManagerAssessmentSubmitOut)
def public_post_manager_assessment(
    body: ManagerRatingsBulk,
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен персональной страницы руководителя"),
) -> ManagerAssessmentSubmitOut:
    """Сохранить оценки руководителя по токену и завершить сессию."""
    return manager_assessment_svc.submit_manager_assessment_by_token(db, token, body)


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


@router.get("/ui/exam-protocols", include_in_schema=True)
def skill_assessment_ui_exam_protocols() -> FileResponse:
    """Таблица протоколов экзаменов по регламентам для отдела кадров (просмотр / скачивание HTML)."""
    return _html_response(_static / "exam_protocols.html", "skill_assessment_exam_protocols_static_missing")


@router.get("/ui/part1-docs-checklist", include_in_schema=True)
def skill_assessment_ui_part1_docs_checklist() -> FileResponse:
    """Личная страница чек-листа по служебным документам (токен в query: ``?token=``)."""
    return _html_response(_static / "part1_docs_employee.html", "skill_assessment_part1_docs_employee_static_missing")


@router.get("/ui/part2-case", include_in_schema=True)
def skill_assessment_ui_part2_case() -> FileResponse:
    """Личная страница кейса (Part 2) для сотрудника, токен в query: ``?token=``."""
    return _html_response(_static / "part2_case_employee.html", "skill_assessment_part2_case_static_missing")


@router.get("/ui/manager-assessment", include_in_schema=True)
def skill_assessment_ui_manager_assessment() -> FileResponse:
    """Личная страница оценки руководителя (Part 3), токен в query: ``?token=``."""
    return _html_response(_static / "manager_assessment.html", "skill_assessment_manager_assessment_static_missing")


@router.get("/ui/part1_docs-checklist", include_in_schema=False)
def skill_assessment_ui_part1_docs_checklist_underscore_alias(request: Request) -> RedirectResponse:
    """Старый/ошибочный URL с подчёркиванием — редирект на канонический путь с дефисами."""
    q = request.url.query
    dest = "/api/skill-assessment/ui/part1-docs-checklist" + ("?" + q if q else "")
    return RedirectResponse(url=dest, status_code=307)


def _docs_survey_telegram_background_enabled() -> bool:
    return (os.getenv("SKILL_ASSESSMENT_DOCS_SURVEY_TELEGRAM_BACKGROUND") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@router.post("/sessions/{session_id}/start", response_model=AssessmentSessionStartOut)
def post_session_start(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> AssessmentSessionStartOut:
    svc.start_session(db, session_id)
    if _docs_survey_telegram_background_enabled():
        background_tasks.add_task(docs_survey_notify.send_docs_survey_assignment_notice_task, session_id)
        tg = DocsSurveyTelegramOut(
            queued=True,
            sent=False,
            skipped_reason="telegram_send_queued_background",
        )
    else:
        tg = docs_survey_notify.send_docs_survey_assignment_notice(db, session_id)
    # После синхронной отправки в Telegram обновляются поля сессии; в фоновом режиме поля появятся чуть позже.
    fresh = svc.get_session(db, session_id)
    return AssessmentSessionStartOut(**fresh.model_dump(), docs_survey_telegram=tg)


@router.post("/sessions/{session_id}/resend-docs-survey-telegram", response_model=DocsSurveyTelegramOut)
def post_resend_docs_survey_telegram(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> DocsSurveyTelegramOut:
    """
    Повторно отправить в Telegram то же уведомление, что при старте этапа 1 (опрос по документам + ПДн).

    Нужно, если сотрудник не получил сообщение или сессия «застряла» без вызова /start.
    Сбрасывает отслеживание согласия ПДн к awaiting_first (как при первой успешной отправке).
    """
    snap = svc.get_session(db, session_id)
    if snap.status in (AssessmentSessionStatus.COMPLETED, AssessmentSessionStatus.CANCELLED):
        raise HTTPException(status_code=400, detail="session_completed_or_cancelled_no_resend")
    if _docs_survey_telegram_background_enabled():
        background_tasks.add_task(docs_survey_notify.send_docs_survey_assignment_notice_task, session_id)
        return DocsSurveyTelegramOut(
            queued=True,
            sent=False,
            skipped_reason="telegram_send_queued_background",
        )
    return docs_survey_notify.send_docs_survey_assignment_notice(db, session_id)


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


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session_route(session_id: str, db: Annotated[Session, Depends(get_db)]) -> None:
    """Удалить запись о сессии оценки из базы (без восстановления). Для кадровых служб — убрать лишнюю строку из истории."""
    svc.delete_session(db, session_id)


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


@router.get("/sessions/{session_id}/report", response_model=SessionReportHrOut)
def get_session_report(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> SessionReportHrOut:
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


@router.get("/public/report", response_model=SessionReportPublicOut)
def public_get_session_report(
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен личной страницы сотрудника"),
) -> SessionReportPublicOut:
    """Публичный JSON-отчёт общей сессии по токену сотрудника."""
    return report_svc.build_public_session_report(db, token)


@router.get("/public/report/html", response_class=HTMLResponse)
def public_get_session_report_html(
    db: Annotated[Session, Depends(get_db)],
    token: str = Query(..., min_length=16, description="Токен личной страницы сотрудника"),
) -> HTMLResponse:
    """Публичный HTML-протокол общей сессии по токену сотрудника."""
    rep = report_svc.build_public_session_report(db, token)
    return HTMLResponse(report_svc.render_session_report_html(rep))


# --- Экзамен по регламентам / ДИ (отдельная сессия, MVP: regulation_v1) ---


@router.get("/display-config", include_in_schema=True)
def get_skill_assessment_display_config() -> dict[str, str]:
    """IANA-зона для отображения дат в UI (совпадает с ``DOCS_SURVEY_LOCAL_TIMEZONE``)."""
    from skill_assessment.services.docs_survey_time import survey_zone_name

    return {"local_timezone": survey_zone_name()}


@router.post("/examination/sessions", response_model=ExaminationSessionOut)
def post_examination_session(
    body: ExaminationSessionCreate,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    """Создать назначение на экзамен (сценарий regulation_v1; вопросы из ядра по KPI или общий набор)."""
    return examination_svc.create_examination_session(db, body)


@router.get("/examination/sessions", response_model=list[ExaminationSessionOut])
def get_examination_sessions(
    db: Annotated[Session, Depends(get_db)],
    client_id: str | None = Query(default=None),
    employee_id: str | None = Query(default=None),
    status: str | None = Query(
        default=None,
        description="Фильтр по статусу сессии экзамена, например completed",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    enrich: bool = Query(
        default=False,
        description="Добавить ФИО/должность/подразделение и средний балл (кадры, список протоколов)",
    ),
) -> list[ExaminationSessionOut]:
    return examination_svc.list_examination_sessions(
        db, client_id, employee_id, limit, status, enrich=enrich
    )


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


@router.post("/examination/sessions/{session_id}/hr/release-regulation-block", response_model=ExaminationSessionOut)
def post_examination_hr_release_regulation(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    """После загрузки регламента/KPI в ядре — снять блок «нет регламента» и снова подобрать вопросы."""
    return examination_svc.hr_release_regulation_block(db, session_id)


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


@router.get("/examination/sessions/{session_id}/protocol/html", response_class=HTMLResponse)
def get_examination_protocol_html(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    download: bool = Query(False, description="Скачать как файл .html"),
) -> HTMLResponse:
    """Просмотр протокола экзамена в HTML; отдел кадров — таблица со ссылками на этот URL."""
    proto = examination_svc.build_protocol(db, session_id)
    html = examination_svc.render_examination_protocol_html(proto)
    headers: dict[str, str] = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="exam-protocol-{session_id[:8]}.html"'
    return HTMLResponse(content=html, headers=headers)


@router.post("/examination/sessions/{session_id}/complete", response_model=ExaminationSessionOut)
def post_examination_complete(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> ExaminationSessionOut:
    return examination_svc.complete_examination_session(db, session_id)


@router.delete("/examination/sessions/{session_id}")
def delete_examination_session_route(
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    """Удалить сессию экзамена и ответы (кадры; MVP без проверки роли)."""
    examination_svc.delete_examination_session(db, session_id)
    return {"ok": True}


@router.post("/examination/telegram/bindings", response_model=TelegramBindingOut)
def post_examination_telegram_binding(
    body: TelegramBindingCreate,
    db: Annotated[Session, Depends(get_db)],
) -> TelegramBindingOut:
    """Привязать chat_id Telegram к client_id + employee_id (HR / интеграция)."""
    d = examination_svc.upsert_telegram_binding(db, body.client_id, body.employee_id, body.telegram_chat_id)
    return TelegramBindingOut(**d)
