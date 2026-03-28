# route: seed | file: skill_assessment/services/kpi_seed.py
"""
Матрица KPI: версия каталога + определения показателей + связи (должность + подразделение + приоритет).

``kpi_rank`` = 1 — наивысший приоритет для данной должности в рамках версии и пары (position, department).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skill_assessment.data.top20_position_kpis import TOP20_POSITION_KPI_ROWS
from skill_assessment.infrastructure.db_models import (
    KpiCatalogVersionRow,
    KpiDefinitionRow,
    KpiMatrixRow,
)
from skill_assessment.services.competency_seed import POSITION_TO_DEPARTMENT_CODE

_log = logging.getLogger(__name__)

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

KPI_VERSION_CODE = "2025-01-kpi-v1"
KPI_VERSION_TITLE = "Матрица KPI (топ-20 должностей, демо)"


def _det_uuid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def ensure_kpi_matrix_seed(db: Session) -> None:
    n_existing = db.scalar(select(func.count()).select_from(KpiMatrixRow))
    if n_existing and n_existing > 0:
        _log.debug("kpi_seed: sa_kpi_matrix уже заполнена, пропуск")
        return

    version_id = _det_uuid(f"sa_kpi_catalog_versions:{KPI_VERSION_CODE}")
    if db.get(KpiCatalogVersionRow, version_id) is None:
        db.add(
            KpiCatalogVersionRow(
                id=version_id,
                client_id=None,
                version_code=KPI_VERSION_CODE,
                title=KPI_VERSION_TITLE,
                status="active",
                effective_from=date(2025, 1, 1),
                effective_to=None,
                notes="Демо-засев; is_active=true; kpi_rank — приоритет (1 — важнее).",
            )
        )
        db.flush()

    # Уникальные определения KPI по kpi_code
    seen_codes: dict[str, str] = {}
    for _pc, _pn, kpis in TOP20_POSITION_KPI_ROWS:
        for kpi_code, title_ru, unit, period_type, default_target in kpis:
            if kpi_code in seen_codes:
                continue
            kid = _det_uuid(f"sa_kpi_definitions:{kpi_code}")
            seen_codes[kpi_code] = kid
            if db.get(KpiDefinitionRow, kid) is None:
                db.add(
                    KpiDefinitionRow(
                        id=kid,
                        client_id=None,
                        kpi_code=kpi_code,
                        title_ru=title_ru,
                        unit=unit,
                        period_type=period_type,
                        default_target=default_target,
                        description=None,
                        is_active=True,
                    )
                )
    db.flush()

    total_m = 0
    for position_code, _position_name, kpis in TOP20_POSITION_KPI_ROWS:
        dept = POSITION_TO_DEPARTMENT_CODE.get(position_code)
        if not dept:
            _log.warning("kpi_seed: нет department_code для %s — пропуск", position_code)
            continue
        for rank, row in enumerate(kpis, start=1):
            kpi_code, _t, _u, _p, _dt = row
            kid = seen_codes[kpi_code]
            mid = _det_uuid(f"sa_kpi_matrix:{version_id}:{position_code}:{dept}:{rank}")
            db.add(
                KpiMatrixRow(
                    id=mid,
                    version_id=version_id,
                    position_code=position_code,
                    department_code=dept,
                    kpi_definition_id=kid,
                    kpi_rank=rank,
                    is_active=True,
                )
            )
            total_m += 1

    _log.info(
        "kpi_seed: version=%s, уникальных KPI=%s, строк матрицы=%s",
        KPI_VERSION_CODE,
        len(seen_codes),
        total_m,
    )


def write_kpi_postgres_seed_sql(out_path: Path | None = None) -> Path:
    if out_path is None:
        out_path = Path(__file__).resolve().parents[3] / "exports" / "kpi_postgres_seed.sql"

    version_id = _det_uuid(f"sa_kpi_catalog_versions:{KPI_VERSION_CODE}")

    seen_codes: dict[str, str] = {}
    for _pc, _pn, kpis in TOP20_POSITION_KPI_ROWS:
        for kpi_code, title_ru, unit, period_type, default_target in kpis:
            if kpi_code not in seen_codes:
                seen_codes[kpi_code] = _det_uuid(f"sa_kpi_definitions:{kpi_code}")

    def esc(s: str) -> str:
        return s.replace("'", "''")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def sql_float(v: float | None) -> str:
        if v is None:
            return "NULL"
        return str(v)

    lines: list[str] = [
        "-- Автоген: skill_assessment.services.kpi_seed.write_kpi_postgres_seed_sql",
        "",
        """CREATE TABLE IF NOT EXISTS sa_kpi_catalog_versions (
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
    CONSTRAINT uq_sa_kcv_version_code UNIQUE (version_code)
);""",
        """CREATE TABLE IF NOT EXISTS sa_kpi_definitions (
    id VARCHAR(36) PRIMARY KEY,
    client_id VARCHAR(32),
    kpi_code VARCHAR(64) NOT NULL,
    title_ru VARCHAR(512) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    period_type VARCHAR(16) NOT NULL,
    default_target DOUBLE PRECISION,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_sa_kd_kpi_code UNIQUE (kpi_code)
);""",
        """CREATE TABLE IF NOT EXISTS sa_kpi_matrix (
    id VARCHAR(36) PRIMARY KEY,
    version_id VARCHAR(36) NOT NULL REFERENCES sa_kpi_catalog_versions(id) ON DELETE CASCADE,
    position_code VARCHAR(64) NOT NULL,
    department_code VARCHAR(32) NOT NULL,
    kpi_definition_id VARCHAR(36) NOT NULL REFERENCES sa_kpi_definitions(id) ON DELETE CASCADE,
    kpi_rank INTEGER NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_sa_km_version_pos_dept_rank UNIQUE (version_id, position_code, department_code, kpi_rank)
);""",
        "CREATE INDEX IF NOT EXISTS ix_sa_km_version_pos_dept ON sa_kpi_matrix (version_id, position_code, department_code);",
        "",
        "BEGIN;",
    ]

    lines.append(
        f"INSERT INTO sa_kpi_catalog_versions (id, client_id, version_code, title, status, effective_from, effective_to, notes, created_at, updated_at) "
        f"VALUES ('{version_id}', NULL, '{KPI_VERSION_CODE}', '{esc(KPI_VERSION_TITLE)}', 'active', '2025-01-01', NULL, "
        f"'{esc('Демо; kpi_rank=приоритет')}', '{now}', '{now}') ON CONFLICT (id) DO NOTHING;"
    )

    for kpi_code, kid in sorted(seen_codes.items(), key=lambda x: x[0]):
        # найти метаданные по первому вхождению
        meta = None
        for _pc, _pn, kpis in TOP20_POSITION_KPI_ROWS:
            for row in kpis:
                if row[0] == kpi_code:
                    meta = row
                    break
            if meta:
                break
        assert meta is not None
        _code, title_ru, unit, period_type, default_target = meta
        lines.append(
            f"INSERT INTO sa_kpi_definitions (id, client_id, kpi_code, title_ru, unit, period_type, default_target, description, is_active, created_at, updated_at) "
            f"VALUES ('{kid}', NULL, '{esc(kpi_code)}', '{esc(title_ru)}', '{esc(unit)}', '{esc(period_type)}', {sql_float(default_target)}, NULL, TRUE, '{now}', '{now}') "
            f"ON CONFLICT (id) DO NOTHING;"
        )

    for position_code, _pn, kpis in TOP20_POSITION_KPI_ROWS:
        dept = POSITION_TO_DEPARTMENT_CODE.get(position_code)
        if not dept:
            continue
        for rank, row in enumerate(kpis, start=1):
            kpi_code = row[0]
            kid = seen_codes[kpi_code]
            mid = _det_uuid(f"sa_kpi_matrix:{version_id}:{position_code}:{dept}:{rank}")
            lines.append(
                f"INSERT INTO sa_kpi_matrix (id, version_id, position_code, department_code, kpi_definition_id, kpi_rank, is_active, created_at, updated_at) "
                f"VALUES ('{mid}', '{version_id}', '{esc(position_code)}', '{esc(dept)}', '{kid}', {rank}, TRUE, '{now}', '{now}') "
                f"ON CONFLICT (id) DO NOTHING;"
            )

    lines.append("COMMIT;")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log.info("kpi_seed: записан %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(write_kpi_postgres_seed_sql())
