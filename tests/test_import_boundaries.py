"""Границы пакетов (design hh-service, D1): что кому разрешено импортировать.

``jobdesk`` — домен, ни от кого не зависит; ``jobdesk_platform`` — инфраструктура поверх
домена; вынесенные сервисы (``jobdesk_hh``) не знают ядра, ядро (``backend``) не знает
сервисов — только HTTP. ``jobdesk_mcp`` — чистый HTTP-клиент API. Импорты ищутся по всему
AST, включая отложенные внутри функций.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PACKAGES = ("jobdesk", "jobdesk_platform", "jobdesk_hh", "jobdesk_mcp", "backend")

FORBIDDEN: dict[str, frozenset[str]] = {
    "jobdesk": frozenset({"backend", "jobdesk_platform", "jobdesk_hh", "jobdesk_mcp"}),
    "jobdesk_platform": frozenset({"backend", "jobdesk_hh", "jobdesk_mcp"}),
    "jobdesk_hh": frozenset({"backend", "jobdesk_mcp"}),
    "jobdesk_mcp": frozenset({"backend", "jobdesk", "jobdesk_platform", "jobdesk_hh"}),
    "backend": frozenset({"jobdesk_hh", "jobdesk_mcp"}),
}

# Единственное исключение ядра: дамп и импорт БД (CLAUDE.md, «Граница core ↔ backend»).
ALLOWED: dict[str, frozenset[str]] = {
    "jobdesk/cli/db.py": frozenset({"backend"}),
}


def imported_packages(source: str) -> set[str]:
    """Верхние пакеты абсолютных импортов модуля."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def violations(package: str) -> list[str]:
    out: list[str] = []
    base = ROOT / package
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        bad = imported_packages(path.read_text(encoding="utf-8")) & FORBIDDEN[package]
        bad -= ALLOWED.get(rel, frozenset())
        out.extend(f"{rel} → {name}" for name in sorted(bad))
    return out


@pytest.mark.parametrize("package", PACKAGES)
def test_package_respects_import_boundaries(package):
    assert violations(package) == []


def test_detector_sees_deferred_and_from_imports():
    source = "import os\ndef f():\n    from backend.db import session\n    import jobdesk_hh.api as a\n"
    assert {"backend", "jobdesk_hh"} <= imported_packages(source)


def test_detector_flags_forbidden_import(tmp_path, monkeypatch):
    pkg = tmp_path / "jobdesk_platform"
    pkg.mkdir()
    (pkg / "bad.py").write_text("from backend.settings import get_settings\n", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    assert violations("jobdesk_platform") == ["jobdesk_platform/bad.py → backend"]
