# route: clone | file: skill_assessment/services/client_matrix_clone.py
"""
Копирование глобальных матриц компетенций и KPI (``client_id IS NULL``) в рабочие
копии для организации (``client_id`` задан).

Нужно для сценария: в глобальных справочниках — шаблоны по типовым должностям и
«отделениям» (код функции/типа подразделения); при развёртывании клиента —
локальные строки, которые можно «допилить» без затрагивания глобального слоя.

Уникальность ``kpi_code`` / ``skill_code`` в БД глобальная — у клонов коды
получают суффикс ``_<short_client_id>``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skill_assessment.infrastructure.db_models import (
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    CompetencySkillDefinitionRow,
    KpiCatalogVersionRow,
    KpiDefinitionRow,
    KpiMatrixRow,
)
from skill_assessment.services.competency_seed import ensure_competency_matrix_seed
from skill_assessment.services.kpi_seed import ensure_kpi_matrix_seed

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _client_suffix(client_id: str) -> str:
    return client_id.replace("-", "")[:8]


def _uniq_code(base: str, suffix: str, max_len: int = 64) -> str:
    s = f"{base}_{suffix}"
    return s if len(s) <= max_len else f"{base[: max(1, max_len - 1 - len(suffix))]}_{suffix}"


def clone_global_matrices_to_client(db: Session, client_id: str) -> dict[str, Any]:
    """
    Идемпотентно: блок компетенций и блок KPI клонируются независимо, если для клиента
    ещё нет соответствующей версии каталога.

    Перед клонированием поднимаем глобальный засев (если таблицы пустые).
    """
    ensure_competency_matrix_seed(db)
    ensure_kpi_matrix_seed(db)
    db.flush()

    existing_cc = (
        db.scalar(
            select(func.count())
            .select_from(CompetencyCatalogVersionRow)
            .where(CompetencyCatalogVersionRow.client_id == client_id)
        )
        or 0
    )
    existing_kc = (
        db.scalar(
            select(func.count())
            .select_from(KpiCatalogVersionRow)
            .where(KpiCatalogVersionRow.client_id == client_id)
        )
        or 0
    )
    if existing_cc > 0 and existing_kc > 0:
        _log.info("client_matrix_clone: каталоги уже есть для client_id=%s — пропуск", client_id)
        return {"skipped": True, "reason": "already_present", "client_id": client_id}

    suf = _client_suffix(client_id)
    do_comp = existing_cc == 0
    do_kpi = existing_kc == 0

    # --- Competency ---
    glob_cv = db.scalar(
        select(CompetencyCatalogVersionRow)
        .where(
            CompetencyCatalogVersionRow.client_id.is_(None),
            CompetencyCatalogVersionRow.status == "active",
        )
        .order_by(CompetencyCatalogVersionRow.created_at.desc())
        .limit(1)
    )
    comp_stats: dict[str, int] = {"versions": 0, "skill_definitions": 0, "matrix_rows": 0}
    if do_comp and glob_cv:
        new_vid = str(uuid.uuid4())
        new_vcode = _uniq_code(glob_cv.version_code, suf)
        db.add(
            CompetencyCatalogVersionRow(
                id=new_vid,
                client_id=client_id,
                version_code=new_vcode,
                title=glob_cv.title + " (копия для организации)",
                status="active",
                effective_from=glob_cv.effective_from,
                effective_to=glob_cv.effective_to,
                notes=(glob_cv.notes or "") + f" cloned_from_version_id={glob_cv.id}",
            )
        )
        comp_stats["versions"] = 1

        matrix_rows = db.scalars(
            select(CompetencyMatrixRow).where(CompetencyMatrixRow.version_id == glob_cv.id)
        ).all()
        old_skill_ids = {r.skill_definition_id for r in matrix_rows}
        old_to_new_skill: dict[str, str] = {}
        for sid in old_skill_ids:
            sd = db.get(CompetencySkillDefinitionRow, sid)
            if not sd or sd.client_id is not None:
                continue
            nid = str(uuid.uuid4())
            old_to_new_skill[sid] = nid
            new_sc = _uniq_code(sd.skill_code, suf)
            db.add(
                CompetencySkillDefinitionRow(
                    id=nid,
                    client_id=client_id,
                    skill_code=new_sc,
                    title_ru=sd.title_ru,
                    description=sd.description,
                    is_active=sd.is_active,
                )
            )
            comp_stats["skill_definitions"] += 1

        for mr in matrix_rows:
            n_sid = old_to_new_skill.get(mr.skill_definition_id)
            if not n_sid:
                continue
            db.add(
                CompetencyMatrixRow(
                    id=str(uuid.uuid4()),
                    version_id=new_vid,
                    position_code=mr.position_code,
                    department_code=mr.department_code,
                    skill_definition_id=n_sid,
                    skill_rank=mr.skill_rank,
                    is_active=mr.is_active,
                )
            )
            comp_stats["matrix_rows"] += 1
    elif do_comp:
        _log.warning("client_matrix_clone: нет глобальной версии компетенций — пропуск клонирования навыков")

    # --- KPI ---
    glob_kv = db.scalar(
        select(KpiCatalogVersionRow)
        .where(
            KpiCatalogVersionRow.client_id.is_(None),
            KpiCatalogVersionRow.status == "active",
        )
        .order_by(KpiCatalogVersionRow.created_at.desc())
        .limit(1)
    )
    kpi_stats: dict[str, int] = {"versions": 0, "definitions": 0, "matrix_rows": 0}
    if do_kpi and glob_kv:
        new_kvid = str(uuid.uuid4())
        new_kvcode = _uniq_code(glob_kv.version_code, suf)
        db.add(
            KpiCatalogVersionRow(
                id=new_kvid,
                client_id=client_id,
                version_code=new_kvcode,
                title=glob_kv.title + " (копия для организации)",
                status="active",
                effective_from=glob_kv.effective_from,
                effective_to=glob_kv.effective_to,
                notes=(glob_kv.notes or "") + f" cloned_from_version_id={glob_kv.id}",
            )
        )
        kpi_stats["versions"] = 1

        km_rows = db.scalars(select(KpiMatrixRow).where(KpiMatrixRow.version_id == glob_kv.id)).all()
        old_kpi_def_ids = {r.kpi_definition_id for r in km_rows}
        old_to_new_kpi: dict[str, str] = {}
        for kid in old_kpi_def_ids:
            kd = db.get(KpiDefinitionRow, kid)
            if not kd or kd.client_id is not None:
                continue
            nid = str(uuid.uuid4())
            old_to_new_kpi[kid] = nid
            new_kc = _uniq_code(kd.kpi_code, suf)
            db.add(
                KpiDefinitionRow(
                    id=nid,
                    client_id=client_id,
                    kpi_code=new_kc,
                    title_ru=kd.title_ru,
                    unit=kd.unit,
                    period_type=kd.period_type,
                    default_target=kd.default_target,
                    description=kd.description,
                    is_active=kd.is_active,
                )
            )
            kpi_stats["definitions"] += 1

        for mr in km_rows:
            n_kid = old_to_new_kpi.get(mr.kpi_definition_id)
            if not n_kid:
                continue
            db.add(
                KpiMatrixRow(
                    id=str(uuid.uuid4()),
                    version_id=new_kvid,
                    position_code=mr.position_code,
                    department_code=mr.department_code,
                    kpi_definition_id=n_kid,
                    kpi_rank=mr.kpi_rank,
                    is_active=mr.is_active,
                )
            )
            kpi_stats["matrix_rows"] += 1
    elif do_kpi:
        _log.warning("client_matrix_clone: нет глобальной версии KPI — пропуск клонирования KPI")

    _log.info(
        "client_matrix_clone: client_id=%s competency=%s kpi=%s",
        client_id,
        comp_stats,
        kpi_stats,
    )
    return {
        "skipped": not (comp_stats["versions"] or kpi_stats["versions"]),
        "client_id": client_id,
        "competency": comp_stats,
        "kpi": kpi_stats,
    }
