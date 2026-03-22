# route: GET /skill-assessment | file: skill_assessment/runner.py
"""
Точка входа ASGI: приложение ядра typical_infrastructure + роутер skill_assessment.

Запускать из **корня клона ядра**, чтобы пакет ``app`` был в PYTHONPATH::

    cd D:\\path\\to\\typical_infrastructure
    .venv\\Scripts\\activate
    pip install -e D:\\path\\to\\skill_assessment
    uvicorn skill_assessment.runner:app --reload --host 127.0.0.1 --port 8000

    Либо из каталога ядра: ``python -m skill_assessment`` (тот же хост/порт).

    Если все пути плагина дают 404, а ``GET /health/ready`` от ядра открывается: на Windows
    в браузере открывайте **http://127.0.0.1:8000/...**, а не ``http://localhost:8000/...``
    — ``localhost`` может уйти на другой процесс по IPv6 (``::1``), а uvicorn слушает IPv4.
    Проверка: ``netstat -ano | findstr :8000`` (несколько LISTENING — разные стеки).

Ядро в upstream не меняется; зависимость на skill_assessment в requirements ядра не добавляется.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from starlette.requests import Request

import skill_assessment.infrastructure.db_models  # noqa: F401 — таблицы на общем Base

from app.db import SessionLocal
from app.main import app

from skill_assessment.router import router as skill_assessment_router
from skill_assessment.services.taxonomy_seed import ensure_demo_taxonomy

_log = logging.getLogger("skill_assessment")


def _ensure_sa_session_phase_column(engine) -> None:
    """SQLite: добавить колонку phase к существующим БД без Alembic."""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("sa_assessment_sessions")]
    except Exception:
        return
    if "phase" in cols:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE sa_assessment_sessions ADD COLUMN phase VARCHAR(32) NOT NULL DEFAULT 'draft'"
            )
        )
    _log.info("skill_assessment: added column sa_assessment_sessions.phase (migration)")


app.include_router(skill_assessment_router, prefix="/api")

_static = Path(__file__).resolve().parent / "static"


def _redirect_skill_assessment_ui(dest: str, request: Request) -> RedirectResponse:
    """Редирект на страницу плагина с сохранением query (например client_id из ядра)."""
    q = request.url.query
    url = dest + ("?" + q if q else "")
    return RedirectResponse(url=url, status_code=302)


@app.middleware("http")
async def _skill_assessment_short_urls(request: Request, call_next):
    """Короткие URL → те же страницы под /api/skill-assessment/… (надёжнее, чем только декораторы на app)."""
    p = request.url.path
    if p in ("/skill-assessment", "/skill-assessment/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/landing", request)
    if p in ("/skill-assessment/future-demo", "/skill-assessment/future-demo/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/demo/future-scenario", request)
    if p in ("/skill-assessment/part3", "/skill-assessment/part3/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part3", request)
    if p in ("/skill-assessment/part1", "/skill-assessment/part1/"):
        return _redirect_skill_assessment_ui("/api/skill-assessment/ui/part1", request)
    return await call_next(request)


@app.on_event("startup")
def _skill_assessment_startup() -> None:
    """Гарантируем таблицы skill_assessment и демо-таксономию при пустых таблицах."""
    _log.info(
        "skill_assessment runner: плагин активен — GET /skill-assessment → /api/skill-assessment/landing, "
        "рабочий UI: GET /api/skill-assessment/workspace (GET /ui → 307 на workspace), health: GET /api/skill-assessment/health"
    )
    _log.info(
        "skill_assessment: в браузере используйте http://127.0.0.1:8000/api/skill-assessment/health "
        "(не localhost — на Windows возможен другой процесс на IPv6). "
        "Проверка порта: netstat -ano | findstr :8000"
    )
    from app.db import Base, engine

    Base.metadata.create_all(bind=engine)
    _ensure_sa_session_phase_column(engine)
    db = SessionLocal()
    try:
        ensure_demo_taxonomy(db)
        db.commit()
    finally:
        db.close()


@app.get("/skill-assessment", include_in_schema=False)
def skill_assessment_page() -> FileResponse:
    """Входная страница плагина (лендинг; короткий URL /skill-assessment редиректит на /api/…/landing)."""
    p = _static / "index.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_static_missing")
    return FileResponse(p)


@app.get("/skill-assessment/future-demo", include_in_schema=False)
def skill_assessment_future_demo_page() -> FileResponse:
    """Превью вымышленного сценария экзамена (LLM + кейс + оценка руководителя)."""
    p = _static / "demo_future_scenario.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_demo_future_missing")
    return FileResponse(p)
