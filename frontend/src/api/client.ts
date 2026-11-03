import type {
  AskMarketResult,
  AskMarketTurn,
  AuthUser,
  CompanyStats,
  LifetimeAnalytics,
  MarketMap,
  RecruiterContacts,
  SkillWhatIf,
  CareerPaths,
  ApiToken,
  ApiTokenCreated,
  ApiTokenScope,
  ConfigDefaults,
  CoverLetter,
  CoverLetterTone,
  HhStatus,
  HhSyncAccepted,
  HhSyncJob,
  HhSyncRequest,
  LlmJob,
  LlmJobAccepted,
  LlmModelsCatalog,
  MetricsSummary,
  ProgressEvent,
  Resume,
  SkillGaps,
  ResumeMarket,
  MatchBatchResponse,
  SemanticStatus,
  SimilarVacanciesResponse,
  ResumeFileText,
  ResumeTailoring,
  InterviewPrep,
  CompanyProfile,
  ChannelCandidate,
  Digest,
  DigestListItem,
  VacancyFlags,
  VacancyHistory,
  MarketSalary,
  SalaryEstimate,
  TechnologyAnalytics,
  TechnologyGraph,
  RunCreateBody,
  RunDetail,
  RunStatus,
  SalaryAnalytics,
  SavedFilter,
  SavedFilterParams,
  SourceStats,
  SseTicketResponse,
  TechnologyCount,
  TokenResponse,
  Vacancy,
  VacancyImportOutcome,
  VacancyImportResponse,
  VacancyListItem,
  VacancyMatch,
  VacancyReextractInfo,
  VacancyReextractResponse,
} from "../types/api";

// По умолчанию — тот же origin, что у UI (dev: прокси Vite, prod: nginx): refresh-cookie
// SameSite=Strict работает только когда UI и API — один сайт.
const API_BASE = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") || "/api/v1";

const VITE_API_KEY = (import.meta.env.VITE_API_KEY as string | undefined)?.trim() || "";

/**
 * Access-токен живёт только в памяти вкладки (не в localStorage — XSS не унесёт его
 * между сессиями). Сессию держит httpOnly refresh-cookie: на старте и при 401
 * access берётся заново через POST /auth/refresh.
 */
let accessToken: string | null = null;

try {
  localStorage.removeItem("tgjp_token"); // прежнее хранилище access-токена
} catch {
  /* storage недоступен */
}

export function getToken(): string | null {
  return accessToken;
}

export function setToken(token: string | null) {
  accessToken = token;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;

  constructor(
    message: string,
    opts: { status: number; code?: string; retryable?: boolean },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code ?? "INTERNAL";
    this.retryable = opts.retryable ?? false;
  }
}

/**
 * `retry` для запросов, которые иначе не повторяются (`me`): повторить до 3 раз только
 * retryable-ошибку — 503 OVERLOADED под перегрузкой, сбой сети. 401/403 — сразу.
 */
export function retryIfRetryable(failureCount: number, error: Error): boolean {
  return error instanceof ApiError && error.retryable && failureCount < 3;
}

function apiKeyHeaders(): Record<string, string> {
  return VITE_API_KEY ? { "X-API-Key": VITE_API_KEY } : {};
}

function authHeaders(): Record<string, string> {
  const headers = apiKeyHeaders();
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

let refreshing: Promise<boolean> | null = null;

/**
 * Обновить access-токен по refresh-cookie. Параллельные вызовы делят один запрос:
 * cookie ротируется, второй обмен того же значения был бы лишним.
 */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: apiKeyHeaders(),
      });
      if (!res.ok) {
        setToken(null);
        return false;
      }
      setToken(((await res.json()) as TokenResponse).access_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

/** 401 здесь — «неверные креды» / «нет сессии», а не протухший access: без refresh и редиректа. */
const NO_REFRESH_PATHS = ["/auth/login", "/auth/register", "/auth/refresh", "/auth/logout"];

function handleUnauthorized() {
  setToken(null);
  if (window.location.pathname !== "/login") window.location.assign("/login");
}

/**
 * fetch к API с access-токеном. На 401 — один тихий refresh и повтор запроса
 * (401 отдаётся до побочных эффектов, повтор POST безопасен); не вышло — на /login.
 * credentials: "include" — чтобы браузер принял и отправил refresh-cookie (path /auth).
 */
async function authFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = () =>
    fetch(`${API_BASE}${path}`, {
      credentials: "include",
      ...init,
      headers: { ...authHeaders(), ...init.headers },
    });
  let res = await send();
  if (res.status !== 401 || NO_REFRESH_PATHS.some((p) => path.startsWith(p))) return res;
  if (await refreshSession()) res = await send();
  if (res.status === 401) handleUnauthorized();
  return res;
}

