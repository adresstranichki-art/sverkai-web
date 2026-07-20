# Expert Findings Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Выводить экспертные расхождения в колонках «Документ», «Влияние», «Пояснение».

**Architecture:** `buildIndependentExpertDiscrepancies` собирает уникальные названия источников. Отдельный рендерер экспертных строк использует их и не меняет универсальный рендерер программного анализа.

**Tech Stack:** HTML, CSS, vanilla JavaScript, Python unittest, Node.js.

## Global Constraints

- Изменяется только таблица выводов независимого экспертного анализа.
- Колонка «Документ» не содержит категорию или произвольный заголовок Claude.
- Группировка по типам и существующий порядок групп сохраняются.
- Отсутствующий источник обозначается текстом `Документ не определён`.

---

### Task 1: Контракт экспертной строки

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `static/index.html`

**Interfaces:**
- Consumes: `resolved_evidence[].document`, `title`, `reason`, `evidence_warning`, `influence`.
- Produces: `source_documents: string[]`, `expertDocumentLabel(documents): string`, `renderIndependentExpertRows(items): string`.

- [ ] **Step 1: Добавить падающие тесты**

Проверить наличие трёх заголовков, отдельного рендерера, сбор уникальных `source_documents`, объединение двух документов через ` ↔ ` и заглушку для пустого списка.

- [ ] **Step 2: Подтвердить ожидаемое падение**

Run: `python -m unittest tests.test_frontend_contract.FrontendContractTests.test_independent_expert_findings_use_document_influence_explanation_columns -v`

Expected: FAIL, потому что отдельного экспертного рендерера ещё нет.

- [ ] **Step 3: Реализовать минимальное отображение**

Добавить в преобразование результата уникальные названия документов, чистую функцию формирования подписи и отдельный HTML-рендерер. В пояснение поместить `title`, `reason` и `evidence_warning`.

- [ ] **Step 4: Проверить целевой контракт**

Run: `python -m unittest tests.test_frontend_contract -v`

Expected: PASS.

### Task 2: Проверка и публикация

**Files:**
- Verify: `static/index.html`
- Verify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes: завершённую реализацию Task 1.
- Produces: проверенный коммит в `origin/staging`.

- [ ] **Step 1: Проверить JavaScript и полный набор тестов**

Run: проверка синтаксиса встроенного JavaScript и `python -m unittest discover -s tests -v`.

Expected: zero failures.

- [ ] **Step 2: Проверить изменения**

Run: `git diff --check` и `git status --short`.

Expected: только файлы текущей задачи.

- [ ] **Step 3: Опубликовать и проверить staging**

Создать один коммит, выполнить `git push origin HEAD:staging`, затем проверить `/api/health` до появления версии нового коммита.

