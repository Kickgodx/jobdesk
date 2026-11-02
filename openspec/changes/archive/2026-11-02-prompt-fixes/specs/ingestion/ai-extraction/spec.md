# Spec Delta

## ADDED Requirements

### Requirement: Компания и занятость из ответа LLM
Промпт извлечения MUST определять `company` как нанимателя: не Telegram-канал, не агрегатор и не рекрутер; у кадрового агентства — клиент, если он назван, иначе агентство; описание без названия («крупный финтех») — `null`. Контакт канала из футера SHALL NOT становиться `contact`. `employment_type` ответа LLM MUST приводиться к значениям правил `full-time`, `part-time`, `contract` (фриланс, проектная работа, ГПХ — `contract`); другое значение SHALL становиться `null`.

#### Scenario: Агентство ищет для клиента
- **WHEN** пост «Кадровое агентство TalentHub ищет Java-разработчика для клиента NordBank»
- **THEN** ожидаемая компания — `NordBank` (golden `agency-for-named-client`)

#### Scenario: Занятость свободным текстом
- **WHEN** LLM вернул `employment_type` «Полная занятость» или «ГПХ»
- **THEN** сохраняется `full-time` или `contract`

#### Scenario: Непонятная занятость
- **WHEN** LLM вернул `employment_type` «5/2, офис»
- **THEN** `employment_type` из ответа LLM — `null`
