"""
Точка входа ASGI: приложение ядра typical_infrastructure + роутер skill_assessment.

Запускать из **корня клона ядра**, чтобы пакет ``app`` был в PYTHONPATH::

    cd D:\\path\\to\\typical_infrastructure
    .venv\\Scripts\\activate
    pip install -e D:\\path\\to\\skill_assessment
    uvicorn skill_assessment.runner:app --reload

Ядро в upstream не меняется; зависимость на skill_assessment в requirements ядра не добавляется.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

import skill_assessment.infrastructure.db_models  # noqa: F401 — таблицы на общем Base

from app.db import SessionLocal
from app.main import app

from skill_assessment.router import router as skill_assessment_router
from skill_assessment.services.taxonomy_seed import ensure_demo_taxonomy

app.include_router(skill_assessment_router, prefix="/api")

_static = Path(__file__).resolve().parent / "static"


@app.on_event("startup")
def _skill_assessment_startup() -> None:
    """Гарантируем таблицы skill_assessment и демо-таксономию при пустых таблицах."""
    from app.db import Base, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_demo_taxonomy(db)
        db.commit()
    finally:
        db.close()


@app.get("/skill-assessment", include_in_schema=False)
def skill_assessment_page() -> FileResponse:
    """Черновой UI плагина (статика из пакета skill_assessment)."""
    p = _static / "index.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill_assessment_static_missing")
    return FileResponse(p)
