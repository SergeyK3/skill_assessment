# route: (integration) | file: skill_assessment/integration/__init__.py
"""Интеграция с ядром typical_infrastructure (in-process)."""

from skill_assessment.integration.hr_core import (
    CORE_HR_AVAILABLE,
    EmployeeSnapshot,
    employee_display_label,
    employee_greeting_label,
    get_employee,
)

__all__ = [
    "CORE_HR_AVAILABLE",
    "EmployeeSnapshot",
    "employee_display_label",
    "employee_greeting_label",
    "get_employee",
]
