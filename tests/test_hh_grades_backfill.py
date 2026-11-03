"""Бэкфилл detected_grade для HH по required_experience."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from backend.db.models import VacancyRow
from backend.db.vacancy_repository import backfill_hh_grades_from_experience
from jobdesk.hh.mapper import hh_source_post_key, stable_vacancy_id


def test_backfill_hh_grades_from_experience_name(db_session):
    now = datetime.now(UTC)
    with db_session as session:
        cases = (
            ("1", "Нет опыта", "intern"),
            ("2", "От 1 года до 3 лет", "junior"),
            ("3", "От 3 до 6 лет", "middle"),
            ("4", "Более 6 лет", "senior"),
            ("5", "непонятно", "unknown"),
            ("6", None, "unknown"),
        )
        for hid, exp, _ in cases:
            session.add(
                VacancyRow(
                    id=stable_vacancy_id(hid),
                    source_post_key=hh_source_post_key(hid),
                    source_channel="hh",
                    source_url="https://hh.ru",
                    raw_text="x",
                    cleaned_text="x",
                    detected_grade="unknown",
                    required_experience=exp,
                    extraction_source="hh_api",
                    vacancy_score=100,
                    parsed_at=now,
                )
            )
        # TG-вакансия с unknown — не трогаем
        session.add(
            VacancyRow(
                id="tg1",
                source_post_key="ch-1",
                source_channel="python_jobs",
                source_url="https://t.me",
                raw_text="x",
                cleaned_text="x",
                detected_grade="unknown",
                required_experience="От 1 года до 3 лет",
                extraction_source="rules",
                vacancy_score=50,
                parsed_at=now,
            )
        )
        session.commit()

        n = backfill_hh_grades_from_experience(session)
        session.commit()
        assert n == 4

        rows = {r.source_post_key: r.detected_grade for r in session.scalars(select(VacancyRow)).all()}
        assert rows[hh_source_post_key("1")] == "intern"
        assert rows[hh_source_post_key("2")] == "junior"
        assert rows[hh_source_post_key("3")] == "middle"
        assert rows[hh_source_post_key("4")] == "senior"
        assert rows[hh_source_post_key("5")] == "unknown"
        assert rows[hh_source_post_key("6")] == "unknown"
        assert rows["ch-1"] == "unknown"

        assert backfill_hh_grades_from_experience(session) == 0
