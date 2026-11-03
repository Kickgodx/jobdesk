import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type ProgressEvent } from "../api/client";
import MetricsCharts from "../components/MetricsCharts";
import VacancyTable from "../components/VacancyTable";
import type { Vacancy } from "../types/api";
import type { VacancySortField, VacancySortOrder } from "../types/sort";
import {
  elapsedSecondsBetween,
  elapsedSecondsUntil,
  formatDuration,
  parseSummaryFromRunParams,
} from "../utils/formatDuration";
import { filterActionableWarnings } from "../utils/runWarnings";
import { toVacancyListItem } from "../utils/vacancyListItem";

function formatUsd(v: number): string {
  if (v > 0 && v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

function AiStats({ runParams }: { runParams?: Record<string, unknown> | null }) {
  const aiStats = (runParams?.ai_stats ?? null) as Record<string, number> | null;
  const ai = (runParams?.ai ?? null) as { model?: string } | null;
  if (!aiStats) return null;
  const tin = aiStats.tokens_in ?? 0;
  const tout = aiStats.tokens_out ?? 0;
  const tsum = aiStats.tokens_total ?? tin + tout;
  const costIn = aiStats.cost_usd_input as number | undefined;
  const costOut = aiStats.cost_usd_output as number | undefined;
  const costTot = aiStats.cost_usd_total as number | undefined;
  return (
    <div className="ai-stats card-inner">
      <h4>AI</h4>
      <div className="metrics-row">
        <span>input: {tin.toLocaleString()}</span>
        <span>output: {tout.toLocaleString()}</span>
        <span>всего: {tsum.toLocaleString()}</span>
      </div>
      {ai?.model && (
        <p className="ai-model-line">
          Модель: <code>{ai.model}</code>
        </p>
      )}
      {costTot != null && (
        <div className="metrics-row cost-row">
          <span title="Стоимость input (USD)">input: {formatUsd(costIn ?? 0)}</span>
          <span title="Стоимость output (USD)">output: {formatUsd(costOut ?? 0)}</span>
          <span title="Стоимость всего (USD)">total: {formatUsd(costTot)}</span>
        </div>
      )}
      <p className="caption">
        Вызовов {aiStats.calls ?? 0} · кэш {aiStats.cache_hits ?? 0} · без LLM (score){" "}
        {aiStats.skipped_score ?? 0} · не-вакансий {aiStats.dropped_not_job ?? 0} · ошибок{" "}
        {aiStats.errors ?? 0}
      </p>
    </div>
  );
}

function RunTimingSummary({
  run,
  progress,
  isActive,
  nowMs,
}: {
  run: {
    started_at?: string | null;
    finished_at?: string | null;
    posts_fetched: number;
    run_params?: Record<string, unknown> | null;
  };
  progress: ProgressEvent | null;
  isActive: boolean;
  nowMs: number | null;
}) {
  const summary = parseSummaryFromRunParams(run.run_params);
  const elapsedSec =
    summary?.total_elapsed_seconds ??
    elapsedSecondsBetween(run.started_at, run.finished_at) ??
    (isActive && run.started_at && nowMs != null
      ? elapsedSecondsUntil(run.started_at, nowMs)
      : null);

  const postsProcessed = summary?.posts_processed ?? progress?.posts_total ?? run.posts_fetched;
  const postsSkipped =
    summary?.posts_skipped ??
    (progress && progress.posts_total > 0
      ? Math.max(0, progress.posts_processed - progress.vacancies_found)
      : null);

  if (elapsedSec == null && postsSkipped == null && !postsProcessed) return null;

  return (
    <p className="run-timing-summary">
      {elapsedSec != null && (
        <>
          Время парсинга: <strong>{formatDuration(elapsedSec)}</strong>
        </>
      )}
      {postsProcessed != null && postsProcessed > 0 && (
        <>
          {elapsedSec != null && " · "}
          обработано постов: <strong>{postsProcessed.toLocaleString()}</strong>
        </>
      )}
      {postsSkipped != null && (
        <>
          {(elapsedSec != null || postsProcessed) && " · "}
          пропущено постов: <strong>{postsSkipped.toLocaleString()}</strong>
          {!summary && isActive && (
            <span className="caption"> (без вакансии после фильтров, оценка)</span>
          )}
        </>
      )}
    </p>
  );
}

/** Каналы, где больше всего постов не стали вакансией: кандидаты на удаление из источников. */
function SkippedChannels({ runParams }: { runParams?: Record<string, unknown> | null }) {
  const summary = parseSummaryFromRunParams(runParams);
  const rows = Array.isArray(summary?.top_skipped_channels) ? summary.top_skipped_channels : [];
  if (!rows.length) return null;
  return (
    <div className="skipped-stats">
      <h4>Больше всего пропусков</h4>
      <ul className="skipped-channels">
        {rows.map((r) => (
          <li key={r.channel}>
            <a href={`https://t.me/${encodeURIComponent(r.channel)}`} target="_blank" rel="noreferrer">
              @{r.channel}
            </a>{" "}
            — пропущено <strong>{r.skipped.toLocaleString()}</strong> из {r.posts.toLocaleString()} (
            {Math.round((100 * r.skipped) / Math.max(1, r.posts))}%)
          </li>
        ))}
      </ul>
      <p className="caption">Пропущен — пост, который не стал вакансией после разбора и фильтров прогона.</p>
      {summary?.top_skipped_channels_incomplete && (
        <p className="caption warning">
          Часть постов не разобрана из-за LLM и тоже учтена как пропуск — список приблизительный.
        </p>
      )}
    </div>
  );
}

export default function RunPage() {
  const { runId } = useParams<{ runId: string }>();
  const qc = useQueryClient();
  const [stream, setStream] = useState<{
    runId: string;
    progress: ProgressEvent;
    vacancies: Vacancy[];
  } | null>(null);
  const [sortBy, setSortBy] = useState<VacancySortField>("post_date");
  const [sortOrder, setSortOrder] = useState<VacancySortOrder>("desc");
  const [nowMs, setNowMs] = useState<number | null>(null);

  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(runId!),
    enabled: !!runId,
  });

  const serverProgress = run.data?.progress ?? null;
  const streamForRun = stream?.runId === runId ? stream : null;
  const progress = streamForRun?.progress ?? serverProgress;
  const liveVacancies = streamForRun?.vacancies.length
    ? streamForRun.vacancies
    : (serverProgress?.vacancies_preview ?? []);

  const runFinished =
    run.data?.status === "completed" ||
    run.data?.status === "partial" ||
    run.data?.status === "failed" ||
    run.data?.status === "cancelled" ||
    progress?.phase === "post_processing_done";

  const vacancies = useQuery({
    queryKey: ["run-vacancies", runId, sortBy, sortOrder],
    queryFn: () => api.listRunVacancies(runId!, 1, 500, { sort_by: sortBy, sort_order: sortOrder }),
    enabled: !!runId && !!runFinished,
  });

  const [cancelError, setCancelError] = useState<string | null>(null);

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(runId!),
    onSuccess: () => {
      setCancelError(null);
      void qc.invalidateQueries({ queryKey: ["run", runId] });
    },
    onError: (err: Error) => {
      setCancelError(err.message || "Не удалось отправить отмену");
    },
  });

  useEffect(() => {
    if (!run.data?.started_at || run.data.status !== "running") return;
    const tick = () => setNowMs(Date.now());
    const first = window.setTimeout(tick, 0);
    const id = window.setInterval(tick, 1000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [run.data?.started_at, run.data?.status]);

  const [streamError, setStreamError] = useState<{
    runId: string;
    status: string;
    message: string;
  } | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportNote, setExportNote] = useState<string | null>(null);

  useEffect(() => {
    const status = run.data?.status;
    if (
      !runId ||
      (status !== "queued" && status !== "running")
    ) {
      return;
    }
    return api.subscribeRunEvents(
      runId,
      (snap) => {
        setStream((prev) => {
          const prevForRun = prev?.runId === runId ? prev : null;
          const preview = snap.vacancies_preview ?? [];
          return {
            runId,
            progress: snap,
            vacancies: preview.length ? preview : (prevForRun?.vacancies ?? []),
          };
        });
      },
      () => {
        qc.invalidateQueries({ queryKey: ["run", runId] });
        qc.invalidateQueries({ queryKey: ["run-vacancies", runId] });
      },
      (err) => {
        setStreamError({
          runId,
          status,
          message: err.message || "Ошибка подписки на прогресс",
        });
      },
    );
  }, [runId, run.data?.status, qc]);

  if (!runId) return null;

  // Вычитка и обработка идут потоком: пока не все каналы прочитаны, posts_total растёт,
  // поэтому долю обработанного масштабируем долей прочитанных каналов.
  const channelsTotal = progress?.channels_total ?? 0;
  const channelsFetched = progress?.channels_fetched ?? 0;
  const fetching = progress?.phase === "fetching" && channelsTotal > 0;
  const pct =
    progress && progress.posts_total > 0
      ? Math.round(
          ((100 * progress.posts_processed) / progress.posts_total) *
            (fetching ? channelsFetched / channelsTotal : 1),
        )
      : 0;

  const displayVacancies = vacancies.data?.items?.length
    ? vacancies.data.items
    : liveVacancies.map(toVacancyListItem);
  const vacancyTotal = vacancies.data?.total ?? progress?.vacancies_found ?? displayVacancies.length;

  const isActive =
    run.data?.status === "queued" ||
    run.data?.status === "running";
  const actionableWarnings = filterActionableWarnings(run.data?.parse_warnings);

  return (
    <>
      <p>
        <Link to="/">← Назад</Link>
      </p>

      <div className="card">
        <h2>Прогон {runId.slice(0, 8)}…</h2>
        {run.data && (
          <>
            <p>
              Статус: <strong>{run.data.status}</strong>
              {run.data.unique_vacancies != null && (
                <>
                  {" "}
                  · уникальных: <strong>{run.data.unique_vacancies}</strong>
                </>
              )}
            </p>
            {actionableWarnings.length > 0 && (
              <ul className="run-warnings">
                {actionableWarnings.map((w: string) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
            {run.data.error_message && <p className="error">{run.data.error_message}</p>}

            <RunTimingSummary run={run.data} progress={progress} isActive={isActive} nowMs={nowMs} />

            {isActive && (
              <>
                {run.data.status === "queued" && <p>В очереди…</p>}
                <div className="progress-bar">
                  <span style={{ width: `${pct}%` }} />
                </div>
                <p>
                  Постов: {progress?.posts_processed ?? 0} / {progress?.posts_total ?? "?"}, вакансий:{" "}
                  {progress?.vacancies_found ?? 0}, фаза: {progress?.phase ?? "…"}
                  {fetching && (
                    <>
                      , каналов прочитано: {channelsFetched} / {channelsTotal}
                    </>
                  )}
                </p>
                {progress?.ai_llm_disabled && (
                  <p className="warning">LLM отключён: {progress.ai_llm_disabled_reason}</p>
                )}
                {(progress?.ai_tokens_in || progress?.ai_tokens_out) ? (
                  <p className="caption">
                    Токены LLM: in={progress.ai_tokens_in}, out={progress.ai_tokens_out}
                  </p>
                ) : null}
                {progress?.phase === "post_processing_done" && run.data.status === "running" && (
                  <p>Сохранение в базу…</p>
                )}
                {(run.data.status === "queued" ||
                  (run.data.status === "running" && progress?.phase !== "post_processing_done")) && (
                  <>
                    <button className="secondary" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                      {cancel.isPending ? "Отмена…" : "Отменить"}
                    </button>
                    {cancelError && <p className="error">{cancelError}</p>}
                    {streamError &&
                      streamError.runId === runId &&
                      streamError.status === run.data.status && (
                        <p className="error">{streamError.message}</p>
                      )}
                  </>
                )}
              </>
            )}

            {!isActive && run.data.metrics && (
              <>
                <p className="success">
                  Постов: {run.data.posts_fetched}; уникальных: {run.data.metrics.unique_vacancies};
                  дубликатов: {run.data.metrics.duplicate_count}
                </p>
                <SkippedChannels runParams={run.data.run_params} />
                <AiStats runParams={run.data.run_params} />
                <div className="export-links">
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => {
                      setExportError(null);
                      setExportNote(null);
                      void api
                        .downloadExport(api.exportRunPath(runId, "xlsx"), `run-${runId.slice(0, 8)}.xlsx`)
                        .then(setExportNote)
                        .catch((e: Error) => setExportError(e.message));
                    }}
                  >
                    Скачать Excel
                  </button>
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => {
                      setExportError(null);
                      setExportNote(null);
                      void api
                        .downloadExport(api.exportRunPath(runId, "json"), `run-${runId.slice(0, 8)}.json`)
                        .then(setExportNote)
                        .catch((e: Error) => setExportError(e.message));
                    }}
                  >
                    Скачать JSON
                  </button>
                </div>
                {exportError && <p className="error">{exportError}</p>}
                {exportNote && <p className="warning">{exportNote}</p>}
              </>
            )}
          </>
        )}
      </div>

      {run.data?.metrics && !isActive && (
        <div className="card">
          <h3>Графики</h3>
          <MetricsCharts metrics={run.data.metrics} />
        </div>
      )}

      {displayVacancies.length > 0 && (
        <div className="card">
          <VacancyTable
            items={displayVacancies}
            title={`Вакансии (${vacancyTotal})`}
            sortBy={sortBy}
            sortOrder={sortOrder}
            onSortChange={
              runFinished && vacancies.data?.items?.length
                ? (by, order) => {
                    setSortBy(by);
                    setSortOrder(order);
                  }
                : undefined
            }
          />
        </div>
      )}

      {runFinished && !displayVacancies.length && !vacancies.isLoading && (
        <div className="card">
          <p>Вакансий не найдено или ещё не сохранены в БД.</p>
        </div>
      )}
    </>
  );
}
