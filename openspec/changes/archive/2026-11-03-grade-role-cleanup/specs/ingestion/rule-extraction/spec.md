# Spec Delta

## ADDED Requirements

### Requirement: Грейд в начале роли не входит в роль
Специализация MUST сохраняться без грейда в начале имени: senior, middle, junior, sr, jr, mid, сеньор, мидл, джун(иор), в том числе с «+», если после грейда есть имя роли; затем применяются синонимы. Это SHALL действовать везде, где специализации приводятся к каноническим. Lead и Head SHALL NOT срезаться: `Team Lead`, `QA Lead`, `Head of Engineering` — роли. Имя из одного грейда и имя, которое им только начинается (`Middleware Engineer`), не меняются.

#### Scenario: Грейд от LLM
- **WHEN** LLM вернул специализации `["QA Lead", "Senior QA", "Middle QA"]`
- **THEN** специализации — `[QA Lead, QA]`

#### Scenario: Грейд перед ролью разработчика
- **WHEN** специализация — `Senior Python Developer`
- **THEN** разбор тот же, что у `Python Developer`: роль `Software Engineer`, технология `Python`