function parseErrorDetail(
  status: number,
  detail: unknown,
  fallback: string,
): ApiError {
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { code?: unknown; message?: unknown; retryable?: unknown };
    const message =
      typeof d.message === "string"
        ? d.message
        : typeof detail === "string"
          ? detail
          : fallback;
    return new ApiError(message, {
      status,
      code: typeof d.code === "string" ? d.code : "INTERNAL",
      retryable: d.retryable === true,
    });
  }
  if (typeof detail === "string") {
    return new ApiError(detail, { status, code: "INTERNAL" });
  }
  return new ApiError(fallback, { status, code: "INTERNAL" });
}

export interface RunSummary {
  id: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  posts_fetched: number;
  parse_warnings?: string | null;
  unique_vacancies?: number | null;
}

export interface VacancyListResponse {
  items: VacancyListItem[];
  total: number;
  page: number;
  page_size: number;
}

export type VacancyQueryParams = Record<string, string | number | boolean | string[]>;

function appendSearchParams(q: URLSearchParams, params?: VacancyQueryParams) {
  if (!params) return;
  Object.entries(params).forEach(([k, v]) => {
    if (v === "" || v === undefined) return;
    if (Array.isArray(v)) {
      v.forEach((item) => {
        if (item !== "") q.append(k, String(item));
      });
    } else {
      q.set(k, String(v));
    }
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail ?? j;
    } catch {
      detail = await res.text();
    }
    throw parseErrorDetail(res.status, detail, res.statusText);
  }
  return res.json() as Promise<T>;
}

/**
 * URL SSE с одноразовым тикетом (POST /auth/sse-ticket): EventSource не шлёт
 * заголовки, а JWT в query оседал бы в логах прокси. Тикет живёт секунды и
 * принимается один раз — автопереподключение EventSource с ним получит 401,
 * поэтому обрыв везде ведёт в fallback-poll.
 */
async function sseUrl(path: string, signal?: AbortSignal): Promise<string> {
  const { ticket } = await request<SseTicketResponse>("/auth/sse-ticket", { method: "POST", signal });
  return `${API_BASE}${path}?ticket=${encodeURIComponent(ticket)}`;
}

/**
 * EventSource по тикету. Тикет запрашивается асинхронно: `setup` вызывается, когда
 * источник открыт, `onFail` — если тикет не получен; `close()` можно звать в любой момент.
 */
function openSse(
  path: string,
  setup: (es: EventSource) => void,
  onFail: () => void,
): { close: () => void } {
  let es: EventSource | null = null;
  let closed = false;
  void sseUrl(path).then(
    (url) => {
      if (closed) return;
      es = new EventSource(url);
      setup(es);
    },
    () => {
      if (!closed) onFail();
    },
  );
  return {
    close: () => {
      closed = true;
      es?.close();
    },
  };
}

const LLM_JOB_POLL_MS = 1000;
/**
 * Потолок ожидания LLM-джобы на клиенте — страховка от вечного ожидания. До error
 * джобу доводит backend (LLM_RQ_JOB_TIMEOUT_SECONDS × LLM_JOB_MAX_ATTEMPTS + recover);
 * сама джоба по таймауту клиента не отменяется, матч/письмо попадут в кэш.
 */
const LLM_JOB_WAIT_MS = 30 * 60 * 1000;
const HH_SYNC_POLL_MS = 1500;
const HH_SYNC_TERMINAL: ReadonlySet<HhSyncJob["status"]> = new Set(["completed", "failed", "cancelled"]);
const LLM_JOB_TERMINAL: ReadonlySet<LlmJob["status"]> = new Set(["done", "error", "cancelled"]);
const RUN_POLL_MS = 2000;
/** Как часто SSE переключается на poll при долгом молчании (не hard-fail). */
const SSE_FALLBACK_IDLE_MS = 5 * 60 * 1000;

const RUN_TERMINAL: ReadonlySet<RunStatus> = new Set([
  "completed",
  "partial",
  "failed",
  "cancelled",
]);

