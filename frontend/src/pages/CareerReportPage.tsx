import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { SalaryStrip } from "../components/MarketSalaryBlock";
import type {
  CareerPath,
  CareerPaths,
  ResumeMarket,
  SkillGaps,
  SkillWhatIf,
  VacancyListItem,
} from "../types/api";
import { formatThousands as k } from "../utils/formatSalary";
import { loadResumeMarketPrefs } from "../utils/resumeMarketPrefs";

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${Math.round(v * 100)}%`;
}

function signedPct(v: number): string {
  const s = Math.round(v * 100);
  return s > 0 ? `+${s}%` : `${s}%`;
}

function months(days: number): number {
  return Math.round(days / 30);
}

/** Раздел отчёта: заголовок, пояснение, загрузка, «нет данных» и ошибка — единообразно. */
function Section({
  title,
  loading,
  error,
  empty,
  children,
}: {
  title: string;
  loading: boolean;
  error: unknown;
  empty?: string | null;
  children: ReactNode;
}) {
  let body: ReactNode = children;
  if (loading) body = <p className="muted">Загрузка…</p>;
  else if (error instanceof ApiError && error.code === "UPSTREAM_UNAVAILABLE")
    body = <p className="muted">Нужны эмбеддинги (смысловой поиск): {error.message}</p>;
  else if (error) body = <p className="error">{(error as Error).message}</p>;
  else if (empty) body = <p className="muted">{empty}</p>;
  return (
    <section className="report-section">
      <h3>{title}</h3>
      {body}
    </section>
  );
}

function Summary({
  market,
  whatIf,
  paths,
}: {
  market?: ResumeMarket;
  whatIf?: SkillWhatIf;
  paths?: CareerPaths;
}) {
  const topRole = market?.specializations[0];
  const topGrade = market?.grade ? undefined : market?.grades[0];
  const bestSkill = whatIf?.options.find((o) => o.new_fit > 0) ?? whatIf?.options[0];
  const nextGrade = paths?.paths.find((p) => p.kind === "grade");
  const tiles: { value: string; label: string }[] = [];
  if (market?.median != null)
    tiles.push({
      value: `${k(market.median)} ₽`,
      label: market.grade
        ? `медиана зарплаты похожих вакансий ${market.grade}`
        : "медиана зарплаты похожих вакансий",
    });
  if (market?.coverage != null)
    tiles.push({ value: pct(market.coverage), label: "технологий похожих вакансий названо в резюме" });
  if (whatIf && whatIf.with_technologies > 0)
    tiles.push({
      value: `${whatIf.baseline_fit} из ${whatIf.with_technologies}`,
      label: "похожих вакансий подходят по стеку",
    });
  if (tiles.length === 0 && !topRole) return null;
  return (
    <section className="report-section">
      <h3>Главное</h3>
      <div className="stat-tiles">
        {tiles.map((t) => (
          <div className="stat-tile" key={t.label}>
            <span className="stat-value">{t.value}</span>
            <span className="stat-label">{t.label}</span>
          </div>
        ))}
      </div>
      <ul className="report-bullets">
        {topRole && (
          <li>
            Рынок видит резюме как <strong>{topRole.name}</strong>
            {topGrade && (
              <>
                {" "}
                уровня <strong>{topGrade.name}</strong>
              </>
            )}{" "}
            — так размечены {pct(topRole.share)} самых похожих вакансий.
          </li>
        )}
        {bestSkill && (
          <li>
            Первое, что стоит добавить в стек: <strong>{bestSkill.technology}</strong> — нужна в{" "}
            {pct(bestSkill.demand_share)} похожих вакансий
            {bestSkill.new_fit > 0 && <>, откроет ещё {bestSkill.new_fit}</>}.
          </li>
        )}
        {nextGrade && (
          <li>
            Следующий грейд — <strong>{nextGrade.grade}</strong>: сейчас подходит {pct(nextGrade.fit_share)}{" "}
            таких вакансий
            {nextGrade.salary_uplift != null && <>, зарплата {signedPct(nextGrade.salary_uplift)}</>}.
          </li>
        )}
      </ul>
    </section>
  );
}

function MarketSection({ m }: { m: ResumeMarket }) {
  return (
    <>
      <p className="caption">
        {m.peers} вакансий{m.grade ? ` грейда ${m.grade}` : ""}, ближайших к резюме по смыслу текста, за{" "}
        {months(m.window_days)} месяцев.
      </p>
      <div className="report-columns">
        <div>
          <strong>Роли</strong>
          <ul className="report-list">
            {m.specializations.slice(0, 5).map((s) => (
              <li key={s.name}>
                {s.name} <span className="muted">{pct(s.share)}</span>
              </li>
            ))}
          </ul>
        </div>
        {!m.grade && (
          <div>
            <strong>Грейды</strong>
            <ul className="report-list">
              {m.grades.slice(0, 5).map((s) => (
                <li key={s.name}>
                  {s.name} <span className="muted">{pct(s.share)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div>
          <strong>Зарплата</strong>
          {m.median != null ? (
            <>
              <p>
                обычно {k(m.p25)} – {k(m.p75)}, медиана <strong>{k(m.median)}</strong> ₽
              </p>
              <SalaryStrip q={m} />
              <p className="caption">по {m.salary_count} вакансиям с зарплатой</p>
            </>
          ) : (
            <p className="muted">мало вакансий с зарплатой</p>
          )}
        </div>
      </div>
      <strong>Технологии похожих вакансий</strong>
      <p className="report-chips">
        {m.technologies.slice(0, 20).map((t) => (
          <span key={t.name} className={t.in_resume ? "report-chip have" : "report-chip"}>
            {t.in_resume ? "✓ " : ""}
            {t.name} <span className="muted">{pct(t.share)}</span>
          </span>
        ))}
      </p>
      <p className="caption">✓ — технология названа в резюме.</p>
    </>
  );
}

function WhatIfSection({ m }: { m: SkillWhatIf }) {
  return (
    <>
      <p className="caption">
        Вакансия подходит, если резюме называет не меньше {pct(m.fit_threshold)} её технологий. Сейчас
        подходят {m.baseline_fit} из {m.with_technologies}.
      </p>
      <table>
        <thead>
          <tr>
            <th>Технология</th>
            <th>Нужна в</th>
            <th>+подходящих</th>
            <th>Премия</th>
            <th>Медиана с ней</th>
          </tr>
        </thead>
        <tbody>
          {m.options.slice(0, 8).map((o) => (
            <tr key={o.technology}>
              <td>
                <strong>{o.technology}</strong>
              </td>
              <td>
                {o.demand} <span className="muted">({pct(o.demand_share)})</span>
              </td>
              <td>{o.new_fit > 0 ? `+${o.new_fit}` : "0"}</td>
              <td>{o.premium == null ? "—" : signedPct(o.premium)}</td>
              <td>{o.median_with != null ? k(o.median_with) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function pathTitle(p: CareerPath): string {
  return p.kind === "grade" ? `${p.role} · ${p.grade}` : `${p.role}${p.grade ? ` · ${p.grade}` : ""}`;
}

function PathsSection({ m }: { m: CareerPaths }) {
  return (
    <>
      {m.current && (
        <p>
          Сейчас:{" "}
          <strong>
            {pathTitle({ ...m.current, kind: "role", closeness: null, route: [], salary_uplift: null })}
          </strong>{" "}
          — подходит {pct(m.current.fit_share)} из {m.current.vacancies} вакансий
          {m.current.salary_median != null && <>, медиана {k(m.current.salary_median)} ₽</>}.
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Куда</th>
            <th>Вакансий</th>
            <th>Подходит сейчас</th>
            <th>Зарплата</th>
            <th>Что добавить</th>
          </tr>
        </thead>
        <tbody>
          {m.paths.slice(0, 5).map((p) => (
            <tr key={`${p.kind}-${p.role}-${p.grade}`}>
              <td>
                <strong>{pathTitle(p)}</strong>
                <div className="muted">{p.kind === "grade" ? "следующий грейд" : "соседняя роль"}</div>
              </td>
              <td>{p.vacancies}</td>
              <td>{pct(p.fit_share)}</td>
              <td>
                {p.salary_median != null ? k(p.salary_median) : "—"}
                {p.salary_uplift != null && <div className="muted">{signedPct(p.salary_uplift)}</div>}
              </td>
              <td>
                {p.route.length === 0
                  ? "—"
                  : p.route
                      .slice(0, 4)
                      .map((s) => `${s.technology} (→ ${pct(s.fit_share)})`)
                      .join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="caption">
        «Подходит сейчас» — доля вакансий, где резюме называет достаточно технологий; в скобках у шага —
        сколько будет подходить после него.
      </p>
    </>
  );
}

function GapsSection({ m }: { m: SkillGaps }) {
  const items = [...m.technologies, ...m.other].sort((a, b) => b.vacancies - a.vacancies).slice(0, 10);
  return (
    <>
      <p className="caption">По {m.evaluated} сохранённым оценкам соответствия (LLM) резюме вакансиям.</p>
      <ul className="report-list">
        {items.map((g) => (
          <li key={g.name}>
            <strong>{g.name}</strong> — не хватает в {g.vacancies} из {m.evaluated}
            {g.critical > 0 && <span className="muted"> (критично в {g.critical})</span>}
          </li>
        ))}
      </ul>
    </>
  );
}

function VacanciesSection({ items, byMatch }: { items: VacancyListItem[]; byMatch: boolean }) {
  return (
    <>
      <p className="caption">
        {byMatch
          ? "С лучшей сохранённой оценкой соответствия."
          : "Ближайшие к резюме по смыслу (оценок пока нет)."}
      </p>
      <table>
        <thead>
          <tr>
            <th>Вакансия</th>
            <th>Компания</th>
            <th>Зарплата</th>
            {byMatch && <th>Оценка</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((v) => (
            <tr key={v.id}>
              <td>
                {v.post_url ? (
                  <a href={v.post_url} target="_blank" rel="noreferrer">
                    {v.detected_specializations.join(", ") || "Вакансия"}
                  </a>
                ) : (
                  v.detected_specializations.join(", ") || "Вакансия"
                )}
                {v.detected_grade && v.detected_grade !== "unknown" && (
                  <span className="muted"> · {v.detected_grade}</span>
                )}
                {v.technologies && v.technologies.length > 0 && (
                  <div className="muted">{v.technologies.slice(0, 4).join(", ")}</div>
                )}
              </td>
              <td>{v.company ?? "—"}</td>
              <td>{v.detected_salary_avg_rub != null ? `${k(v.detected_salary_avg_rub)} ₽` : "—"}</td>
              {byMatch && <td>{v.match_score != null ? Math.round(v.match_score) : "—"}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/**
 * Карьерный отчёт по резюме — одна страница для печати и «Сохранить как PDF»: рынок резюме,
 * что выучить, куда расти, пробелы по оценкам и подходящие вакансии. Собирается из тех же
 * эндпоинтов, что панели страницы «Резюме», без вызова LLM.
 */
export default function CareerReportPage() {
  const resumeId = Number(useParams().resumeId);
  const [today] = useState(() =>
    new Date().toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" }),
  );
  const opts = { staleTime: 60_000, retry: false as const, enabled: Number.isFinite(resumeId) };
  const resumesQ = useQuery({ queryKey: ["resumes"], queryFn: () => api.listResumes() });
  // Та же выборка, что выбрана в «Резюме и рынок» на странице резюме.
  const [marketPrefs] = useState(loadResumeMarketPrefs);
  const marketQ = useQuery({
    queryKey: ["resume-market", resumeId, marketPrefs.peers, marketPrefs.grade],
    queryFn: () =>
      api.getResumeMarket(resumeId, { peers: marketPrefs.peers, grade: marketPrefs.grade || undefined }),
    ...opts,
  });
  const whatIfQ = useQuery({
    queryKey: ["skill-what-if", resumeId],
    queryFn: () => api.getSkillWhatIf(resumeId),
    ...opts,
  });
  const pathsQ = useQuery({
    queryKey: ["career-paths", resumeId],
    queryFn: () => api.getCareerPaths(resumeId),
    ...opts,
  });
  const gapsQ = useQuery({
    queryKey: ["skill-gaps", resumeId],
    queryFn: () => api.getSkillGaps(resumeId),
    ...opts,
  });
  const matchedQ = useQuery({
    queryKey: ["report-matched", resumeId],
    queryFn: () =>
      api.listVacancies({ resume_id: resumeId, sort_by: "match_score", sort_order: "desc", page_size: 10 }),
    ...opts,
  });
  const matched = (matchedQ.data?.items ?? []).filter((v) => v.match_score != null);
  const closestQ = useQuery({
    queryKey: ["report-closest", resumeId],
    queryFn: () => api.listVacancies({ resume_id: resumeId, sort_by: "resume_fit", page_size: 10 }),
    ...opts,
    enabled: opts.enabled && matchedQ.isSuccess && matched.length === 0,
  });

  const resume = resumesQ.data?.items.find((r) => r.id === resumeId);
  if (resumesQ.isSuccess && !resume) {
    return (
      <div className="card">
        <p className="error">Резюме не найдено.</p>
        <Link to="/resumes">← К резюме</Link>
      </div>
    );
  }
  const byMatch = matched.length > 0;
  const vacancies = byMatch ? matched : (closestQ.data?.items ?? []);

  return (
    <div className="career-report">
      <div className="report-toolbar no-print">
        <Link to="/resumes">← К резюме</Link>
        <button type="button" onClick={() => window.print()}>
          Сохранить в PDF / печать
        </button>
      </div>
      <header className="report-header">
        <h2>Карьерный отчёт: {resume?.name ?? "…"}</h2>
        <p className="muted">
          {today} · по базе вакансий Telegram и HH · без LLM, кроме сохранённых оценок соответствия
        </p>
      </header>
      <Summary market={marketQ.data} whatIf={whatIfQ.data} paths={pathsQ.data} />
      <Section
        title="Резюме и рынок"
        loading={marketQ.isLoading}
        error={marketQ.error}
        empty={
          marketQ.data && marketQ.data.peers === 0
            ? "Похожих вакансий пока нет — индексация идёт в фоне."
            : null
        }
      >
        {marketQ.data && <MarketSection m={marketQ.data} />}
      </Section>
      <Section
        title="Что выучить"
        loading={whatIfQ.isLoading}
        error={whatIfQ.error}
        empty={
          whatIfQ.data && whatIfQ.data.options.length === 0
            ? "Недостающих технологий, которые часто нужны похожим вакансиям, нет."
            : null
        }
      >
        {whatIfQ.data && <WhatIfSection m={whatIfQ.data} />}
      </Section>
      <Section
        title="Куда расти"
        loading={pathsQ.isLoading}
        error={pathsQ.error}
        empty={
          pathsQ.data && pathsQ.data.paths.length === 0
            ? "Соседних ролей и следующего грейда с достаточной выборкой нет."
            : null
        }
      >
        {pathsQ.data && <PathsSection m={pathsQ.data} />}
      </Section>
      {gapsQ.data && gapsQ.data.evaluated > 0 && (
        <Section title="Чего не хватает по оценкам" loading={false} error={gapsQ.error}>
          <GapsSection m={gapsQ.data} />
        </Section>
      )}
      <Section
        title="Подходящие вакансии"
        loading={matchedQ.isLoading || closestQ.isLoading}
        error={byMatch ? matchedQ.error : (matchedQ.error ?? closestQ.error)}
        empty={
          !matchedQ.isLoading && !closestQ.isLoading && vacancies.length === 0
            ? "Подходящих вакансий пока нет."
            : null
        }
      >
        <VacanciesSection items={vacancies} byMatch={byMatch} />
      </Section>
    </div>
  );
}
