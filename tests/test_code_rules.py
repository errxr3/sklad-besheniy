"""Правила кода из SPEC §8 и §13, проверяемые автоматически."""

import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Присваивание статуса зеркала: `что-то.status = ...`
STATUS_ASSIGN = re.compile(r"\.status\s*=(?!=)")
FORBIDDEN_MARKERS = ("TODO", "FIXME", "NotImplementedError", "XXX:")


def python_files(root: Path) -> list[Path]:
    """Все .py файлы каталога."""
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_status_assigned_only_in_transitions() -> None:
    """Статус зеркала меняется только в app/transitions.py (SPEC §8)."""
    offenders: list[str] = []
    for path in python_files(APP_DIR):
        if path.name == "transitions.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if STATUS_ASSIGN.search(line):
                offenders.append(f"{path.relative_to(PROJECT_DIR)}:{number}: {line.strip()}")
    assert not offenders, "статус меняется вне модуля переходов:\n" + "\n".join(offenders)


def test_transition_table_is_single_source() -> None:
    """Словарь переходов объявлен ровно в одном месте."""
    declarations = []
    for path in python_files(APP_DIR):
        text = path.read_text(encoding="utf-8")
        if "ALLOWED_TRANSITIONS: Final" in text or "ALLOWED_TRANSITIONS =" in text:
            declarations.append(path.name)
    assert declarations == ["transitions.py"]


def test_no_todo_markers() -> None:
    """В коде нет заглушек и пометок «доделать» (SPEC §13)."""
    offenders: list[str] = []
    for path in python_files(APP_DIR) + python_files(PROJECT_DIR / "tests"):
        if path.name == Path(__file__).name:
            continue  # этот файл сам перечисляет искомые слова
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for marker in FORBIDDEN_MARKERS:
                if marker in line:
                    offenders.append(f"{path.relative_to(PROJECT_DIR)}:{number}: {line.strip()}")
    assert not offenders, "найдены заглушки:\n" + "\n".join(offenders)


def test_no_todo_in_templates_and_scripts() -> None:
    """В шаблонах и скриптах тоже нет заглушек."""
    offenders: list[str] = []
    patterns = ("*.html", "*.js", "*.css", "*.sh")
    for pattern in patterns:
        for path in (APP_DIR).rglob(pattern):
            if "vendor" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                for marker in ("TODO", "FIXME"):
                    if marker in line:
                        offenders.append(f"{path.relative_to(PROJECT_DIR)}:{number}")
    assert not offenders, "найдены заглушки:\n" + "\n".join(offenders)
