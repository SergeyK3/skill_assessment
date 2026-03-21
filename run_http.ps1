# Запуск приложения с ядром и skill_assessment (из каталога typical_infrastructure).
# Пример:
#   .\run_http.ps1 -CoreRoot "D:\...\typical_infrastructure" -SkillPkgRoot "D:\...\repo\skill_assessment"

param(
    [Parameter(Mandatory = $true)]
    [string] $CoreRoot,
    [Parameter(Mandatory = $true)]
    [string] $SkillPkgRoot
)

Set-Location $CoreRoot
if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
    Write-Error "Создайте venv в ядре: python -m venv .venv"
    exit 1
}
& .\.venv\Scripts\Activate.ps1
pip install -e $SkillPkgRoot -q
Write-Host "Uvicorn: skill_assessment.runner:app (cwd=$CoreRoot)"
uvicorn skill_assessment.runner:app --host 0.0.0.0 --port 8000 --reload
