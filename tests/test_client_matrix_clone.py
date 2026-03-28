"""Клонирование глобальных матриц компетенций/KPI в организацию."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("SQLITE_PATH", ":memory:")

from app.db import Base  # noqa: E402
from skill_assessment.infrastructure import db_models  # noqa: F401, E402 — регистрация таблиц
from skill_assessment.services.client_matrix_clone import clone_global_matrices_to_client  # noqa: E402
from skill_assessment.services.competency_seed import ensure_competency_matrix_seed  # noqa: E402
from skill_assessment.services.kpi_seed import ensure_kpi_matrix_seed  # noqa: E402


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, future=True)
    s = Sess()
    try:
        ensure_competency_matrix_seed(s)
        ensure_kpi_matrix_seed(s)
        s.commit()
        yield s
    finally:
        s.close()


def test_clone_creates_client_scoped_rows(db_session: Session) -> None:
    client_id = uuid.uuid4().hex[:32]
    out = clone_global_matrices_to_client(db_session, client_id)
    db_session.commit()

    assert out.get("skipped") is False
    assert out["competency"]["versions"] == 1
    assert out["competency"]["matrix_rows"] > 0
    assert out["kpi"]["versions"] == 1
    assert out["kpi"]["matrix_rows"] > 0

    out2 = clone_global_matrices_to_client(db_session, client_id)
    db_session.commit()
    assert out2.get("skipped") is True


def test_clone_second_client_independent(db_session: Session) -> None:
    c1 = uuid.uuid4().hex[:32]
    c2 = uuid.uuid4().hex[:32]
    clone_global_matrices_to_client(db_session, c1)
    clone_global_matrices_to_client(db_session, c2)
    db_session.commit()

    from skill_assessment.infrastructure.db_models import CompetencyCatalogVersionRow

    n = (
        db_session.query(CompetencyCatalogVersionRow)
        .filter(CompetencyCatalogVersionRow.client_id.in_((c1, c2)))
        .count()
    )
    assert n == 2
