"""Грейд в начале роли срезается: «Senior QA» → «QA», «Middle QA» → «QA».

Тот же шаблон, что ``_GRADE_PREFIX`` в ``jobdesk/config/schema.py``: senior / middle / junior /
sr / jr / mid / сеньор / мидл / джун(иор), с «+», и после него ещё есть имя. Lead и Head не
трогаются — «Team Lead», «QA Lead» остаются ролями. После среза имена проходят через синонимы
миграции 036 («Тестировщик» → «QA»), повторы схлопываются, порядок сохраняется. Меняются
вакансии и сохранённые фильтры; снимки прогонов — история, их не трогаем. Downgrade не
возвращает грейд в роль.

Revision ID: 053_strip_grade_from_roles
Revises: 052_vacancy_score_100
Create Date: 2026-11-03
"""

from typing import Sequence, Union

from alembic import op

revision: str = "053_strip_grade_from_roles"
down_revision: Union[str, None] = "052_vacancy_score_100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREFIX = r"^(senior|middle|junior|sr\.?|jr\.?|mid|сеньор|мидл|джун(иор)?)\+?\s+(?=\S)"
_ALIASES = {
    "Manual QA": "QA",
    "Тестировщик": "QA",
    "AQA": "Automation QA",
    "Программист, разработчик": "Software Engineer",
}

_STRIPPED = f"btrim(regexp_replace(e, '{_PREFIX}', '', 'i'))"
_CASE = (
    f"CASE {_STRIPPED} "
    + " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in _ALIASES.items())
    + f" ELSE {_STRIPPED} END"
)


def _canonical_array(arr: str) -> str:
    """SQL-выражение: JSONB-массив ``arr`` без грейда в начале имён, без повторов."""
    return (
        "(SELECT COALESCE(jsonb_agg(name ORDER BY first_ord), '[]'::jsonb) FROM ("
        f" SELECT {_CASE} AS name, min(ord) AS first_ord"
        f" FROM jsonb_array_elements_text({arr}) WITH ORDINALITY AS t(e, ord)"
        f" GROUP BY {_CASE}) d)"
    )


def _has_graded(arr: str) -> str:
    return f"EXISTS (SELECT 1 FROM jsonb_array_elements_text({arr}) AS t(e) WHERE e ~* '{_PREFIX}')"


def upgrade() -> None:
    op.execute(
        f"UPDATE vacancies SET detected_specializations = {_canonical_array('detected_specializations')}, "
        f"updated_at = now() WHERE {_has_graded('detected_specializations')}"
    )
    specs = "params::jsonb -> 'specializations'"
    op.execute(
        f"UPDATE saved_filters SET params = jsonb_set(params::jsonb, '{{specializations}}', "
        f"{_canonical_array(specs)})::json "
        f"WHERE jsonb_typeof({specs}) = 'array' AND {_has_graded(specs)}"
    )


def downgrade() -> None:
    pass
