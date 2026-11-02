"""Миграция 052: оценки правил переводятся со старой шкалы (0–12) на 0–100, HH не трогается."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from tests.conftest import add_legacy_vacancy

from backend.db.migrate import alembic_ini_path
from backend.db.models import VacancyRow


def test_migration_rescales_rule_scores(pg_empty_db):
    cfg = Config(str(alembic_ini_path()))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "051_maintenance_requests")

    engine = create_engine(pg_empty_db)
    rows = (
        # id, extraction_source, score, substantive
        ("tg-max", "rules", 12, 10),
        ("tg-mid", "ai", 3, 1),
        ("tg-zero", "rules", 0, 0),
        ("hh", "hh_api", 100, 100),
    )
    with sessionmaker(bind=engine)() as s:
        for vid, source, score, substantive in rows:
            add_legacy_vacancy(
                s,
                id=vid,
                source_post_key=vid,
                source_channel="hh" if source == "hh_api" else "chan",
                source_url="u",
                raw_text=vid,
                cleaned_text=vid,
                extraction_source=source,
                vacancy_score=score,
                vacancy_substantive_score=substantive,
            )
        s.commit()

    command.upgrade(cfg, "head")
    with sessionmaker(bind=engine)() as s:
        got = {r.id: (r.vacancy_score, r.vacancy_substantive_score) for r in s.scalars(select(VacancyRow))}
    engine.dispose()
    assert got == {
        "tg-max": (100, 83),
        "tg-mid": (25, 8),
        "tg-zero": (0, 0),
        "hh": (100, 100),
    }
