# route: build-from-regs | file: skill_assessment/services/regulation_catalog_build.py
"""
Создание новых глобальных версий каталогов (навыки / KPI) из данных глобальных регламентов.

Навыки: markdown-таблицы в тексте регламентов (``app.services.regulation_matrix_extract``).
KPI: таблица ``regulation_kpis`` + подписи из ``kpi_templates``.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.regulation_matrix_extract import (
    extract_all_regulation_table_skills,
    list_kpi_rows_from_regulation_registry,
)
from skill_assessment.infrastructure.db_models import (
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    CompetencySkillDefinitionRow,
    KpiCatalogVersionRow,
    KpiDefinitionRow,
    KpiMatrixRow,
)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _uniq_version_code(db: Session, prefix: str) -> str:
    """Уникальный ``version_code`` среди обеих таблиц версий (разные UQ)."""
    for _ in range(20):
        cand = f"{prefix}-{secrets.token_hex(3)}"
        cc = db.scalar(select(CompetencyCatalogVersionRow).where(CompetencyCatalogVersionRow.version_code == cand))
        kc = db.scalar(select(KpiCatalogVersionRow).where(KpiCatalogVersionRow.version_code == cand))
        if cc is None and kc is None:
            return cand
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _alloc_skill_ranks(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Уникальные ранги внутри каждой пары (должность, подразделение-функция)."""
    by_pd: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pd[(r["position_code"], r["department_code"])].append(r)
    out: list[tuple[dict[str, Any], int]] = []
    for _key, lst in by_pd.items():
        lst.sort(
            key=lambda x: (
                int(x["skill_rank"]) if str(x.get("skill_rank", "")).isdigit() else 999,
                x.get("skill_title_ru") or "",
            )
        )
        used: set[int] = set()
        for r in lst:
            raw = r.get("skill_rank")
            rank = int(raw) if str(raw).isdigit() else None
            if rank is None or rank < 1 or rank in used:
                rank = max(used, default=0) + 1
            while rank in used:
                rank += 1
            used.add(rank)
            out.append((r, rank))
    return out


def _skill_code_unique(db: Session, regulation_code: str, title: str, table_index: int, rank: int) -> str:
    base = hashlib.sha256(
        f"{regulation_code}|{title}|{table_index}|{rank}".encode("utf-8")
    ).hexdigest()[:16]
    code = f"C_REG_{base}"
    n = 0
    while True:
        cand = code if n == 0 else f"{code[: max(1, 64 - len(str(n)) - 1)]}_{n}"
        exists = db.scalar(select(CompetencySkillDefinitionRow.id).where(CompetencySkillDefinitionRow.skill_code == cand))
        if exists is None:
            return cand[:64]
        n += 1


def build_global_competency_catalog_from_regulations(
    db: Session,
    *,
    status: str = "draft",
    version_code: str | None = None,
) -> dict[str, Any]:
    """
    Новая глобальная версия каталога компетенций из таблиц в тексте актуальных регламентов.
    ``status``: draft | active | archived (рекомендуется draft, затем POST …/activate-global).
    """
    st = status if status in ("draft", "active", "archived") else "draft"
    extracted = extract_all_regulation_table_skills(db)
    vid = str(uuid.uuid4())
    vcode = (version_code or "").strip() or _uniq_version_code(db, f"reg-skills-{_utc_today().strftime('%Y%m%d')}")
    if db.scalar(select(CompetencyCatalogVersionRow).where(CompetencyCatalogVersionRow.version_code == vcode)):
        raise ValueError("competency_version_code_already_exists")

    v = CompetencyCatalogVersionRow(
        id=vid,
        client_id=None,
        version_code=vcode,
        title="Навыки из таблиц в тексте глобальных регламентов",
        status=st,
        effective_from=_utc_today() if st == "active" else None,
        effective_to=None,
        notes=f"built_from=regulation_markdown_tables extracted_rows={len(extracted)}",
        source_regulation_code=None,
        source_regulation_version_no=None,
        replaces_version_id=None,
        published_at=datetime.now(timezone.utc) if st == "active" else None,
    )
    db.add(v)
    db.flush()

    n_defs = n_matrix = 0
    for row, rank in _alloc_skill_ranks(extracted):
        title = (row.get("skill_title_ru") or "").strip()
        if not title:
            continue
        reg_code = row.get("regulation_code") or ""
        tbl_idx = int(row.get("table_index") or 0)
        scode = (row.get("skill_code_text") or "").strip()
        if scode:
            existing = db.scalar(
                select(CompetencySkillDefinitionRow).where(
                    CompetencySkillDefinitionRow.skill_code == scode,
                    CompetencySkillDefinitionRow.client_id.is_(None),
                )
            )
            if existing:
                sid = existing.id
            else:
                sid = str(uuid.uuid4())
                db.add(
                    CompetencySkillDefinitionRow(
                        id=sid,
                        client_id=None,
                        skill_code=scode[:64],
                        title_ru=title[:512],
                        description=None,
                        is_active=True,
                    )
                )
                n_defs += 1
        else:
            sid = str(uuid.uuid4())
            db.add(
                CompetencySkillDefinitionRow(
                    id=sid,
                    client_id=None,
                    skill_code=_skill_code_unique(db, reg_code, title, tbl_idx, rank),
                    title_ru=title[:512],
                    description=None,
                    is_active=True,
                )
            )
            n_defs += 1
        db.flush()
        db.add(
            CompetencyMatrixRow(
                id=str(uuid.uuid4()),
                version_id=vid,
                position_code=row["position_code"],
                department_code=row["department_code"],
                skill_definition_id=sid,
                skill_rank=rank,
                is_active=True,
            )
        )
        n_matrix += 1

    db.flush()
    return {
        "catalog_kind": "competency",
        "version_id": vid,
        "version_code": vcode,
        "status": st,
        "extracted_input_rows": len(extracted),
        "skill_definitions_created": n_defs,
        "matrix_rows_created": n_matrix,
    }


