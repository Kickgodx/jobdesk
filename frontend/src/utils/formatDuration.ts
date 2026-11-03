/** Форматирование длительности в секундах для UI прогона. */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  if (total < 60) return `${total} с`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return s > 0 ? `${m} м ${s} с` : `${m} м`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h} ч ${rm} м` : `${h} ч`;
}

export interface ParseSummary {
  total_elapsed_seconds?: number;
  fetch_elapsed_seconds?: number;
  process_elapsed_seconds?: number;
  dedup_elapsed_seconds?: number;
  posts_fetched?: number;
  posts_processed?: number;
  posts_skipped?: number;
  vacancy_candidates?: number;
  /** Каналы с наибольшим числом постов, не ставших вакансией (по убыванию пропусков). */
  top_skipped_channels?: SkippedChannel[];
  /** Часть постов потеряна из-за LLM и учтена как пропуск — список приблизительный. */
  top_skipped_channels_incomplete?: boolean;
}

export interface SkippedChannel {
  channel: string;
  posts: number;
  skipped: number;
}

export function parseSummaryFromRunParams(
  runParams?: Record<string, unknown> | null,
): ParseSummary | null {
  const raw = runParams?.parse_summary;
  if (!raw || typeof raw !== "object") return null;
  return raw as ParseSummary;
}

export function elapsedSecondsBetween(isoStart?: string | null, isoEnd?: string | null): number | null {
  if (!isoStart || !isoEnd) return null;
  const ms = new Date(isoEnd).getTime() - new Date(isoStart).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms / 1000;
}

/** Секунды от ISO-старта до уже известной отметки времени (мс). */
export function elapsedSecondsUntil(isoStart: string | null | undefined, endMs: number): number | null {
  if (!isoStart || !Number.isFinite(endMs)) return null;
  const startMs = Date.parse(isoStart);
  if (!Number.isFinite(startMs)) return null;
  const ms = endMs - startMs;
  if (ms < 0) return null;
  return ms / 1000;
}
