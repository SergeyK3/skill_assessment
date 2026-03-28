# -*- coding: utf-8 -*-
"""Пишет repo/exports/competency_postgres_seed.sql для загрузки в PostgreSQL (Docker)."""

from __future__ import annotations

from skill_assessment.services.competency_seed import write_postgres_seed_sql

if __name__ == "__main__":
    print(write_postgres_seed_sql())
