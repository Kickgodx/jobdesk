"""vacancy_score по шкале 0–100 у всех вакансий.

Правила (``jobdesk/extraction/vacancy_detector.py``) раньше писали сырую сумму признаков
(максимум 12), а HH — 100: в одной колонке смешались две шкалы, сортировка и средние
по score теряли смысл. Теперь правила нормируют сумму на 100; здесь та же формула
(``round(raw * 100 / 12)``) применяется к уже сохранённым вакансиям не из HH.
Дробная часть ``raw * 100 / 12`` никогда не равна .5, поэтому ``ROUND`` Postgres
и ``round`` Python дают одно и то же.

Revision ID: 052_vacancy_score_100
Revises: 051_maintenance_requests
Create Date: 2026-11-02
"""

from typing import Sequence, Union

from alembic import op

revision: str = "052_vacancy_score_100"
down_revision: Union[str, None] = "051_maintenance_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE vacancies
        SET vacancy_score = LEAST(100, ROUND(vacancy_score * 100.0 / 12)),
            vacancy_substantive_score = LEAST(100, ROUND(vacancy_substantive_score * 100.0 / 12))
        WHERE extraction_source <> 'hh_api'
          AND (vacancy_score <> 0 OR vacancy_substantive_score <> 0)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE vacancies
        SET vacancy_score = ROUND(vacancy_score * 12 / 100.0),
            vacancy_substantive_score = ROUND(vacancy_substantive_score * 12 / 100.0)
        WHERE extraction_source <> 'hh_api'
          AND (vacancy_score <> 0 OR vacancy_substantive_score <> 0)
        """
    )