def _ensure_global_kpi_definition(
    db: Session,
    *,
    kpi_code: str,
    title_ru: str,
    unit: str,
    period_type: str,
    default_target: float | None,
) -> str:
    row = db.scalar(
        select(KpiDefinitionRow).where(
            KpiDefinitionRow.kpi_code == kpi_code,
            KpiDefinitionRow.client_id.is_(None),
        )
    )
    if row:
        return row.id
    kid = str(uuid.uuid4())
    db.add(
        KpiDefinitionRow(
            id=kid,
            client_id=None,
            kpi_code=kpi_code[:64],
            title_ru=(title_ru or kpi_code)[:512],
            unit=(unit or "%")[:32],
            period_type=(period_type or "month")[:16],
            default_target=default_target,
            description=None,
            is_active=True,
        )
    )
    db.flush()
    return kid


def build_global_kpi_catalog_from_regulations(
    db: Session,
    *,
    status: str = "draft",
    version_code: str | None = None,
) -> dict[str, Any]:
    st = status if status in ("draft", "active", "archived") else "draft"
    raw = list_kpi_rows_from_regulation_registry(db)
    vid = str(uuid.uuid4())
    vcode = (version_code or "").strip() or _uniq_version_code(db, f"reg-kpi-{_utc_today().strftime('%Y%m%d')}")
    if db.scalar(select(KpiCatalogVersionRow).where(KpiCatalogVersionRow.version_code == vcode)):
        raise ValueError("kpi_version_code_already_exists")

    v = KpiCatalogVersionRow(
        id=vid,
        client_id=None,
        version_code=vcode,
        title="KPI из regulation_kpis (глобальные регламенты)",
        status=st,
        effective_from=_utc_today() if st == "active" else None,
        effective_to=None,
        notes=f"built_from=regulation_kpis rows={len(raw)}",
        source_regulation_code=None,
        source_regulation_version_no=None,
        replaces_version_id=None,
        published_at=datetime.now(timezone.utc) if st == "active" else None,
    )
    db.add(v)
    db.flush()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in raw:
        grouped[(r["position_code"], r["department_code"])].append(r)

    n_matrix = 0
    n_new_defs = 0
    for (_pos, _dept), items in sorted(grouped.items()):
        items.sort(key=lambda x: (x.get("regulation_code") or "", x.get("kpi_code") or ""))
        seen_codes: set[str] = set()
        rank = 0
        for item in items:
            kc = (item.get("kpi_code") or "").strip()
            if not kc:
                continue
            if kc in seen_codes:
                continue
            seen_codes.add(kc)
            rank += 1
            before_id = db.scalar(
                select(KpiDefinitionRow.id).where(
                    KpiDefinitionRow.kpi_code == kc,
                    KpiDefinitionRow.client_id.is_(None),
                )
            )
            kid = _ensure_global_kpi_definition(
                db,
                kpi_code=kc,
                title_ru=item.get("kpi_title_ru") or kc,
                unit=item.get("unit") or "%",
                period_type=item.get("period_type") or "month",
                default_target=item.get("default_target"),
            )
            if before_id is None:
                n_new_defs += 1
            db.add(
                KpiMatrixRow(
                    id=str(uuid.uuid4()),
                    version_id=vid,
                    position_code=item["position_code"],
                    department_code=item["department_code"],
                    kpi_definition_id=kid,
                    kpi_rank=rank,
                    is_active=True,
                )
            )
            n_matrix += 1
    db.flush()
    return {
        "catalog_kind": "kpi",
        "version_id": vid,
        "version_code": vcode,
        "status": st,
        "regulation_kpi_input_rows": len(raw),
        "kpi_matrix_rows_created": n_matrix,
        "new_global_kpi_definitions": n_new_defs,
    }
