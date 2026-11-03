import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { MarketShare } from "../types/api";
import { formatThousands as k } from "../utils/formatSalary";
import {
  loadResumeMarketPrefs,
  PEER_OPTIONS,
  type ResumeMarketPrefs,
  saveResumeMarketPrefs,
} from "../utils/resumeMarketPrefs";
import { SalaryStrip } from "./MarketSalaryBlock";

function pct(share: number): string {
  return `${Math.round(share * 100)}%`;
}

function shares(items: MarketShare[]): string {
  return items.map((s) => `${s.name} (${pct(s.share)})`).join(", ");
}

/** «Резюме и рынок»: что платят и что требуют в вакансиях, ближайших к резюме по смыслу. */
export default function ResumeMarketPanel({ resumeId }: { resumeId: number }) {
  const [prefs, setPrefs] = useState(loadResumeMarketPrefs);
  const { peers, grade } = prefs;
  const update = (patch: Partial<ResumeMarketPrefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    saveResumeMarketPrefs(next);
  };
  const defaultsQ = useQuery({ queryKey: ["config-defaults"], queryFn: () => api.getConfigDefaults() });
  const gradeOptions = (defaultsQ.data?.grade_options ?? []).filter((g) => g !== "unknown");
  const q = useQuery({
    queryKey: ["resume-market", resumeId, peers, grade],
    queryFn: () => api.getResumeMarket(resumeId, { peers, grade: grade || undefined }),
    staleTime: 60_000,
    retry: false,
    placeholderData: keepPreviousData,
  });
  const m = q.data;
  const unavailable = q.error instanceof ApiError && q.error.code === "UPSTREAM_UNAVAILABLE";
  return (
    <div className="card">
      <h3>Резюме и рынок</h3>
      <div className="filters-panel">
        <label title="Больше вакансий — устойчивее доли, но в выборку попадают менее похожие на резюме">
          Вакансий{" "}
          <select value={peers} onChange={(e) => update({ peers: Number(e.target.value) })}>
            {PEER_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label title="Только вакансии выбранного грейда: что платят и требуют на этом уровне">
          Грейд{" "}
          <select value={grade} onChange={(e) => update({ grade: e.target.value })}>
            <option value="">Все</option>
            {gradeOptions.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </label>
        {q.isFetching && !q.isLoading && <span className="caption">Обновление…</span>}
      </div>
      {q.isLoading && <p>Загрузка…</p>}
      {unavailable && (
        <p className="muted">Нужны эмбеддинги (смысловой поиск): {(q.error as Error).message}</p>
      )}
      {q.error && !unavailable && <p className="error">{(q.error as Error).message}</p>}
      {m && m.peers === 0 && !m.grade && (
        <p className="muted">Вакансий с векторами пока нет — индексация идёт в фоне каждые несколько минут.</p>
      )}
      {m && m.peers === 0 && m.grade && (
        <p className="muted">Вакансий грейда {m.grade} за год нет — выберите другой грейд.</p>
      )}
      {m && m.peers > 0 && (
        <>
          <p className="caption">
            По {m.peers} вакансиям{m.grade ? ` грейда ${m.grade}` : ""}, ближайшим к тексту резюме по смыслу,
            за {Math.round(m.window_days / 30)} месяцев (с архивом).
          </p>
          {m.specializations.length > 0 && (
            <p>
              Роли: <strong>{shares(m.specializations)}</strong>
            </p>
          )}
          {!m.grade && m.grades.length > 0 && (
            <p>
              Грейды: <strong>{shares(m.grades)}</strong>
            </p>
          )}
          {m.median != null ? (
            <aside className="market-salary">
              <p>
                Платят обычно{" "}
                <strong>
                  {k(m.p25)} – {k(m.p75)}
                </strong>
                , медиана <strong>{k(m.median)}</strong>
                <span className="muted">
                  {" "}
                  (p10–p90: {k(m.p10)} – {k(m.p90)}), ₽ в месяц
                </span>
              </p>
              <SalaryStrip q={m} />
              <p className="caption">По {m.salary_count} ближайшим вакансиям с зарплатой.</p>
            </aside>
          ) : (
            <p className="muted">Ближайших вакансий с зарплатой мало — вилку не оценить.</p>
          )}
          {m.technologies.length > 0 && (
            <>
              <h4>Что требуют</h4>
              {m.coverage != null && (
                <p>
                  В среднем резюме называет <strong>{pct(m.coverage)}</strong> технологий из вакансии.
                </p>
              )}
              <table>
                <thead>
                  <tr>
                    <th>Технология</th>
                    <th title="Доля ближайших вакансий с этой технологией">Встречается</th>
                    <th>В резюме</th>
                  </tr>
                </thead>
                <tbody>
                  {m.technologies.map((t) => (
                    <tr key={t.name}>
                      <td>
                        <strong>{t.name}</strong>
                      </td>
                      <td>{pct(t.share)}</td>
                      <td className={t.in_resume ? "delta-up" : "delta-down"}>
                        {t.in_resume ? "есть" : "нет"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="caption">
                «Нет» — технология не названа в тексте резюме. Если опыт с ней есть, его стоит упомянуть.
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}
