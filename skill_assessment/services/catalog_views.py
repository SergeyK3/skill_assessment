# route: read-only catalog API | file: skill_assessment/services/catalog_views.py
"""Плоские выборки матриц компетенций и KPI для UI и отчётов."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import (
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    CompetencySkillDefinitionRow,
    KpiCatalogVersionRow,
    KpiDefinitionRow,
    KpiMatrixRow,
)


def competency_matrix_row_to_dict(row: CompetencyMatrixRow) -> dict[str, Any]:
    version = row.catalog_version
    skill = row.skill_definition
    return {
        "row_id": row.id,
        "version_code": version.version_code,
        "catalog_status": version.status,
        "catalog_effective_from": version.effective_from.isoformat() if version.effective_from else None,
        "catalog_effective_to": version.effective_to.isoformat() if version.effective_to else None,
        "source_regulation_code": version.source_regulation_code,
        "source_regulation_version_no": version.source_regulation_version_no,
        "position_code": row.position_code,
        "department_code": row.department_code,
        "skill_rank": row.skill_rank,
        "skill_code": skill.skill_code,
        "skill_title_ru": skill.title_ru,
        "is_active": row.is_active,
    }


def kpi_matrix_row_to_dict(row: KpiMatrixRow) -> dict[str, Any]:
    version = row.catalog_version
    kpi = row.kpi_definition
    return {
        "row_id": row.id,
        "version_code": version.version_code,
        "catalog_status": version.status,
        "catalog_effective_from": version.effective_from.isoformat() if version.effective_from else None,
        "catalog_effective_to": version.effective_to.isoformat() if version.effective_to else None,
        "source_regulation_code": version.source_regulation_code,
        "source_regulation_version_no": version.source_regulation_version_no,
        "position_code": row.position_code,
        "department_code": row.department_code,
        "kpi_rank": row.kpi_rank,
        "kpi_code": kpi.kpi_code,
        "kpi_title_ru": kpi.title_ru,
        "unit": kpi.unit,
        "period_type": kpi.period_type,
        "default_target": kpi.default_target,
        "is_active": row.is_active,
    }


def list_competency_matrix_rows(
    db: Session,
    *,
    global_only: bool = False,
    client_id: str | None = None,
    version_statuses: tuple[str, ...] | None = ("active",),
) -> list[dict[str, Any]]:
    stmt = (
        select(
            CompetencyMatrixRow,
        )
        .join(CompetencyCatalogVersionRow, CompetencyMatrixRow.version_id == CompetencyCatalogVersionRow.id)
        .join(CompetencySkillDefinitionRow, CompetencyMatrixRow.skill_definition_id == CompetencySkillDefinitionRow.id)
    )
    cid = (client_id or "").strip() or None
    if cid:
        stmt = stmt.where(CompetencyCatalogVersionRow.client_id == cid)
    elif global_only:
        stmt = stmt.where(CompetencyCatalogVersionRow.client_id.is_(None))
    if version_statuses is not None:
        stmt = stmt.where(CompetencyCatalogVersionRow.status.in_(version_statuses))
    stmt = stmt.order_by(
        CompetencyCatalogVersionRow.version_code,
        CompetencyMatrixRow.position_code,
        CompetencyMatrixRow.department_code,
        CompetencyMatrixRow.skill_rank,
    )
    rows = db.scalars(stmt).all()
    return [competency_matrix_row_to_dict(row) for row in rows]


def list_kpi_matrix_rows(
    db: Session,
    *,
    global_only: bool = False,
    client_id: str | None = None,
    version_statuses: tuple[str, ...] | None = ("active",),
) -> list[dict[str, Any]]:
    stmt = (
        select(
            KpiMatrixRow,
        )
        .join(KpiCatalogVersionRow, KpiMatrixRow.version_id == KpiCatalogVersionRow.id)
        .join(KpiDefinitionRow, KpiMatrixRow.kpi_definition_id == KpiDefinitionRow.id)
    )
    cid = (client_id or "").strip() or None
    if cid:
        stmt = stmt.where(KpiCatalogVersionRow.client_id == cid)
    elif global_only:
        stmt = stmt.where(KpiCatalogVersionRow.client_id.is_(None))
    if version_statuses is not None:
        stmt = stmt.where(KpiCatalogVersionRow.status.in_(version_statuses))
    stmt = stmt.order_by(
        KpiCatalogVersionRow.version_code,
        KpiMatrixRow.position_code,
        KpiMatrixRow.department_code,
        KpiMatrixRow.kpi_rank,
    )
    rows = db.scalars(stmt).all()
    return [kpi_matrix_row_to_dict(row) for row in rows]
