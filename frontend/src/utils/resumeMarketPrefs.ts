/** Выбор «Вакансий» и «Грейд» в «Резюме и рынок»: запоминается в браузере и общий с карьерным отчётом. */

export const PEER_OPTIONS = [50, 100, 200, 300, 500];
export const DEFAULT_PEERS = 100;

export interface ResumeMarketPrefs {
  peers: number;
  /** "" — все грейды. */
  grade: string;
}

const KEY = "jobdesk.resumeMarket";
const DEFAULTS: ResumeMarketPrefs = { peers: DEFAULT_PEERS, grade: "" };

export function loadResumeMarketPrefs(): ResumeMarketPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "null") as Partial<ResumeMarketPrefs> | null;
    if (!raw || typeof raw !== "object") return DEFAULTS;
    return {
      peers: PEER_OPTIONS.includes(Number(raw.peers)) ? Number(raw.peers) : DEFAULT_PEERS,
      grade: typeof raw.grade === "string" ? raw.grade : "",
    };
  } catch {
    // Хранилище недоступно (приватное окно, запрет сайта) — работаем без памяти.
    return DEFAULTS;
  }
}

export function saveResumeMarketPrefs(prefs: ResumeMarketPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    // Не сохранилось — выбор живёт до перезагрузки страницы.
  }
}
