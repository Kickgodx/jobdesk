export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  /** Дошёл до конца, но часть постов не разобрана LLM (причина — в error_message). */
  | "partial"
  | "cancelled"
  | "failed";

/** Полная вакансия (detail / import). */
export interface Vacancy {
  id: string;
  source_channel: string;
  source_url?: string;
  post_url?: string | null;
  post_date?: string | null;
  raw_text?: string;
  cleaned_text: string;
  detected_specializations: string[];
  detected_grade: string;
  detected_salary_min?: number | null;
  detected_salary_max?: number | null;
  detected_salary_avg?: number | null;
  detected_salary_avg_rub?: number | null;
  salary_currency?: string | null;
  work_format?: string | null;
  country?: string | null;
  city?: string | null;
  company?: string | null;
  contact?: string | null;
  technologies?: string[];
  application_urls?: string[];
  vacancy_score?: number;
  extraction_source?: string;
  ai_confidence?: number | null;
  ai_notes?: string | null;
  match_score?: number | null;
  match_explanation?: string | null;
  archived_at?: string | null;
}

/** Элемент списка: без полных текстов, только preview. */
export interface VacancyListItem {
  id: string;
  source_post_key?: string;
  source_channel: string;
  source_url?: string;
  post_url?: string | null;
  post_date?: string | null;
  preview: string;
  detected_specializations: string[];
  detected_grade: string;
  detected_salary_min?: number | null;
  detected_salary_max?: number | null;
  detected_salary_avg?: number | null;
  detected_salary_avg_rub?: number | null;
  salary_currency?: string | null;
  work_format?: string | null;
  country?: string | null;
  city?: string | null;
  company?: string | null;
  contact?: string | null;
  technologies?: string[];
  application_urls?: string[];
  vacancy_score?: number;
  extraction_source?: string;
  ai_confidence?: number | null;
  ai_notes?: string | null;
  match_score?: number | null;
  match_explanation?: string | null;
  /** Оценка получена другой моделью матчинга или версией промпта; null — оценки нет. */
  match_stale?: boolean | null;
  archived_at?: string | null;
}

export interface ConfigDefaults {
  channels: string[];
  search: {
    date_from: string | null;
    date_from_resolved: string | null;
    date_to: string;
    date_to_resolved: string;
    max_days: number;
  };
  filters: {
    specializations: string[];
    any_specialization: boolean;
    /** Технологии стека; со специализациями — через И. */
    technologies: string[];
    exclude_keywords: string[];
    countries: string[];
    cities: string[];
    work_formats: string[];
  };
  parser: {
    vacancy_score_threshold: number;
    vacancy_min_substantive_score: number;
  };
  ai: {
    enabled: boolean;
    model: string;
    fallback_models: string[];
    use_cache: boolean;
  };
  telegram: { telethon_session_file: string };
  specialization_options: string[];
  /** Словарь технологий config.yaml — варианты фильтра прогона. */
  technology_options: string[];
  work_format_options: string[];
  grade_options: string[];
}

export interface LlmModelOption {
  id: string;
  label: string;
  provider?: string | null;
}

export interface LlmModelsCatalog {
  default_model: string;
  default_fallbacks: string[];
  models: LlmModelOption[];
  model_ids: string[];
}

export interface MetricsSummary {
  total_posts_fetched: number;
  total_vacancies: number;
  unique_vacancies: number;
  duplicate_count: number;
  vacancies_by_specialization: Record<string, number>;
  vacancies_by_grade: Record<string, number>;
  vacancies_by_work_format: Record<string, number>;
  daily: { period: string; count: number }[];
  vacancies_with_salary: number;
  vacancies_without_salary: number;
  technology_top: Record<string, number>;
  salary_by_specialization: Record<string, { mean?: number; median?: number; count_with_salary: number }>;
  salary_by_grade: Record<string, { mean?: number; median?: number; count_with_salary: number }>;
  grade_distribution: Record<string, number>;
  salary_display?: { display_currency: string; rates_as_of?: string | null; rates_source: string } | null;
  warnings: string[];
}

