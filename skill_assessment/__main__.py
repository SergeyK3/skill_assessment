# route: python -m skill_assessment | file: skill_assessment/__main__.py
"""Запуск uvicorn с привязкой к IPv4 127.0.0.1 (см. runner.py — почему не localhost на Windows)."""

from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "skill_assessment.runner:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