async function pollRunUntilTerminal(
  runId: string,
  onProgress?: (data: ProgressEvent) => void,
  signal?: { cancelled: boolean },
): Promise<RunDetail> {
  while (!signal?.cancelled) {
    const run = await request<RunDetail>(`/runs/${runId}`);
    if (run.progress) onProgress?.(run.progress);
    if (RUN_TERMINAL.has(run.status)) return run;
    await sleep(RUN_POLL_MS);
  }
  throw new ApiError("Подписка на прогон отменена", { status: 499, code: "CANCELLED" });
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** Промежуточные статусы джобы (у импорта — растущий `import_result`). */
type LlmJobListener = (job: LlmJob) => void;

/** Сигнал ожидания джобы: отмена вызывающим (`signal`) или таймаут LLM_JOB_WAIT_MS. */
function llmJobWaitSignal(signal?: AbortSignal): { signal: AbortSignal; dispose: () => void } {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => {
    ctrl.abort(
      new ApiError("LLM: задача выполняется слишком долго — откройте результат позже", {
        status: 408,
        code: "TIMEOUT",
        retryable: true,
      }),
    );
  }, LLM_JOB_WAIT_MS);
  const onAbort = () => ctrl.abort(signal?.reason);
  if (signal?.aborted) onAbort();
  else signal?.addEventListener("abort", onAbort, { once: true });
  return {
    signal: ctrl.signal,
    dispose: () => {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    },
  };
}

async function pollLlmJob(
  jobId: string,
  onUpdate: LlmJobListener | undefined,
  signal: AbortSignal,
): Promise<LlmJob> {
  for (;;) {
    signal.throwIfAborted();
    const job = await request<LlmJob>(`/llm-jobs/${jobId}`, { signal });
    if (LLM_JOB_TERMINAL.has(job.status)) return job;
    onUpdate?.(job);
    await sleep(LLM_JOB_POLL_MS, signal);
  }
}

/** Ждать завершения через SSE; при обрыве — poll до terminal или до `signal`. */
async function waitLlmJobSse(
  jobId: string,
  onUpdate: LlmJobListener | undefined,
  signal: AbortSignal,
): Promise<LlmJob> {
  const url = await sseUrl(`/llm-jobs/${jobId}/events`, signal);
  signal.throwIfAborted();

  return new Promise((resolve, reject) => {
    let last: LlmJob | null = null;
    let settled = false;
    const timer = window.setTimeout(() => {
      // Долгое молчание SSE → poll, не ошибка.
      if (!settled) startFallbackPoll();
    }, SSE_FALLBACK_IDLE_MS);

    const es = new EventSource(url);

    const close = () => {
      settled = true;
      window.clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      es.close();
    };

    const onAbort = () => {
      if (settled) return;
      close();
      reject(signal.reason);
    };
    signal.addEventListener("abort", onAbort, { once: true });

    const finish = (job: LlmJob) => {
      if (settled) return;
      close();
      resolve(job);
    };

    const startFallbackPoll = () => {
      if (settled) return;
      close();
      void pollLlmJob(jobId, onUpdate, signal).then(resolve, reject);
    };

    es.addEventListener("status", (ev) => {
      try {
        last = JSON.parse((ev as MessageEvent).data) as LlmJob;
        if (LLM_JOB_TERMINAL.has(last.status)) finish(last);
        else onUpdate?.(last);
      } catch {
        /* ignore parse */
      }
    });

    es.addEventListener("done", () => {
      if (last && LLM_JOB_TERMINAL.has(last.status)) {
        finish(last);
        return;
      }
      startFallbackPoll();
    });

    es.onerror = () => {
      if (settled) return;
      if (last) {
        startFallbackPoll();
        return;
      }
      window.setTimeout(() => {
        if (!settled && !last) startFallbackPoll();
      }, 1500);
    };
  });
}

async function awaitLlmJob(
  jobId: string,
  onUpdate?: LlmJobListener,
  signal?: AbortSignal,
): Promise<LlmJob> {
  const wait = llmJobWaitSignal(signal);
  try {
    return await waitLlmJobSse(jobId, onUpdate, wait.signal);
  } catch (err) {
    if (wait.signal.aborted) throw err;
    return await pollLlmJob(jobId, onUpdate, wait.signal);
  } finally {
    wait.dispose();
  }
}

/**
 * POST мэтч/письмо/импорт: 200 готовый результат или 202 job → SSE (fallback poll).
 * `signal` отменяет и запрос, и ожидание джобы (закрыли модалку, ушли со страницы).
 */
interface InteractiveLlmOptions<T> {
  /** 202: id поставленной задачи — для её отмены (`api.cancelLlmJob`). */
  onAccepted?: (jobId: string) => void;
  /** Итог отменённой задачи; без него отмена — `ApiError` с кодом CANCELLED. */
  pickCancelled?: (job: LlmJob) => T | null | undefined;
}