export interface SalaryGroupStats {
  group: string;
  count: number;
  mean?: number | null;
  min?: number | null;
  p10?: number | null;
  q1?: number | null;
  median?: number | null;
  q3?: number | null;
  p90?: number | null;
  max?: number | null;
}

export interface SalaryMonthlyBucket {
  period: string;
  count: number;
  mean?: number | null;
  median?: number | null;
}

export interface SalaryPeriodGroupBucket {
  period: string;
  group: string;
  count: number;
  mean?: number | null;
  median?: number | null;
  min?: number | null;
  p10?: number | null;
  q1?: number | null;
  q3?: number | null;
  p90?: number | null;
  max?: number | null;
}

export interface SalaryDistributionBucket {
  range: string;
  count: number;
}

export interface SalaryAnalytics {
  by_grade: SalaryGroupStats[];
  by_work_format: SalaryGroupStats[];
  by_specialization: SalaryGroupStats[];
  monthly: SalaryMonthlyBucket[];
  yearly: SalaryMonthlyBucket[];
  by_grade_yearly: SalaryPeriodGroupBucket[];
  by_work_format_yearly: SalaryPeriodGroupBucket[];
  by_grade_monthly: SalaryPeriodGroupBucket[];
  by_work_format_monthly: SalaryPeriodGroupBucket[];
  distribution: SalaryDistributionBucket[];
  salary_display?: { display_currency: string; rates_as_of?: string | null; rates_source: string } | null;
}

export interface SourceStats {
  channel: string;
  vacancies: number;
  duplicates: number;
  unique_share: number;
  avg_score?: number | null;
  with_salary_share: number;
}

export interface CompanyStats {
  company: string;
  vacancies: number;
  avg_score?: number | null;
  with_salary: number;
  avg_salary_rub?: number | null;
}

export interface SavedFilterParams {
  q?: string | null;
  source_channel?: string | null;
  grade?: string | null;
  specializations?: string[];
  technologies?: string[];
  work_format?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  has_salary?: boolean | null;
  /** Вместе с has_salary: вакансии с оценкой зарплаты по похожим считаются «с зарплатой». */
  salary_estimates?: boolean;
  include_archived?: boolean;
  search_mode?: "text" | "semantic";
  sort_by?: string;
  sort_order?: string;
  then_by?: string | null;
  then_order?: string;
}

export interface SavedFilter {
  id: number;
  name: string;
  params: SavedFilterParams;
  created_at: string;
}

export interface Resume {
  id: number;
  name: string;
  text: string;
  created_at: string;
}

export interface AuthUser {
  id: number;
  email: string;
  is_admin: boolean;
  created_at: string;
}

export type ApiTokenScope = "read" | "llm";

/** Персональный токен доступа (для MCP-сервера и скриптов); значение — только в ответе создания. */
export interface ApiToken {
  id: number;
  name: string;
  prefix: string;
  scopes: ApiTokenScope[];
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
}

export interface ApiTokenCreated extends ApiToken {
  token: string;
}

export interface HhStatus {
  enabled: boolean;
  token_configured: boolean;
  email_configured: boolean;
  listing_ttl_days: number;
  search_text: string;
  search_text_no_python: string;
  professional_role: string[];
  listings_total: number;
  listings_with_detail: number;
  listings_without_detail: number;
}

export type HhSyncWindow = "all" | "7d" | "14d" | "30d" | "range";

/** main / no_python — запросы из hh.search конфига; custom — свой текст в text. */
export type HhSearchQuery = "main" | "no_python" | "custom";

export interface HhSyncRequest {
  window?: HhSyncWindow;
  date_from?: string;
  date_to?: string;
  query?: HhSearchQuery;
  text?: string;
}

export type HhSyncJobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface HhSyncAccepted {
  job_id: string;
  status: HhSyncJobStatus;
}

