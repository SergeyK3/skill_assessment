# -*- coding: utf-8 -*-
"""Пишет repo/exports/kpi_postgres_seed.sql для PostgreSQL (Docker)."""

from __future__ import annotations

from skill_assessment.services.kpi_seed import write_kpi_postgres_seed_sql

if __name__ == "__main__":
    print(write_kpi_postgres_seed_sql())