async function postInteractiveLlm<T>(
  path: string,
  init: RequestInit,
  pick: (job: LlmJob) => T | null | undefined,
  onUpdate?: LlmJobListener,
  signal?: AbortSignal,
  options: InteractiveLlmOptions<T> = {},
): Promise<T> {
  const res = await authFetch(path, {
    ...init,
    signal,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (res.status === 200) return (await res.json()) as T;
  if (res.status === 202) {
    const accepted = (await res.json()) as LlmJobAccepted;
    options.onAccepted?.(accepted.job_id);
    const job = await awaitLlmJob(accepted.job_id, onUpdate, signal);
    if (job.status === "cancelled") {
      const partial = options.pickCancelled?.(job);
      if (partial != null) return partial;
      throw new ApiError("LLM: задача отменена", { status: 499, code: "CANCELLED" });
    }
    if (job.status === "error") {
      throw new ApiError(job.error_message || "LLM: ошибка задачи", {
        status: 500,
        code: "INTERNAL",
        retryable: true,
      });
    }
    const value = pick(job);
    if (value == null) {
      throw new ApiError("LLM: пустой результат задачи", { status: 500, code: "INTERNAL" });
    }
    return value;
  }
  let detail: unknown = res.statusText;
  try {
    const j = await res.json();
    detail = j.detail ?? j;
  } catch {
    detail = await res.text();
  }
  throw parseErrorDetail(res.status, detail, res.statusText);
}

export const api = {
  health: () => request<{ status: string; database: boolean }>("/health"),

  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  register: (email: string, password: string) =>
    request<TokenResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<AuthUser>("/auth/me"),

  listApiTokens: () => request<ApiToken[]>("/auth/tokens"),

  createApiToken: (name: string, scopes: ApiTokenScope[], expiresDays: number) =>
    request<ApiTokenCreated>("/auth/tokens", {
      method: "POST",
      body: JSON.stringify({ name, scopes, expires_days: expiresDays }),
    }),

  revokeApiToken: async (id: number) => {
    const res = await authFetch(`/auth/tokens/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`Отзыв токена: ${res.status} ${res.statusText}`);
  },

  /** Отозвать refresh-сессию на сервере (cookie стирается) и уйти на /login. */
  logout: async () => {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: "POST",
        credentials: "include",
        headers: apiKeyHeaders(),
      });
    } catch {
      /* сеть недоступна — локально всё равно выходим */
    }
    setToken(null);
    window.location.assign("/login");
  },

  getConfigDefaults: () => request<ConfigDefaults>("/config/defaults"),

  getLlmModels: () => request<LlmModelsCatalog>("/config/llm-models"),

  listRuns: () => request<RunSummary[]>("/runs"),

  getRun: (id: string) => request<RunDetail>(`/runs/${id}`),

  createRun: (body: RunCreateBody) =>
    request<{ run_id: string; status: RunStatus }>("/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  cancelRun: (id: string) => request<RunDetail>(`/runs/${id}/cancel`, { method: "POST" }),

  getAiCacheStats: () => request<{ count: number }>("/ai-cache/stats"),

  clearAiCache: () =>
    request<{ deleted: number; remaining: number }>("/ai-cache/clear", { method: "POST" }),

  listVacancies: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<VacancyListResponse>(`/vacancies${qs ? `?${qs}` : ""}`);
  },

  getVacancy: (id: string) => request<Vacancy>(`/vacancies/${encodeURIComponent(id)}`),
  getVacancyHistory: (id: string) => request<VacancyHistory>(`/vacancies/${id}/history`),

  getRecruiterContacts: (id: string) =>
    request<RecruiterContacts>(`/vacancies/${encodeURIComponent(id)}/contacts`),
  getVacancyFlags: (id: string) =>
    request<VacancyFlags>(`/vacancies/${encodeURIComponent(id)}/flags`),
  listChannelCandidates: (includeIgnored: boolean) =>
    request<{ items: ChannelCandidate[] }>(
      `/channels/candidates${includeIgnored ? "?include_ignored=true" : ""}`,
    ),

  setChannelCandidateStatus: (channelId: number, status: ChannelCandidate["status"]) =>
    request<{ status: string }>(`/channels/candidates/${channelId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  listDigests: () => request<{ items: DigestListItem[] }>("/digests"),

  getDigest: (periodStart: string) => request<Digest>(`/digests/${periodStart}`),

  /** Админ: сформировать обзор недели (по умолчанию прошедшей); LLM-вызов идёт в запросе. */
  generateDigest: (weekStart: string | null, force: boolean) =>
    request<Digest>("/digests", {
      method: "POST",
      body: JSON.stringify({ week_start: weekStart, force }),
    }),

  getCompanyProfile: (name: string) =>
    request<CompanyProfile>(`/companies/profile?${new URLSearchParams({ name }).toString()}`),
  getSimilarVacancies: (id: string, limit = 8) =>
    request<SimilarVacanciesResponse>(`/vacancies/${encodeURIComponent(id)}/similar?limit=${limit}`),
  getSemanticStatus: () => request<SemanticStatus>("/vacancies/semantic-status"),

  /** LLM-оценка резюме по нескольким вакансиям одной задачей; `onProgress` — частичный итог. */
  matchBatch: (
    resumeId: number,
    vacancyIds: string[],
    onProgress?: (partial: MatchBatchResponse) => void,
    signal?: AbortSignal,
  ) => {
    const empty: MatchBatchResponse = { total: vacancyIds.length, items: [], scored: 0, skipped: 0, errors: 0 };
    return postInteractiveLlm<MatchBatchResponse>(
      `/resumes/${resumeId}/match-batch`,
      { method: "POST", body: JSON.stringify({ vacancy_ids: vacancyIds }) },
      (job) => job.match_batch_result ?? empty,
      (job) => {
        if (job.match_batch_result) onProgress?.(job.match_batch_result);
      },
      signal,
      { pickCancelled: (job) => job.match_batch_result ?? empty },
    );
  },
  getMarketSalary: (id: string) =>
    request<MarketSalary>(`/vacancies/${encodeURIComponent(id)}/market-salary`),
  getSalaryEstimate: (id: string) =>
    request<SalaryEstimate>(`/vacancies/${encodeURIComponent(id)}/salary-estimate`),

  /** Технологии по числу вакансий под фильтрами (собственный выбор технологий не учитывается). */
  getTechnologyFacets: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<{ items: TechnologyCount[] }>(`/vacancies/technologies${qs ? `?${qs}` : ""}`);
  },

  /** Сколько вакансий без специализации ждут переразбора LLM (admin). */
  getReextractInfo: () => request<VacancyReextractInfo>("/vacancies/reextract"),

  /**
   * Переразобрать LLM вакансии без специализации (admin): 202 и джоба, `onProgress` — частичный итог.
   * Уже идёт переразбор — backend вернёт её job_id, ожидание подхватит её.
   */
  reextractVacancies: async (
    onProgress?: (partial: VacancyReextractResponse) => void,
    signal?: AbortSignal,
    onAccepted?: (jobId: string) => void,
  ): Promise<{ result: VacancyReextractResponse; cancelled: boolean }> => {
    let cancelled = false;
    const empty: VacancyReextractResponse = { total: 0, items: [], updated: 0, kept: 0, errors: 0 };
    const result = await postInteractiveLlm<VacancyReextractResponse>(
      "/vacancies/reextract",
      { method: "POST" },
      (job) => job.reextract_result ?? empty,
      (job) => {
        if (job.reextract_result) onProgress?.(job.reextract_result);
      },
      signal,
      {
        onAccepted,
        pickCancelled: (job) => {
          cancelled = true;
          return job.reextract_result ?? empty;
        },
      },
    );
    return { result, cancelled };
  },

  /** Агрегаты для графиков (те же фильтры, что у listVacancies, без пагинации). */
  getFxRates: () =>
    request<{ rate_date: string | null; source: string; rates: { currency: string; rate_to_rub: number }[] }>(
      "/fx/rates",
    ),

  refreshFxRates: () =>
    request<{
      rate_date: string;
      rates_count: number;
      history_days_synced: number;
      vacancies_processed: number;
      vacancies_updated_rub: number;
      vacancies_skipped_no_rate: number;
    }>("/fx/refresh", { method: "POST" }),

  /** Пересчёт зарплат в ₽ по последнему курсу в БД, без запроса к ЦБ. */
  recalculateSalaryRub: () =>
    request<{
      rate_date: string | null;
      vacancies_processed: number;
      vacancies_updated_rub: number;
      vacancies_skipped_no_rate: number;
    }>("/fx/recalculate-rub", { method: "POST" }),

  getVacancyMetrics: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<MetricsSummary>(`/vacancies/metrics${qs ? `?${qs}` : ""}`);
  },

  getSalaryAnalytics: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<SalaryAnalytics>(`/analytics/salaries${qs ? `?${qs}` : ""}`);
  },

  getSourceStats: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<{ items: SourceStats[] }>(`/analytics/sources${qs ? `?${qs}` : ""}`);
  },

  getTechnologyAnalytics: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<TechnologyAnalytics>(`/analytics/technologies${qs ? `?${qs}` : ""}`);
  },
  getTechnologyGraph: (params?: VacancyQueryParams, nodes = 40) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    q.set("nodes", String(nodes));
    return request<TechnologyGraph>(`/analytics/technology-graph?${q.toString()}`);
  },
  getLifetimeAnalytics: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<LifetimeAnalytics>(`/analytics/lifetime${qs ? `?${qs}` : ""}`);
  },
  getCompanyStats: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return request<{ items: CompanyStats[] }>(`/analytics/companies${qs ? `?${qs}` : ""}`);
  },

  listFilters: () => request<{ items: SavedFilter[] }>("/filters"),

  createFilter: (name: string, params: SavedFilterParams) =>
    request<SavedFilter>("/filters", {
      method: "POST",
      body: JSON.stringify({ name, params }),
    }),

  deleteFilter: async (id: number) => {
    const res = await authFetch(`/filters/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`Удаление пресета: ${res.status} ${res.statusText}`);
  },

  exportVacanciesPath: (params?: VacancyQueryParams) => {
    const q = new URLSearchParams();
    appendSearchParams(q, params);
    const qs = q.toString();
    return `/vacancies/export.xlsx${qs ? `?${qs}` : ""}`;
  },

  /**
   * Скачать export с Authorization / X-API-Key (вместо голого &lt;a href&gt;).
   * Возвращает пометку об обрезке («выгружено N из M»), если backend упёрся в EXPORT_MAX_ROWS.
   */
  downloadExport: async (path: string, filename: string): Promise<string | null> => {
    const res = await authFetch(path);
    if (!res.ok) {
      let detail: unknown = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail ?? j;
      } catch {
        detail = await res.text();
      }
      throw parseErrorDetail(res.status, detail, res.statusText);
    }
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(objectUrl);
    const truncated = res.headers.get("X-Export-Truncated");
    if (!truncated) return null;
    const [exported, total] = truncated.split("/");
    return `Выгружено ${exported} из ${total} — лимит экспорта. Уточните фильтры, чтобы выгрузить остальное.`;
  },

  listRunVacancies: (
    runId: string,
    page = 1,
    pageSize = 100,
    sort?: { sort_by?: string; sort_order?: string },
  ) => {
    const q = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (sort?.sort_by) q.set("sort_by", sort.sort_by);
    if (sort?.sort_order) q.set("sort_order", sort.sort_order);
    return request<VacancyListResponse>(`/runs/${runId}/vacancies?${q}`);
  },

  exportRunPath: (runId: string, kind: "xlsx" | "json") => `/runs/${runId}/export.${kind}`,

  /** Ручной импорт: тексты вакансий → rules + LLM → запись в БД. */
  /** Без LLM — сразу 200; с LLM — 202 и джоба, `onProgress` получает частичный итог. */
  /** Отменённый импорт резолвится итогом по уже обработанным текстам (`cancelled: true`). */
  importVacancies: async (
    texts: string[],
    sourceUrl?: string,
    onProgress?: (partial: VacancyImportResponse) => void,
    signal?: AbortSignal,
    onAccepted?: (jobId: string) => void,
  ): Promise<VacancyImportOutcome> => {
    let cancelled = false;
    const result = await postInteractiveLlm<VacancyImportResponse>(
      "/vacancies/import",
      { method: "POST", body: JSON.stringify({ texts, source_url: sourceUrl || null }) },
      (job) => job.import_result,
      (job) => {
        if (job.import_result) onProgress?.(job.import_result);
      },
      signal,
      {
        onAccepted,
        pickCancelled: (job) => {
          cancelled = true;
          return job.import_result ?? { items: [], created: 0, existing: 0, rejected: 0, ai_enabled: true, total: texts.length };
        },
      },
    );
    return { result, cancelled };
  },

  /** Отмена своей LLM-задачи; ответ — её текущее состояние (и у уже завершённой). */
  /** «Спросить базу»: 202 job → ответ по SSE/poll; onAccepted — id задачи для отмены. */
  askMarket: (
    question: string,
    history: AskMarketTurn[],
    signal?: AbortSignal,
    onAccepted?: (jobId: string) => void,
  ) =>
    postInteractiveLlm<AskMarketResult>(
      "/market-chat/ask",
      { method: "POST", body: JSON.stringify({ question, history }) },
      (job) => job.ask_market_result,
      undefined,
      signal,
      { onAccepted },
    ),

  cancelLlmJob: (jobId: string) => request<LlmJob>(`/llm-jobs/${jobId}/cancel`, { method: "POST" }),

  hhStatus: () => request<HhStatus>("/hh/status"),

  /** 202: sync поставлен в очередь; ход и итог — hhSyncJob / subscribeHhSyncJob. */
  hhSync: (body?: HhSyncRequest) =>
    request<HhSyncAccepted>("/hh/sync", {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    }),

  hhSyncJobs: (limit = 10) => request<HhSyncJob[]>(`/hh/sync-jobs?limit=${limit}`),

  hhSyncJob: (jobId: string) => request<HhSyncJob>(`/hh/sync-jobs/${jobId}`),

  /** Отмена sync (admin); ответ — текущее состояние sync. */
  cancelHhSyncJob: (jobId: string) =>
    request<HhSyncJob>(`/hh/sync-jobs/${jobId}/cancel`, { method: "POST" }),

  /** SSE со статусом sync до completed/failed/cancelled; при обрыве — poll. Возвращает отписку. */
  subscribeHhSyncJob(
    jobId: string,
    onUpdate: (job: HhSyncJob) => void,
    onError: (err: Error) => void,
  ): () => void {
    let cancelled = false;
    let polling = false;

    const startFallbackPoll = () => {
      if (cancelled || polling) return;
      polling = true;
      es.close();
      void (async () => {
        while (!cancelled) {
          const job = await request<HhSyncJob>(`/hh/sync-jobs/${jobId}`);
          if (cancelled) return;
          onUpdate(job);
          if (HH_SYNC_TERMINAL.has(job.status)) return;
          await sleep(HH_SYNC_POLL_MS);
        }
      })().catch((err: unknown) => {
        if (!cancelled) onError(err instanceof Error ? err : new Error(String(err)));
      });
    };

    const es = openSse(
      `/hh/sync-jobs/${jobId}/events`,
      (source) => {
        source.addEventListener("status", (ev) => {
          try {
            onUpdate(JSON.parse((ev as MessageEvent).data) as HhSyncJob);
          } catch {
            /* ignore parse */
          }
        });
        source.addEventListener("done", () => source.close());
        // Обрыв или отказ (тикет одноразовый — переподключение не пройдёт): дочитать опросом.
        source.onerror = startFallbackPoll;
      },
      startFallbackPoll,
    );

    return () => {
      cancelled = true;
      es.close();
    };
  },

  getMarketMap: (resumeId?: number | null) =>
    request<MarketMap>(`/market-map${resumeId != null ? `?resume_id=${resumeId}` : ""}`),
  rebuildMarketMap: () => request<MarketMap>("/market-map/rebuild", { method: "POST" }),
  listResumes: () => request<{ items: Resume[] }>("/resumes"),

  createResume: (name: string, text: string) =>
    request<Resume>("/resumes", {
      method: "POST",
      body: JSON.stringify({ name, text }),
    }),

  updateResume: (id: number, name: string, text: string) =>
    request<Resume>(`/resumes/${id}`, {
      method: "PUT",
      body: JSON.stringify({ name, text }),
    }),

  deleteResume: async (id: number) => {
    const res = await authFetch(`/resumes/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`Удаление резюме: ${res.status} ${res.statusText}`);
  },

  /** Матчинг: кэш → сразу; иначе 202 job + poll. */
  matchVacancy: (resumeId: number, vacancyId: string, force = false, signal?: AbortSignal) =>
    postInteractiveLlm<VacancyMatch>(
      `/resumes/${resumeId}/match/${vacancyId}${force ? "?force=true" : ""}`,
      { method: "POST" },
      (job) => job.match,
      undefined,
      signal,
    ),

  getSkillGaps: (resumeId: number) => request<SkillGaps>(`/resumes/${resumeId}/skill-gaps`),

  /** `peers` — сколько ближайших вакансий учитывать (по умолчанию 100), `grade` — только этого грейда. */
  getResumeMarket: (resumeId: number, opts?: { peers?: number; grade?: string }) => {
    const q = new URLSearchParams();
    if (opts?.peers) q.set("peers", String(opts.peers));
    if (opts?.grade) q.set("grade", opts.grade);
    const qs = q.toString();
    return request<ResumeMarket>(`/resumes/${resumeId}/market${qs ? `?${qs}` : ""}`);
  },
  getSkillWhatIf: (resumeId: number) => request<SkillWhatIf>(`/resumes/${resumeId}/what-if`),
  getCareerPaths: (resumeId: number) => request<CareerPaths>(`/resumes/${resumeId}/career-paths`),

  getVacancyMatch: (resumeId: number, vacancyId: string) =>
    request<VacancyMatch>(`/resumes/${resumeId}/match/${vacancyId}`),

  /** Письмо: кэш → сразу; иначе 202 job + poll. */
  generateCoverLetter: (
    resumeId: number,
    vacancyId: string,
    tone: CoverLetterTone,
    force = false,
    signal?: AbortSignal,
  ) =>
    postInteractiveLlm<CoverLetter>(
      `/resumes/${resumeId}/cover-letter/${vacancyId}${force ? "?force=true" : ""}`,
      { method: "POST", body: JSON.stringify({ tone }) },
      (job) => job.cover_letter,
      undefined,
      signal,
    ),

  getCoverLetter: (resumeId: number, vacancyId: string) =>
    request<CoverLetter>(`/resumes/${resumeId}/cover-letter/${vacancyId}`),

  /** Адаптация резюме: кэш → сразу; иначе 202 job + SSE/poll. */
  tailorResume: (resumeId: number, vacancyId: string, force = false, signal?: AbortSignal) =>
    postInteractiveLlm<ResumeTailoring>(
      `/resumes/${resumeId}/tailor/${vacancyId}${force ? "?force=true" : ""}`,
      { method: "POST" },
      (job) => job.resume_tailoring,
      undefined,
      signal,
    ),

  getResumeTailoring: (resumeId: number, vacancyId: string) =>
    request<ResumeTailoring>(`/resumes/${resumeId}/tailor/${vacancyId}`),

  /** Подготовка к собеседованию: кэш → сразу; иначе 202 job + SSE/poll. */
  prepareInterview: (resumeId: number, vacancyId: string, force = false, signal?: AbortSignal) =>
    postInteractiveLlm<InterviewPrep>(
      `/resumes/${resumeId}/interview-prep/${vacancyId}${force ? "?force=true" : ""}`,
      { method: "POST" },
      (job) => job.interview_prep,
      undefined,
      signal,
    ),

  getInterviewPrep: (resumeId: number, vacancyId: string) =>
    request<InterviewPrep>(`/resumes/${resumeId}/interview-prep/${vacancyId}`),

  /** Текст резюме из PDF/DOCX: тело запроса — сам файл (без multipart). */
  parseResumeFile: (file: File) =>
    request<ResumeFileText>("/resumes/parse-file", {
      method: "POST",
      body: file,
      headers: { "Content-Type": "application/octet-stream" },
    }),

  subscribeRunEvents(
    runId: string,
    onProgress: (data: ProgressEvent) => void,
    onDone: () => void,
    onError: (err: Error) => void,
  ): () => void {
    let settled = false;
    let gotProgress = false;
    let cancelled = false;
    const cancelFlag = { cancelled: false };

    const finishDone = () => {
      if (settled || cancelled) return;
      settled = true;
      window.clearTimeout(timer);
      es.close();
      onDone();
    };

    const startFallbackPoll = () => {
      if (settled || cancelled) return;
      settled = true;
      window.clearTimeout(timer);
      es.close();
      void pollRunUntilTerminal(runId, onProgress, cancelFlag).then(
        () => {
          if (!cancelled) onDone();
        },
        (err: unknown) => {
          if (!cancelled) {
            onError(err instanceof Error ? err : new Error(String(err)));
          }
        },
      );
    };

    // Idle SSE → poll; без hard-fail по времени.
    const timer = window.setTimeout(() => {
      startFallbackPoll();
    }, SSE_FALLBACK_IDLE_MS);

    const es = openSse(
      `/runs/${runId}/events`,
      (source) => {
        source.addEventListener("progress", (ev) => {
          try {
            gotProgress = true;
            onProgress(JSON.parse((ev as MessageEvent).data) as ProgressEvent);
          } catch (e) {
            if (!settled && !cancelled) {
              settled = true;
              window.clearTimeout(timer);
              es.close();
              onError(e instanceof Error ? e : new Error(String(e)));
            }
          }
        });

        source.addEventListener("done", () => {
          finishDone();
        });

        source.onerror = () => {
          if (settled || cancelled) return;
          // Первый error часто при connect — ждём progress; иначе fallback poll
          if (gotProgress) {
            startFallbackPoll();
            return;
          }
          window.setTimeout(() => {
            if (settled || cancelled || gotProgress) return;
            startFallbackPoll();
          }, 1500);
        };
      },
      startFallbackPoll,
    );

    return () => {
      cancelled = true;
      cancelFlag.cancelled = true;
      window.clearTimeout(timer);
      es.close();
    };
  },
};

export type { MetricsSummary, ProgressEvent, Vacancy };