export interface HhSyncProgress {
  /** search → details (done/total групп) → purge */
  phase: "search" | "details" | "purge";
  done: number;
  total: number;
  searched: number;
  pages: number;
  unique: number;
  duplicates: number;
  details_fetched: number;
  details_skipped: number;
  vacancies_inserted: number;
  vacancies_updated: number;
  errors: number;
}

/** Фоновый HH sync (POST /hh/sync → 202). result у failed — частичный, на момент сбоя. */
export interface HhSyncJob {
  id: string;
  status: HhSyncJobStatus;
  window: string;
  period: number | null;
  date_from: string | null;
  date_to: string | null;
  query: HhSearchQuery;
  /** Текст запроса к HH; null — у sync, запущенных до появления выбора запроса. */
  search_text: string | null;
  progress: HhSyncProgress | null;
  result: HhSyncResult | null;
  error_message: string | null;
  attempts: number;
  /** Запрошена отмена; у running — воркер остановит sync перед следующей страницей / группой. */
  cancel_requested: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface HhSyncResult {
  searched: number;
  listings_upserted: number;
  unique: number;
  duplicates: number;
  details_fetched: number;
  details_skipped: number;
  vacancies_inserted: number;
  vacancies_updated: number;
  listings_purged: number;
  pages: number;
  flush_batches: number;
  errors: string[];
  /** Нет у итогов, сохранённых до появления поля. */
  warnings?: string[];
  duration_seconds: number;
  window: string;
  period: number | null;
  date_from: string | null;
  date_to: string | null;
  query: HhSearchQuery;
  search_text: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

export interface SseTicketResponse {
  ticket: string;
  expires_in: number;
}

export type MatchImportance = "critical" | "important" | "secondary";

export interface SkillMatch {
  skill: string;
  importance: MatchImportance;
  evidence: string;
}

/** Недостающий навык в сводке по оценкам резюме (GET /resumes/{id}/skill-gaps). */
export interface SkillGapItem {
  name: string;
  vacancies: number;
  critical: number;
  important: number;
  secondary: number;
  share: number;
  vacancy_ids: string[];
  /** Только у технологий: активных уникальных вакансий каталога с этой технологией. */
  market_vacancies: number | null;
}

export interface SkillGaps {
  resume_id: number;
  evaluated: number;
  technologies: SkillGapItem[];
  other: SkillGapItem[];
}

export interface MarketShare {
  name: string;
  /** Доля ближайших вакансий (0–1). */
  share: number;
}

export interface ResumeMarketTechnology extends MarketShare {
  in_resume: boolean;
}

/** Резюме против рынка (GET /resumes/{id}/market): ближайшие к резюме вакансии по смыслу. */
export interface ResumeMarket {
  resume_id: number;
  window_days: number;
  /** Ближайшие только этого грейда; null — все грейды. */
  grade: string | null;
  peers: number;
  grades: MarketShare[];
  specializations: MarketShare[];
  salary_count: number;
  p10: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  p90: number | null;
  technologies: ResumeMarketTechnology[];
  /** Средняя доля технологий вакансии, названных в резюме (0–1). */
  coverage: number | null;
  resume_technologies: string[];
}

export interface MatchBreakdown {
  role_match: number;
  skills_match: number;
  seniority_match: number;
  domain_match: number;
  work_format_match: number;
}

export interface VacancyMatch {
  resume_id: number;
  vacancy_id: string;
  overall_score: number;
  confidence?: number;
  breakdown?: MatchBreakdown;
  explanation?: string | null;
  matching_skills: SkillMatch[];
  missing_skills: SkillMatch[];
  strengths?: string[];
  risks?: string[];
  model: string;
  created_at: string;
  cached: boolean;
  /** Получена другой моделью матчинга или версией промпта — POST без force пересчитает. */
  stale: boolean;
}

export type CoverLetterTone = "neutral" | "friendly" | "formal";

export interface CoverLetter {
  resume_id: number;
  vacancy_id: string;
  text: string;
  tone: string;
  model: string;
  created_at: string;
  cached: boolean;
  /** Получено другой моделью писем или версией промпта (тон не учитывается). */
  stale: boolean;
}

export type LlmJobKind =
  | "match"
  | "cover_letter"
  | "resume_tailor"
  | "interview_prep"
  | "import"
  | "reextract"
  | "match_batch"
  | "ask_market";

export interface ResumeTailoringSuggestion {
  section: string;
  /** Цитата из резюме; пусто — новая строка из фактов резюме. */
  current: string;
  suggested: string;
  reason: string;
  priority: "high" | "medium" | "low";
}

/** Адаптация резюме под вакансию (POST/GET /resumes/{id}/tailor/{vacancy_id}). */
export interface ResumeTailoring {
  resume_id: number;
  vacancy_id: string;
  summary: string;
  suggestions: ResumeTailoringSuggestion[];
  keywords: string[];
  /** Требования, которых в резюме нет: не добавлять без реального опыта. */
  do_not_claim: string[];
  model: string;
  created_at: string;
  cached: boolean;
  stale: boolean;
}

export type InterviewQuestionKind = "technical" | "experience" | "behavioral" | "gap";

export interface InterviewQuestion {
  kind: InterviewQuestionKind;
  topic: string;
  question: string;
  /** Откуда вопрос: требование вакансии или строка резюме. */
  why: string;
  /** Тезисы ответа — только из фактов резюме. */
  answer_points: string[];
}

export interface InterviewGap {
  skill: string;
  advice: string;
}

/** Подготовка к собеседованию (POST/GET /resumes/{id}/interview-prep/{vacancy_id}). */
export interface InterviewPrep {
  resume_id: number;
  vacancy_id: string;
  summary: string;
  questions: InterviewQuestion[];
  gaps: InterviewGap[];
  questions_to_ask: string[];
  model: string;
  created_at: string;
  cached: boolean;
  stale: boolean;
}

/** Текст резюме из PDF/DOCX (POST /resumes/parse-file). */
export interface ResumeFileText {
  text: string;
  format: "pdf" | "docx";
  chars: number;
  truncated: boolean;
  pages: number | null;
}
export type LlmJobStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface LlmJobAccepted {
  job_id: string;
  status: string;
}

/** Фильтры каталога (поля VacancyQuery без пустых), по которым считали ответ «Спросить базу». */
export interface AskMarketFilters {
  specializations?: string[];
  technologies?: string[];
  grade?: string;
  work_format?: string;
  date_from?: string;
  date_to?: string;
  has_salary?: boolean;
  q?: string;
}

export interface AskMarketExample {
  id: string;
  date: string | null;
  company: string | null;
  roles: string[];
  grade: string;
  salary: number | null;
  work_format: string | null;
}

export interface AskMarketResult {
  question: string;
  answerable: boolean;
  answer: string;
  filters: AskMarketFilters;
  topics: string[];
  variants: { label: string; filters: AskMarketFilters }[];
  unrecognized: string[];
  facts: ({ count?: { vacancies: number; active: number; with_salary: number }; examples?: AskMarketExample[] } & Record<string, unknown>) | null;
  model: string | null;
  prompt_version: string | null;
}

export interface AskMarketTurn {
  question: string;
  answer: string;
  filters: AskMarketFilters;
}

export interface LlmJob {
  id: string;
  kind: LlmJobKind;
  status: LlmJobStatus;
  /** null у импорта (kind=import). */
  resume_id: number | null;
  vacancy_id: string | null;
  error_message?: string | null;
  match?: VacancyMatch | null;
  cover_letter?: CoverLetter | null;
  resume_tailoring?: ResumeTailoring | null;
  interview_prep?: InterviewPrep | null;
  /** Импорт: итог по уже обработанным текстам — растёт, пока джоба running. */
  import_result?: VacancyImportResponse | null;
  /** Переразбор (kind=reextract): итог по уже обработанным вакансиям. */
  reextract_result?: VacancyReextractResponse | null;
  /** Пакетная оценка (kind=match_batch): итог по уже оценённым вакансиям. */
  match_batch_result?: MatchBatchResponse | null;
  /** «Спросить базу» (kind=ask_market): ответ, когда задача done. */
  ask_market_result?: AskMarketResult | null;
  /** Запрошена отмена; у running — воркер остановит задачу в ближайшей точке проверки. */
  cancel_requested: boolean;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export type VacancyReextractStatus = "updated" | "kept" | "error";

export interface VacancyReextractItem {
  index: number;
  vacancy_id: string;
  status: VacancyReextractStatus;
  message?: string | null;
}

export interface VacancyReextractResponse {
  total: number;
  items: VacancyReextractItem[];
  updated: number;
  kept: number;
  errors: number;
  warning?: string | null;
}

export interface VacancyReextractInfo {
  missing_role: number;
  max_per_job: number;
  ai_enabled: boolean;
  active_job_id?: string | null;
}

export interface TechnologyCount {
  name: string;
  count: number;
}

export type VacancyImportStatus = "created" | "exists" | "not_vacancy" | "error";

export interface VacancyImportItem {
  index: number;
  status: VacancyImportStatus;
  message?: string | null;
  vacancy?: Vacancy | null;
}

export interface VacancyImportResponse {
  items: VacancyImportItem[];
  created: number;
  existing: number;
  rejected: number;
  ai_enabled: boolean;
  warning?: string | null;
  /** Текстов в запросе; у выполняемой джобы items короче — это прогресс. */
  total: number;
}

/** Итог `api.importVacancies` (только UI): отменённый импорт — итог по обработанным текстам. */
export interface VacancyImportOutcome {
  result: VacancyImportResponse;
  cancelled: boolean;
}

export interface RunDetail {
  id: string;
  status: RunStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  posts_fetched: number;
  parse_warnings?: string | null;
  unique_vacancies?: number | null;
  metrics?: MetricsSummary | null;
  run_params?: Record<string, unknown> | null;
  error_message?: string | null;
  progress?: ProgressEvent | null;
  progress_updated_at?: string | null;
  cancel_requested?: boolean;
}

export interface ProgressEvent {
  posts_processed: number;
  /** Пока phase === "fetching", это прочитанное на текущий момент — растёт. */
  posts_total: number;
  vacancies_found: number;
  phase: string;
  channels_total?: number;
  channels_fetched?: number;
  ai_llm_disabled?: boolean;
  ai_llm_disabled_reason?: string | null;
  ai_tokens_in?: number;
  ai_tokens_out?: number;
  ai_skipped_score?: number;
  vacancies_preview?: Vacancy[];
}

/** Whitelist overrides прогона — backend отклоняет любое другое поле (422). */
export interface RunCreateBody {
  channels?: string[];
  search?: { date_from?: string; date_to?: string };
  filters?: {
    specializations?: string[];
    any_specialization?: boolean;
    technologies?: string[];
    exclude_keywords?: string[];
    countries?: string[];
    cities?: string[];
    work_formats?: string[];
  };
  parser?: {
    vacancy_score_threshold?: number;
    vacancy_min_substantive_score?: number;
    /** false — перечитать окно целиком, не глядя на чекпоинты каналов. */
    incremental_fetch?: boolean;
  };
  ai?: { enabled?: boolean; model?: string; fallback_models?: string[]; use_cache?: boolean };
}

export interface ParseFormState {
  channelsText: string;
  dateFrom: string;
  dateTo: string;
  anySpecialization: boolean;
  specializations: string[];
  technologies: string[];
  excludeKeywordsText: string;
  countriesText: string;
  citiesText: string;
  workFormats: string[];
  vacancyScoreThreshold: number;
  vacancyMinSubstantive: number;
  useAi: boolean;
  aiModel: string;
  aiFallbacks: string[];
  ignoreLlmCache: boolean;
  fullRescan: boolean;
}

export interface TechnologyGradeStats {
  grade: string;
  with_count: number;
  with_median: number | null;
  without_count: number;
  without_median: number | null;
  /** Грейд вошёл в расчёт премии. */
  compared: boolean;
}

export interface TechnologyPremium {
  technology: string;
  vacancies: number;
  median: number | null;
  /** Средневзвешенное по грейдам (медиана с / медиана без − 1); null — мало данных. */
  premium: number | null;
  compared_vacancies: number;
  by_grade: TechnologyGradeStats[];
}

export interface TechnologyTrend {
  months: string[];
  totals: number[];
  series: { technology: string; counts: number[] }[];
}

export interface TechnologyMomentum {
  technology: string;
  recent: number;
  previous: number;
  recent_share: number;
  previous_share: number;
  delta_pp: number;
}

/** GET /analytics/technology-graph: какие технологии просят вместе. */
export interface TechnologyGraphNode {
  name: string;
  count: number;
  share: number;
  role: string | null;
  role_share: number;
}

export interface TechnologyGraphEdge {
  source: string;
  target: string;
  count: number;
  /** Во сколько раз пара встречается чаще, чем случайно. */
  lift: number;
}

export interface TechnologyGraph {
  total: number;
  nodes: TechnologyGraphNode[];
  edges: TechnologyGraphEdge[];
  min_pair_count: number;
}

export interface TechnologyAnalytics {
  premium: TechnologyPremium[];
  trend: TechnologyTrend;
  momentum: TechnologyMomentum[];
  momentum_window_days: number;
  momentum_anchor: string | null;
  salary_display?: { display_currency: string; rates_as_of?: string | null; rates_source: string } | null;
}

export interface LifetimeBucket {
  label: string;
  min_days: number;
  max_days: number | null;
  count: number;
}

export interface LifetimeGroup {
  name: string;
  vacancies: number;
  republished: number;
  republished_share: number;
  long_open: number;
  median_span_days: number | null;
}

export interface LifetimeCompany {
  company: string;
  vacancies: number;
  republished: number;
  long_open: number;
  max_span_days: number;
}

export interface LifetimeVacancy {
  id: string;
  company: string | null;
  specializations: string[];
  grade: string;
  source_channel: string;
  first_at: string;
  last_at: string;
  span_days: number;
  publication_days: number;
  posts: number;
  archived: boolean;
}

/** GET /analytics/lifetime — жизнь вакансии по публикациям в Telegram. */
export interface LifetimeAnalytics {
  vacancies: number;
  republished: number;
  republished_share: number;
  long_open: number;
  long_open_days: number;
  span_p50_days: number | null;
  span_p90_days: number | null;
  buckets: LifetimeBucket[];
  by_role: LifetimeGroup[];
  by_grade: LifetimeGroup[];
  companies: LifetimeCompany[];
  longest: LifetimeVacancy[];
  min_group: number;
}

export type MarketSalaryLevel = "grade_role_tech" | "grade_role" | "grade_tech" | "role";

/** Рыночная вилка вакансии (GET /vacancies/{id}/market-salary). */
export interface MarketSalary {
  vacancy_id: string;
  level: MarketSalaryLevel | null;
  grade: string | null;
  specializations: string[];
  technologies: string[];
  count: number;
  p10: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  p90: number | null;
  vacancy_salary: number | null;
  /** Доля похожих с зарплатой не выше, чем у вакансии (0–1). */
  percentile: number | null;
  window_days: number;
}

/** Оценка зарплаты вакансии без вилки (GET /vacancies/{id}/salary-estimate). */
export interface SalaryEstimate {
  vacancy_id: string;
  status: "ready" | "insufficient" | "pending" | "has_salary" | "unavailable";
  /** Грейд, среди которого искали похожих; null — среди всех. */
  grade: string | null;
  neighbours: number;
  min_neighbours: number;
  p25: number | null;
  median: number | null;
  p75: number | null;
  window_days: number;
  examples: VacancyListItem[];
}

export interface NamedCount {
  name: string;
  count: number;
}

export interface CompanyProfileVacancy {
  id: string;
  post_date: string | null;
  specializations: string[];
  grade: string;
  salary_rub: number | null;
  archived: boolean;
  source_channel: string;
}

/** Профиль компании (GET /companies/profile?name=). */
export interface CompanyProfile {
  key: string;
  name: string;
  variants: string[];
  vacancies: number;
  active: number;
  first_post: string | null;
  last_post: string | null;
  with_salary: number;
  salary_p25: number | null;
  salary_median: number | null;
  salary_p75: number | null;
  roles: NamedCount[];
  technologies: NamedCount[];
  grades: NamedCount[];
  channels: NamedCount[];
  telegram_posts: number;
  telegram_post_days: number;
  recent: CompanyProfileVacancy[];
}

export type VacancyFlagCode = "reposted" | "long_open" | "frequent_role" | "no_salary";

export interface VacancyFlag {
  code: VacancyFlagCode;
  severity: "warning" | "info";
  message: string;
  value: number | null;
}

export interface SkillWhatIfOption {
  technology: string;
  demand: number;
  demand_share: number;
  new_fit: number;
  coverage_gain: number;
  premium: number | null;
  premium_scope: "grade" | "role" | null;
  median_with: number | null;
}

export interface SkillWhatIfPeer {
  total: number;
  covered: number;
  missing: string[];
}

/** GET /resumes/{id}/what-if — эффект недостающих технологий для сегмента рынка резюме. */
export interface SkillWhatIf {
  resume_id: number;
  window_days: number;
  peers: number;
  with_technologies: number;
  role: string | null;
  grade: string | null;
  fit_threshold: number;
  baseline_fit: number;
  baseline_coverage: number | null;
  resume_technologies: string[];
  options: SkillWhatIfOption[];
  peer_gaps: SkillWhatIfPeer[];
}

/** Вакансии роли (в грейде или во всех грейдах, `grade: null`): покрытие резюме и зарплата. */
export interface CareerSegment {
  role: string;
  grade: string | null;
  vacancies: number;
  fit_share: number;
  coverage: number;
  salary_median: number | null;
  salary_count: number;
}

export interface CareerRouteStep {
  technology: string;
  demand_share: number;
  /** Доля подходящих вакансий после шага (вместе с предыдущими). */
  fit_share: number;
  coverage: number;
}

export interface CareerPath extends CareerSegment {
  kind: "grade" | "role";
  /** Доля роли среди ближайших к резюме вакансий; null — у шага по грейду. */
  closeness: number | null;
  route: CareerRouteStep[];
  salary_uplift: number | null;
}

/** GET /resumes/{id}/career-paths — следующий грейд и соседние роли с маршрутом технологий. */
export interface CareerPaths {
  resume_id: number;
  window_days: number;
  peers: number;
  fit_threshold: number;
  resume_technologies: string[];
  current: CareerSegment | null;
  paths: CareerPath[];
}

export interface MarketMapCluster {
  id: number;
  label: string;
  size: number;
  x: number;
  y: number;
  roles: MarketShare[];
  technologies: MarketShare[];
  salary_median: number | null;
  salary_count: number;
  recent: number;
  previous: number;
  /** Изменение доли ниши за последние 30 дней против предыдущих 30, п.п. */
  delta_pp: number | null;
}

/** GET /market-map — точки колонками (индекс i — одна вакансия) и ниши. */
export interface MarketMap {
  available: boolean;
  created_at: string | null;
  model: string | null;
  points: number;
  window_days: number | null;
  clusters: MarketMapCluster[];
  ids: string[];
  x: number[];
  y: number[];
  cluster: number[];
  role: (string | null)[];
  grade: (string | null)[];
  salary: (number | null)[];
  company: (string | null)[];
  archived: boolean[];
  resume: { resume_id: number; x: number; y: number; cluster: number; nearest: string[] } | null;
  resume_error: string | null;
  /** Админ запросил пересчёт, фоновое обслуживание его ещё не выполнило. */
  rebuild_pending: boolean;
}

export interface RecruiterContact {
  kind: "telegram" | "telegram_bot" | "email";
  /** Username без «@» или email. */
  value: string;
  /** https://t.me/<username> или mailto:<email>; черновик добавляет UI. */
  url: string;
  has_contact_hint: boolean;
}

/** GET /vacancies/{id}/contacts — куда откликнуться. */
export interface RecruiterContacts {
  vacancy_id: string;
  contacts: RecruiterContact[];
  application_urls: string[];
}

export interface VacancyFlags {
  vacancy_id: string;
  company_key: string | null;
  flags: VacancyFlag[];
}

export interface MatchBatchItem {
  index: number;
  vacancy_id: string;
  status: "scored" | "skipped" | "error";
  score: number | null;
  message: string | null;
}

export interface MatchBatchResponse {
  total: number;
  items: MatchBatchItem[];
  scored: number;
  /** Свежая оценка уже была — LLM не вызывался. */
  skipped: number;
  errors: number;
  warning?: string | null;
}

export interface SimilarVacancy {
  vacancy: VacancyListItem;
  /** 1 − косинусное расстояние эмбеддингов. */
  similarity: number;
}

export interface SimilarVacanciesResponse {
  items: SimilarVacancy[];
  /** У вакансии ещё нет эмбеддинга — индексация впереди. */
  pending: boolean;
}

export interface SemanticStatus {
  enabled: boolean;
  model: string;
  vacancies: number;
  embedded: number;
}

/** Обзор рынка: число вакансий недели с признаком и за прошлую неделю. */
export interface DigestCount {
  name: string;
  count: number;
  previous: number;
}

export interface DigestSalary {
  /** Грейд или «all». */
  grade: string;
  median: number | null;
  count: number;
  previous_median: number | null;
  previous_count: number;
}

export interface DigestTechnology {
  name: string;
  share: number;
  previous_share: number;
  delta_pp: number;
}

export interface DigestFacts {
  period_start: string;
  period_end: string;
  vacancies: number;
  previous_vacancies: number;
  avg_4w_vacancies: number;
  with_salary_share: number | null;
  roles: DigestCount[];
  work_formats: DigestCount[];
  salaries: DigestSalary[];
  salary_window_days: number;
  technologies_up: DigestTechnology[];
  technologies_down: DigestTechnology[];
  top_companies: DigestCount[];
  new_companies: DigestCount[];
  sources: DigestCount[];
}

export interface DigestListItem {
  period_start: string;
  period_end: string;
  vacancies: number;
  has_summary: boolean;
  created_at: string;
}

/** Еженедельный обзор рынка (GET /digests/{period_start}). */
export interface Digest {
  period_start: string;
  period_end: string;
  facts: DigestFacts;
  summary: string | null;
  summary_error: string | null;
  model: string | null;
  created_at: string;
  stale: boolean;
}

/** Канал, из которого пересылают посты в читаемые каналы (GET /channels/candidates, admin). */
export interface ChannelCandidate {
  channel_id: number;
  username: string;
  title: string | null;
  status: "new" | "ignored";
  forwards: number;
  vacancy_forwards: number;
  seen_in: string[];
  first_post_date: string | null;
  last_post_date: string | null;
  example_url: string | null;
}

/** Отличие перепоста от первой публикации (GET /vacancies/{id}/history). */
export interface VacancyChange {
  field: "salary" | "work_format" | "city" | "technologies";
  old: unknown;
  new: unknown;
  added: string[];
  removed: string[];
}

export interface VacancySighting {
  source_channel: string;
  post_date: string | null;
  post_url: string | null;
  kind: "original" | "repost" | "edited";
  duplicate_reason: string | null;
  changes: VacancyChange[];
}

export interface VacancyHistory {
  vacancy_id: string;
  /** Вакансия скрыта как более поздняя копия этой. */
  duplicate_of: string | null;
  sightings: VacancySighting[];
  edits: number;
}
