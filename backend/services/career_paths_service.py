"""Карьерная траектория: куда расти из резюме — следующий грейд своей роли и соседние роли.

Позиция резюме — самые частые роль и грейд среди ``POSITION_PEERS`` вакансий, ближайших к резюме
по эмбеддингу за год (как профиль «Резюме и рынок»). Соседние роли — частые среди ``NEIGHBORHOOD``
ближайших. Для каждой цели берутся ``ROLE_PEERS`` уникальных вакансий роли за год (с архивом),
ближайших к резюме: широкая роль («Software Engineer») иначе смешала бы 1С, Go и C++ в один маршрут.
Из них сегмент — грейд цели, если в нём хватает вакансий с технологиями, иначе все грейды.
По сегменту: какая доля вакансий подходит резюме уже сейчас (правило «Что если выучить»), маршрут —
до ``ROUTE_STEPS`` недостающих технологий, где каждая следующая открывает больше всего вакансий
(с учётом уже выбранных), и медиана зарплаты против текущей позиции. LLM не нужен.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from statistics import median

from sqlalchemy.orm import Session

from backend.db.models import ResumeRow
from backend.db.vacancy_metrics import _prepare_fx
from backend.schemas import CareerPath, CareerPathsOut, CareerRouteStep, CareerSegment
from backend.services import embedding_service
from backend.services.resume_market_service import WINDOW_DAYS, nearest_vacancies, resume_technology_check
from backend.services.skill_what_if_service import FIT_THRESHOLD, fits, is_learnable
from jobdesk.extraction.grade_extractor import GRADE_RANK, UNKNOWN_GRADE

# Позиция резюме — по стольким ближайшим вакансиям.
POSITION_PEERS = 50
# Соседние роли — среди стольких ближайших к резюме вакансий.
NEIGHBORHOOD = 300
# Соседняя роль — у стольких ближайших вакансий и не реже этой доли.
MIN_NEAR = 5
MIN_NEAR_SHARE = 0.03
ROLES_TOP = 6
# Сегмент роли — из стольких её вакансий за окно, ближайших к резюме.
ROLE_PEERS = 300
# Грейд — отдельный сегмент, если в нём столько вакансий с технологиями; иначе берутся все грейды.
MIN_SEGMENT = 20
# Технология маршрута — нужна не меньше чем в такой доле вакансий сегмента; кандидатов — самые частые.
ROUTE_MIN_SHARE = 0.05
ROUTE_CANDIDATES = 30
ROUTE_STEPS = 5
# Медиана зарплаты — от стольких вакансий сегмента с зарплатой.
MIN_SALARY = 8

# (грейд, технологии, зарплата ₽) — вакансия из выборки роли.
Row = tuple[str | None, list[str] | None, float | None]
# (всего технологий, покрыто резюме, недостающие) — вакансия сегмента с технологиями.
Peer = tuple[int, int, frozenset[str]]


def plan_route(peers: Sequence[Peer], *, steps: int = ROUTE_STEPS) -> list[CareerRouteStep]:
    """Жадный маршрут: на каждом шаге — технология, после которой подходит больше всего новых
    вакансий; при равенстве — с наибольшим ростом покрытия, затем по имени.

    Шаги в конце, которые не добавляют подходящих вакансий, отбрасываются — кроме случая, когда
    подходящих не добавляет ни один шаг: тогда маршрут показывает рост покрытия.
    """
    if not peers:
        return []
    total = len(peers)
    demand = Counter(t for _, _, missing in peers for t in missing)
    min_demand = max(2, math.ceil(ROUTE_MIN_SHARE * total - 1e-9))
    candidates = [t for t, k in demand.most_common() if k >= min_demand and is_learnable(t)][:ROUTE_CANDIDATES]
    by_tech: dict[str, list[int]] = defaultdict(list)
    for i, (_, _, missing) in enumerate(peers):
        for t in missing:
            by_tech[t].append(i)
    covered = [c for _, c, _ in peers]
    n_fit = sum(fits(c, n) for (n, _, _), c in zip(peers, covered, strict=True))
    coverage = sum(c / n for (n, _, _), c in zip(peers, covered, strict=True))

    route: list[CareerRouteStep] = []
    gains: list[int] = []
    left = set(candidates)
    for _ in range(steps):
        best: tuple[int, float, str] | None = None
        for tech in sorted(left):
            new_fit = 0
            gain = 0.0
            for i in by_tech[tech]:
                n = peers[i][0]
                gain += 1 / n
                if not fits(covered[i], n) and fits(covered[i] + 1, n):
                    new_fit += 1
            if best is None or (new_fit, gain) > (best[0], best[1]):
                best = (new_fit, gain, tech)
        if best is None:
            break
        new_fit, gain, tech = best
        left.discard(tech)
        for i in by_tech[tech]:
            covered[i] += 1
        n_fit += new_fit
        coverage += gain
        gains.append(new_fit)
        route.append(
            CareerRouteStep(
                technology=tech,
                demand_share=demand[tech] / total,
                fit_share=n_fit / total,
                coverage=coverage / total,
            )
        )
    if any(gains):
        while gains and gains[-1] == 0:
            gains.pop()
            route.pop()
    return route


def _role_sample(session: Session, vector: list[float], role: str, *, model: str, now: datetime) -> list[Row]:
    rows = nearest_vacancies(session, vector, model=model, now=now, limit=ROLE_PEERS, specialization=role)
    return [(grade, techs, salary) for grade, _, techs, salary in rows]


def _with_techs(rows: Sequence[Row], grade: str | None) -> int:
    return sum(1 for g, techs, _ in rows if techs and (grade is None or g == grade))


def _segment(
    rows: Sequence[Row], role: str, grade: str | None, in_resume: Callable[[str], bool]
) -> tuple[CareerSegment, list[Peer]]:
    """Сегмент роли: грейд ``grade`` или все грейды (``None``)."""
    picked = [r for r in rows if grade is None or r[0] == grade]
    peers: list[Peer] = []
    for _, techs, _ in picked:
        ts = set(techs or [])
        if not ts:
            continue
        missing = frozenset(t for t in ts if not in_resume(t))
        peers.append((len(ts), len(ts) - len(missing), missing))
    salaries = [float(s) for _, _, s in picked if s is not None]
    segment = CareerSegment(
        role=role,
        grade=grade,
        vacancies=len(peers),
        fit_share=sum(fits(c, n) for n, c, _ in peers) / len(peers) if peers else 0.0,
        coverage=sum(c / n for n, c, _ in peers) / len(peers) if peers else 0.0,
        salary_median=median(salaries) if len(salaries) >= MIN_SALARY else None,
        salary_count=len(salaries),
    )
    return segment, peers


def _segment_grade(rows: Sequence[Row], grade: str | None) -> str | None:
    """Грейд как сегмент — если в нём хватает вакансий с технологиями, иначе все грейды."""
    if grade is not None and _with_techs(rows, grade) >= MIN_SEGMENT:
        return grade
    return None


def _next_grade(rows: Sequence[Row], grade: str | None) -> str | None:
    """Ближайший грейд выше ``grade``, в котором хватает вакансий роли с технологиями."""
    if grade is None or grade not in GRADE_RANK:
        return None
    higher = sorted((g for g in GRADE_RANK if GRADE_RANK[g] > GRADE_RANK[grade]), key=GRADE_RANK.__getitem__)
    return next((g for g in higher if _with_techs(rows, g) >= MIN_SEGMENT), None)


def _path(
    segment: CareerSegment, peers: list[Peer], *, kind: str, closeness: float | None, base: float | None
) -> CareerPath:
    uplift = segment.salary_median / base - 1 if base and segment.salary_median else None
    return CareerPath(
        **segment.model_dump(),
        kind=kind,
        closeness=closeness,
        route=plan_route(peers),
        salary_uplift=uplift,
    )


def career_paths(session: Session, resume: ResumeRow) -> CareerPathsOut:
    """``EmbeddingsUnavailable`` — эмбеддинги выключены или недоступны."""
    vector = embedding_service.resume_embedding(session, resume)
    model = embedding_service._ai().embeddings.model
    now = datetime.now(UTC)
    rows = nearest_vacancies(session, vector, model=model, now=now, limit=NEIGHBORHOOD)
    resume_techs, in_resume = resume_technology_check(session, resume)
    out = CareerPathsOut(
        resume_id=resume.id,
        window_days=WINDOW_DAYS,
        peers=len(rows),
        fit_threshold=FIT_THRESHOLD,
        resume_technologies=resume_techs,
        salary_display=_prepare_fx(session),
    )
    position = rows[:POSITION_PEERS]
    roles = Counter(r for _, specs, *_ in position for r in set(specs or []))
    if not roles:
        return out
    grades = Counter(g for g, *_ in position if g and g != UNKNOWN_GRADE)
    role = min(roles, key=lambda r: (-roles[r], r))
    grade = min(grades, key=lambda g: (-grades[g], g)) if grades else None

    own_rows = _role_sample(session, vector, role, model=model, now=now)
    current, _ = _segment(own_rows, role, _segment_grade(own_rows, grade), in_resume)
    base = current.salary_median
    paths: list[CareerPath] = []

    up = _next_grade(own_rows, grade)
    if up is not None:
        segment, peers = _segment(own_rows, role, up, in_resume)
        paths.append(_path(segment, peers, kind="grade", closeness=None, base=base))

    near = Counter(r for _, specs, *_ in rows for r in set(specs or []))
    min_near = max(MIN_NEAR, math.ceil(MIN_NEAR_SHARE * len(rows) - 1e-9))
    neighbours = sorted((r for r, k in near.items() if r != role and k >= min_near), key=lambda r: (-near[r], r))
    lateral: list[CareerPath] = []
    for other in neighbours[:ROLES_TOP]:
        other_rows = _role_sample(session, vector, other, model=model, now=now)
        segment, peers = _segment(other_rows, other, _segment_grade(other_rows, grade), in_resume)
        if not peers:
            continue
        lateral.append(_path(segment, peers, kind="role", closeness=near[other] / len(rows), base=base))
    lateral.sort(key=lambda p: (-p.coverage, -(p.closeness or 0), p.role))
    return out.model_copy(update={"current": current, "paths": paths + lateral})
