# route: (pytest) | file: tests/test_hr_core.py
"""Интеграция hr_core: заглушки и подписи без зависимости от app.hr."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skill_assessment.integration import hr_core


def test_employee_greeting_label_name_patronymic() -> None:
    s = hr_core.EmployeeSnapshot(
        id="e1",
        client_id="c1",
        first_name="Иван",
        middle_name="Иванович",
        display_name="Иванов Иван Иванович",
    )
    assert hr_core.employee_greeting_label(s) == "Иван Иванович"


def test_employee_greeting_label_from_full_name() -> None:
    s = hr_core.EmployeeSnapshot(
        id="e1",
        client_id="c1",
        display_name="Петрова Мария Сергеевна",
    )
    assert hr_core.employee_greeting_label(s) == "Мария Сергеевна"


def test_employee_display_label_priority() -> None:
    assert hr_core.employee_display_label(None) is None
    s = hr_core.EmployeeSnapshot(id="e1", client_id="c1", display_name="  Иван  ", email="a@b.c")
    assert hr_core.employee_display_label(s) == "Иван"
    s2 = hr_core.EmployeeSnapshot(id="e1", client_id="c1", display_name=None, email=" x@y.z ")
    assert hr_core.employee_display_label(s2) == "x@y.z"
    s3 = hr_core.EmployeeSnapshot(id="e99", client_id="c1", display_name=None, email=None)
    assert hr_core.employee_display_label(s3) == "e99"


def test_get_employee_stub_when_core_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hr_core, "CORE_HR_AVAILABLE", False)
    monkeypatch.setattr(hr_core, "_core_get_employee", None)
    db = MagicMock()
    assert hr_core.get_employee(db, "c1", None) is None
    snap = hr_core.get_employee(db, "c1", "emp_x")
    assert snap is not None
    assert snap.id == "emp_x"
    assert snap.client_id == "c1"
    assert hr_core.employee_display_label(snap) == "emp_x"
