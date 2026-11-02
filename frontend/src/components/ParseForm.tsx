import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { ParseFormState } from "../types/api";
import { buildRunBody, formFromDefaults, splitCsv } from "../utils/parseForm";
import ChannelCandidatesPanel from "./ChannelCandidatesPanel";

interface Props {
  onSubmit: (body: ReturnType<typeof buildRunBody>) => void;
  disabled?: boolean;
  isSubmitting?: boolean;
}

export default function ParseForm({ onSubmit, disabled, isSubmitting }: Props) {
  const defaultsQ = useQuery({ queryKey: ["config-defaults"], queryFn: () => api.getConfigDefaults() });
  const llmQ = useQuery({ queryKey: ["llm-models"], queryFn: () => api.getLlmModels() });

  const [form, setForm] = useState<ParseFormState | null>(null);
  // Первый снимок настроек фиксируем один раз: дальнейшие refetch не затирают правки.
  if (form === null && defaultsQ.data) {
    setForm(formFromDefaults(defaultsQ.data));
  }

  if (defaultsQ.isError) {
    const msg =
      defaultsQ.error instanceof Error ? defaultsQ.error.message : "Не удалось загрузить config.yaml";
    return (
      <p className="error">
        Ошибка загрузки настроек: {msg}. Проверьте config.yaml и llm_models.yaml.
      </p>
    );
  }

  if (!form || !defaultsQ.data) {
    return <p>Загрузка настроек из config.yaml…</p>;
  }

  const d = defaultsQ.data;
  const modelOptions = llmQ.data?.models ?? [];
  const fallbackOpts = modelOptions.filter((m) => m.id !== form.aiModel).map((m) => m.id);

  const patch = (p: Partial<ParseFormState>) => setForm({ ...form, ...p });

  const toggleSpec = (spec: string) => {
    if (form.specializations.includes(spec)) {
      patch({ specializations: form.specializations.filter((s) => s !== spec) });
    } else {
      patch({ specializations: [...form.specializations, spec] });
    }
  };

  const toggleTech = (tech: string) => {
    if (form.technologies.includes(tech)) {
      patch({ technologies: form.technologies.filter((t) => t !== tech) });
    } else {
      patch({ technologies: [...form.technologies, tech] });
    }
  };
  // Технологии из снимка/конфига, которых нет в словаре, — тоже видны и снимаются.
  const techOptions = [
    ...d.technology_options,
    ...form.technologies.filter((t) => !d.technology_options.includes(t)),
  ];

  const toggleWf = (wf: string) => {
    if (form.workFormats.includes(wf)) {
      patch({ workFormats: form.workFormats.filter((w) => w !== wf) });
    } else {
      patch({ workFormats: [...form.workFormats, wf] });
    }
  };

  return (
    <div className="parse-form">
      <section>
        <h3>Источники</h3>
        <label className="field">
          Каналы (по строке или через запятую)
          <textarea
            rows={5}
            value={form.channelsText}
            onChange={(e) => patch({ channelsText: e.target.value })}
            placeholder="qa_jobs&#10;python_django_jobs"
          />
        </label>
        <ChannelCandidatesPanel
          inForm={splitCsv(form.channelsText).map((c) => c.replace(/^@/, "").toLowerCase())}
          onAdd={(u) =>
            patch({ channelsText: [...splitCsv(form.channelsText), u].join("\n") })
          }
        />
        <p className="caption">
          Сессия Telethon: <code>{d.telegram.telethon_session_file}</code> — задаётся в config.yaml на
          backend. Нужны TELEGRAM_API_ID / TELEGRAM_API_HASH в .env.
        </p>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={form.fullRescan}
            onChange={(e) => patch({ fullRescan: e.target.checked })}
          />
          Перечитать окно дат целиком
        </label>
        <p className="caption">
          По умолчанию из канала читаются только сообщения новее прошлого успешного прогона. Полный
          перечит нужен после смены фильтров или промпта, чтобы старые посты прошли разбор заново.
        </p>
      </section>

      <section>
        <h3>Фильтры</h3>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={form.anySpecialization}
            onChange={(e) => patch({ anySpecialization: e.target.checked })}
          />
          Любая IT-специализация
        </label>
        {!form.anySpecialization && (
          <div className="chip-grid">
            {d.specialization_options.map((spec) => (
              <label key={spec} className="chip">
                <input
                  type="checkbox"
                  checked={form.specializations.includes(spec)}
                  onChange={() => toggleSpec(spec)}
                />
                {spec}
              </label>
            ))}
          </div>
        )}
        <p className="field-label">Технологии (пусто — любые; со специализациями — через «и»)</p>
        <div className="chip-grid">
          {techOptions.map((tech) => (
            <label key={tech} className="chip">
              <input
                type="checkbox"
                checked={form.technologies.includes(tech)}
                onChange={() => toggleTech(tech)}
              />
              {tech}
            </label>
          ))}
        </div>
        <label className="field">
          Исключающие ключевые слова (по строке)
          <textarea
            rows={4}
            value={form.excludeKeywordsText}
            onChange={(e) => patch({ excludeKeywordsText: e.target.value })}
          />
        </label>
        <div className="row">
          <label className="field">
            Дата от
            <input type="date" value={form.dateFrom} onChange={(e) => patch({ dateFrom: e.target.value })} />
          </label>
          <label className="field">
            Дата до
            <input type="date" value={form.dateTo} onChange={(e) => patch({ dateTo: e.target.value })} />
          </label>
        </div>
        <label className="field">
          Страны (через запятую)
          <input value={form.countriesText} onChange={(e) => patch({ countriesText: e.target.value })} />
        </label>
        <label className="field">
          Города (через запятую)
          <input value={form.citiesText} onChange={(e) => patch({ citiesText: e.target.value })} />
        </label>
        <p className="field-label">Формат работы</p>
        <div className="chip-grid">
          {d.work_format_options.map((wf) => (
            <label key={wf} className="chip">
              <input type="checkbox" checked={form.workFormats.includes(wf)} onChange={() => toggleWf(wf)} />
              {wf}
            </label>
          ))}
        </div>
        <label className="field">
          Мин. vacancy_score: {form.vacancyScoreThreshold}
          <input
            type="range"
            min={0}
            max={100}
            value={form.vacancyScoreThreshold}
            onChange={(e) => patch({ vacancyScoreThreshold: Number(e.target.value) })}
          />
        </label>
        <label className="field">
          Мин. содержательных баллов: {form.vacancyMinSubstantive}
          <input
            type="range"
            min={0}
            max={100}
            value={form.vacancyMinSubstantive}
            onChange={(e) => patch({ vacancyMinSubstantive: Number(e.target.value) })}
          />
        </label>
      </section>

      <section>
        <h3>AI (OpenRouter)</h3>
        <label className="checkbox-row">
          <input type="checkbox" checked={form.useAi} onChange={(e) => patch({ useAi: e.target.checked })} />
          Использовать LLM
        </label>
        {form.useAi && (
          <>
            <label className="field">
              Основная модель
              <select value={form.aiModel} onChange={(e) => patch({ aiModel: e.target.value })}>
                {modelOptions.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
            <p className="field-label">Fallback-модели</p>
            <div className="chip-grid">
              {fallbackOpts.map((id) => {
                const label = modelOptions.find((m) => m.id === id)?.label ?? id;
                return (
                  <label key={id} className="chip">
                    <input
                      type="checkbox"
                      checked={form.aiFallbacks.includes(id)}
                      onChange={() => {
                        if (form.aiFallbacks.includes(id)) {
                          patch({ aiFallbacks: form.aiFallbacks.filter((x) => x !== id) });
                        } else {
                          patch({ aiFallbacks: [...form.aiFallbacks, id] });
                        }
                      }}
                    />
                    {label}
                  </label>
                );
              })}
            </div>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={form.ignoreLlmCache}
                onChange={(e) => patch({ ignoreLlmCache: e.target.checked })}
              />
              Не использовать кэш LLM в этом прогоне
            </label>
          </>
        )}
      </section>

      <button
        type="button"
        className="primary"
        disabled={disabled || isSubmitting}
        onClick={() => onSubmit(buildRunBody(form, d))}
      >
        {isSubmitting ? "Запуск…" : "Запустить парсинг"}
      </button>
    </div>
  );
}
