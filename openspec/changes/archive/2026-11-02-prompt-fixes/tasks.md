# Tasks

- [x] 1.1 `ask-market-v2` и флаг `partial` в `trend`. Проверка — `tests/test_ask_market.py::test_trend_marks_current_month_partial`, `test_answer_prompt_explains_scope_fields`
- [x] 1.2 `extract-v12`: правила полей нанимателя, few-shot, `normalize_employment_type`. Проверка — `tests/test_extract_prompt_fields.py`
- [x] 1.3 Golden extraction: 5 кейсов (`tags: employer / employment`) + `evals/fixtures/extraction_outputs.json`. Проверка — `tests/test_evals_extraction.py`
- [x] 1.4 `match-v3`: язык текстовых полей, футер канала. Проверка — `tests/test_resume_matching.py` (промпт матчинга)
- [x] 1.5 `market-digest-v3`. Проверка — `tests/test_market_digest.py::test_prompt_v3_explains_volume_baseline_and_sources`
- [x] 1.6 `ai.prompt_version` по умолчанию, README
