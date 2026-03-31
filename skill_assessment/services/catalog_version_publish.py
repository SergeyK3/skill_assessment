# route: publish | file: skill_assessment/services/catalog_version_publish.py
"""
Публикация глобальной версии каталога (навыки или KPI): архив предшественников, даты effective_* , published_at.

Работает только для строк с client_id IS NULL (шаблоны).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import CompetencyCatalogVersionRow, KpiCatalogVersionRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_before(d: date) -> date:
    return d - timedelta(days=1)


def activate_global_competency_catalog_version(
    db: Session,
    version_id: str,
    *,
    effective_from: date | None = None,
    set_replaces_link: bool = True,
) -> dict[str, Any]:
    """Все прочие глобальные competency-версии со status=active → archived + effective_to; цель → active."""
    eff = effective_from or date.today()
    target = db.get(CompetencyCatalogVersionRow, version_id)
    if not target:
        raise ValueError("competency_catalog_version_not_found")
    if target.client_id is not None:
        raise ValueError("competency_version_not_global")

    prev_active = list(
        db.scalars(
            select(CompetencyCatalogVersionRow).where(
                CompetencyCatalogVersionRow.client_id.is_(None),
                CompetencyCatalogVersionRow.status == "active",
                CompetencyCatalogVersionRow.id != version_id,
            )
        ).all()
    )
    boundary = _day_before(eff)
    for v in prev_active:
        v.status = "archived"
        if v.effective_to is None:
            v.effective_to = boundary
    if set_replaces_link and prev_active:
        prev_sorted = sorted(prev_active, key=lambda x: (x.updated_at or x.created_at), reverse=True)
        target.replaces_version_id = prev_sorted[0].id
    target.status = "active"
    target.effective_from = eff
    target.effective_to = None
    target.published_at = _utcnow()
    db.flush()
    return {
        "activated_id": target.id,
        "version_code": target.version_code,
        "effective_from": eff.isoformat(),
        "archived_ids": [v.id for v in prev_active],
    }


def activate_global_kpi_catalog_version(
    db: Session,
    version_id: str,
    *,
    effective_from: date | None = None,
    set_replaces_link: bool = True,
) -> dict[str, Any]:
    eff = effective_from or date.today()
    target = db.get(KpiCatalogVersionRow, version_id)
    if not target:
        raise ValueError("kpi_catalog_version_not_found")
    if target.client_id is not None:
        raise ValueError("kpi_version_not_global")

    prev_active = list(
        db.scalars(
            select(KpiCatalogVersionRow).where(
                KpiCatalogVersionRow.client_id.is_(None),
                KpiCatalogVersionRow.status == "active",
                KpiCatalogVersionRow.id != version_id,
            )
        ).all()
    )
    boundary = _day_before(eff)
    for v in prev_active:
        v.status = "archived"
        if v.effective_to is None:
            v.effective_to = boundary
    if set_replaces_link and prev_active:
        prev_sorted = sorted(prev_active, key=lambda x: (x.updated_at or x.created_at), reverse=True)
        target.replaces_version_id = prev_sorted[0].id
    target.status = "active"
    target.effective_from = eff
    target.effective_to = None
    target.published_at = _utcnow()
    db.flush()
    return {
        "activated_id": target.id,
        "version_code": target.version_code,
        "effective_from": eff.isoformat(),
        "archived_ids": [v.id for v in prev_active],
    }


def competency_catalog_version_to_dict(v: CompetencyCatalogVersionRow) -> dict[str, Any]:
    return {
        "id": v.id,
        "client_id": v.client_id,
        "version_code": v.version_code,
        "title": v.title,
        "status": v.status,
        "effective_from": v.effective_from.isoformat() if v.effective_from else None,
        "effective_to": v.effective_to.isoformat() if v.effective_to else None,
        "notes": v.notes,
        "source_regulation_code": v.source_regulation_code,
        "source_regulation_version_no": v.source_regulation_version_no,
        "replaces_version_id": v.replaces_version_id,
        "published_at": v.published_at.isoformat() if v.published_at else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


def kpi_catalog_version_to_dict(v: KpiCatalogVersionRow) -> dict[str, Any]:
    return {
        "id": v.id,
        "client_id": v.client_id,
        "version_code": v.version_code,
        "title": v.title,
        "status": v.status,
        "effective_from": v.effective_from.isoformat() if v.effective_from else None,
        "effective_to": v.effective_to.isoformat() if v.effective_to else None,
        "notes": v.notes,
        "source_regulation_code": v.source_regulation_code,
        "source_regulation_version_no": v.source_regulation_version_no,
        "replaces_version_id": v.replaces_version_id,
        "published_at": v.published_at.isoformat() if v.published_at else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


def list_competency_catalog_versions(
    db: Session,
    *,
    global_only: bool = False,
    client_id: str | None = None,
    status_in: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    stmt = select(CompetencyCatalogVersionRow)
    cid = (client_id or "").strip() or None
    if cid:
        stmt = stmt.where(CompetencyCatalogVersionRow.client_id == cid)
    elif global_only:
        stmt = stmt.where(CompetencyCatalogVersionRow.client_id.is_(None))
    if status_in:
        stmt = stmt.where(CompetencyCatalogVersionRow.status.in_(status_in))
    stmt = stmt.order_by(CompetencyCatalogVersionRow.created_at.desc())
    return [competency_catalog_version_to_dict(v) for v in db.scalars(stmt).all()]


def list_kpi_catalog_versions(
    db: Session,
    *,
    global_only: bool = False,
    client_id: str | None = None,
    status_in: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    stmt = select(KpiCatalogVersionRow)
    cid = (client_id or "").strip() or None
    if cid:
        stmt = stmt.where(KpiCatalogVersionRow.client_id == cid)
    elif global_only:
        stmt = stmt.where(KpiCatalogVersionRow.client_id.is_(None))
    if status_in:
        stmt = stmt.where(KpiCatalogVersionRow.status.in_(status_in))
    stmt = stmt.order_by(KpiCatalogVersionRow.created_at.desc())
    return [kpi_catalog_version_to_dict(v) for v in db.scalars(stmt).all()]
