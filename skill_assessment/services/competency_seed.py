# route: seed | file: skill_assessment/services/competency_seed.py
"""
Засев матрицы компетенций: версия каталога + справочник навыков + связи (должность + подразделение).

Источник данных: ``skill_assessment.data.top20_position_skills`` + соответствие
должность → код функции/подразделения (как в ``app/seed.py`` POSITION_DEPT_TYPE_SEEDS).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skill_assessment.data.top20_position_skills import TOP20_POSITION_SKILL_ROWS
from skill_assessment.infrastructure.db_models import (
    CompetencyCatalogVersionRow,
    CompetencyMatrixRow,
    CompetencySkillDefinitionRow,
)

_log = logging.getLogger(__name__)

# Совпадает с typical_infrastructure/app/seed.py POSITION_DEPT_TYPE_SEEDS (position_code → dept_type_code)
POSITION_TO_DEPARTMENT_CODE: dict[str, str] = {
    "ADM_DIRECTOR": "ADM",
    "ADM_ZAMADM": "ADM",
    "ADM_SYS_ADMIN": "ADM",
    "INFO_SYSTEM_SUPPORT": "ADM",
    "HR_MANAGER": "HR",
    "HR_HEAD": "HR",
    "HR_RECRUITER": "HR",
    "HR_GENERALIST": "HR",
    "MKT_MANAGER": "MKT",
    "LEADGEN_SPECIALIST": "LEAD",
    "SALES_MANAGER": "SALES",
    "SALES_TEAM_LEAD": "SALES",
    "ACC_ACCOUNTANT": "ACC",
    "ACC_MATERIAL_ACCOUNTANT": "ACC",
    "ACC_CHIEF_ACCOUNTANT": "ACC",
    "PROD_SUPERVISOR": "PROD",
    "PROD_TECH_DIR": "PROD",
    "QUAL_SPECIALIST": "QUAL",
    "QUAL_HEAD": "QUAL",
    "PR_SPECIALIST": "PR",
}

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

VERSION_CODE = "2025-01-v1"
VERSION_TITLE = "Матрица компетенций (топ-20 должностей, демо)"


def _det_uuid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def _skill_code_from_title(title_ru: str) -> str:
    h = hashlib.sha256(title_ru.strip().encode("utf-8")).hexdigest()[:14]
    return f"C_{h}"


def ensure_competency_matrix_seed(db: Session) -> None:
    """
    Идемпотентно создаёт версию, определения навыков и строки матрицы.
    Если в ``sa_competency_matrix`` уже есть строки — выходим.
    """
    n_existing = db.scalar(select(func.count()).select_from(CompetencyMatrixRow))
    if n_existing and n_existing > 0:
        _log.debug("competency_seed: sa_competency_matrix уже заполнена, пропуск")
        return

    version_id = _det_uuid(f"sa_competency_catalog_versions:{VERSION_CODE}")
    v = db.get(CompetencyCatalogVersionRow, version_id)
    if v is None:
        v = CompetencyCatalogVersionRow(
            id=version_id,
            client_id=None,
            version_code=VERSION_CODE,
            title=VERSION_TITLE,
            status="active",
            effective_from=date(2025, 1, 1),
            effective_to=None,
            notes="Демо-засев из top20_position_skills; поле is_active у строк и навыков = true.",
        )
        db.add(v)
        db.flush()

    # Справочник навыков: одна строка на уникальный title_ru
    title_to_id: dict[str, str] = {}
    all_titles: set[str] = set()
    for _code, _title, skills in TOP20_POSITION_SKILL_ROWS:
        for s in skills:
            all_titles.add(s.strip())

    for t in sorted(all_titles):
        sid = _det_uuid(f"sa_competency_skill_definitions:{t}")
        title_to_id[t] = sid
        code = _skill_code_from_title(t)
        existing = db.get(CompetencySkillDefinitionRow, sid)
        if existing is None:
            db.add(
                CompetencySkillDefinitionRow(
                    id=sid,
                    client_id=None,
                    skill_code=code,
                    title_ru=t,
                    description=None,
                    is_active=True,
                )
            )
    db.flush()

    for position_code, position_name_ru, skills in TOP20_POSITION_SKILL_ROWS:
        dept = POSITION_TO_DEPARTMENT_CODE.get(position_code)
        if not dept:
            _log.warning("competency_seed: нет department_code для %s — пропуск", position_code)
            continue
        for rank, skill_title in enumerate(skills, start=1):
            st = skill_title.strip()
            sid = title_to_id[st]
            mid = _det_uuid(f"sa_competency_matrix:{version_id}:{position_code}:{dept}:{rank}")
            db.add(
                CompetencyMatrixRow(
                    id=mid,
                    version_id=version_id,
                    position_code=position_code,
                    department_code=dept,
                    skill_definition_id=sid,
                    skill_rank=rank,
                    is_active=True,
                )
            )

    total_matrix = sum(len(s) for _, _, s in TOP20_POSITION_SKILL_ROWS)
    _log.info(
        "competency_seed: version=%s, уникальных навыков=%s, строк матрицы=%s",
        VERSION_CODE,
        len(title_to_id),
        total_matrix,
    )


def write_postgres_seed_sql(out_path: Path | None = None) -> Path:
    """
    Генерирует SQL (PostgreSQL) для создания таблиц и вставки того же содержимого, что и засев SQLite.
    Удобно для ``docker compose`` + initdb.d или ручного ``psql``.
    """
    if out_path is None:
        # repo/exports от каталога skill_assessment (родитель pyproject)
        out_path = Path(__file__).resolve().parents[3] / "exports" / "competency_postgres_seed.sql"

    version_id = _det_uuid(f"sa_competency_catalog_versions:{VERSION_CODE}")

    title_to_id: dict[str, str] = {}
    all_titles: set[str] = set()
    for _c, _t, skills in TOP20_POSITION_SKILL_ROWS:
        for s in skills:
            all_titles.add(s.strip())
    for t in sorted(all_titles):
        title_to_id[t] = _det_uuid(f"sa_competency_skill_definitions:{t}")

    def esc(s: str) -> str:
        return s.replace("'", "''")

    lines: list[str] = [
        "-- Автоген: skill_assessment.services.competency_seed.write_postgres_seed_sql",
        "-- Таблицы совпадают по имени с SQLite (ORM), типы — PostgreSQL.",
        "",
        """CREATE TABLE IF NOT EXISTS sa_competency_catalog_versions (
    id VARCHAR(36) PRIMARY KEY,
    client_id VARCHAR(32),
    version_code VARCHAR(64) NOT NULL,
    title VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    effective_from DATE,
    effective_to DATE,
    notes TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_sa_ccv_version_code UNIQUE (version_code)
);""",
        """CREATE TABLE IF NOT EXISTS sa_competency_skill_definitions (
    id VARCHAR(36) PRIMARY KEY,
    client_id VARCHAR(32),
    skill_code VARCHAR(64) NOT NULL,
    title_ru VARCHAR(512) NOT NULL,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_sa_csd_skill_code UNIQUE (skill_code)
);""",
        """CREATE TABLE IF NOT EXISTS sa_competency_matrix (
    id VARCHAR(36) PRIMARY KEY,
    version_id VARCHAR(36) NOT NULL REFERENCES sa_competency_catalog_versions(id) ON DELETE CASCADE,
    position_code VARCHAR(64) NOT NULL,
    department_code VARCHAR(32) NOT NULL,
    skill_definition_id VARCHAR(36) NOT NULL REFERENCES sa_competency_skill_definitions(id) ON DELETE CASCADE,
    skill_rank INTEGER NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_sa_cm_version_pos_dept_rank UNIQUE (version_id, position_code, department_code, skill_rank)
);""",
        "CREATE INDEX IF NOT EXISTS ix_sa_cm_version_pos_dept ON sa_competency_matrix (version_id, position_code, department_code);",
        "",
        "BEGIN;",
    ]

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines.append(
        f"INSERT INTO sa_competency_catalog_versions (id, client_id, version_code, title, status, effective_from, effective_to, notes, created_at, updated_at) "
        f"VALUES ('{version_id}', NULL, '{VERSION_CODE}', '{esc(VERSION_TITLE)}', 'active', '2025-01-01', NULL, "
        f"'{esc('Демо-засев; is_active=true')}', '{now}', '{now}') "
        f"ON CONFLICT (id) DO NOTHING;"
    )

    for t in sorted(all_titles):
        sid = title_to_id[t]
        code = _skill_code_from_title(t)
        lines.append(
            f"INSERT INTO sa_competency_skill_definitions (id, client_id, skill_code, title_ru, description, is_active, created_at, updated_at) "
            f"VALUES ('{sid}', NULL, '{esc(code)}', '{esc(t)}', NULL, TRUE, '{now}', '{now}') "
            f"ON CONFLICT (id) DO NOTHING;"
        )

    for position_code, _pn, skills in TOP20_POSITION_SKILL_ROWS:
        dept = POSITION_TO_DEPARTMENT_CODE.get(position_code)
        if not dept:
            continue
        for rank, skill_title in enumerate(skills, start=1):
            st = skill_title.strip()
            sid = title_to_id[st]
            mid = _det_uuid(f"sa_competency_matrix:{version_id}:{position_code}:{dept}:{rank}")
            lines.append(
                f"INSERT INTO sa_competency_matrix (id, version_id, position_code, department_code, skill_definition_id, skill_rank, is_active, created_at, updated_at) "
                f"VALUES ('{mid}', '{version_id}', '{esc(position_code)}', '{esc(dept)}', '{sid}', {rank}, TRUE, '{now}', '{now}') "
                f"ON CONFLICT (id) DO NOTHING;"
            )

    lines.append("COMMIT;")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log.info("competency_seed: записан %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = write_postgres_seed_sql()
    print(p)
