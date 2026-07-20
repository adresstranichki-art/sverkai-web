# Original Excel Expert Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Передавать Claude два оригинальных Excel-файла через Files API и Code Execution, безопасно удалять загрузки и показывать предупреждение о конфиденциальности на production.

**Architecture:** `expert_reconciliation.py` локально строит только индекс физических строк для UI-ссылок, загружает исходные бинарные файлы в Anthropic Files API и выполняет один структурированный запрос с `container_upload` и Code Execution. Все `file_id` удаляются в гарантированной очистке. `static/index.html` использует существующий `env-badge` для предупреждения на production и индикатора окружения на остальных стендах.

**Tech Stack:** Python 3, pandas, openpyxl, xlrd, Anthropic Files API beta, Anthropic Code Execution, FastAPI, vanilla JavaScript, unittest.

## Global Constraints

- Оригинальные `.xls`/`.xlsx` передаются без преобразования содержимого в JSON.
- В экспертный запрос не передаются программные пары, операции, сальдо или выводы стандартной сверки.
- Все успешно загруженные `file_id` удаляются при любом исходе запроса.
- Содержимое файлов и `file_id` не журналируются и не сохраняются.
- Пользовательские настройки передаются полностью.
- Текущий UI-контракт `independent_expert` и архивный модуль v2 сохраняются.
- На production используется точный утверждённый пользователем текст предупреждения.

---

### Task 1: Зафиксировать Files API-контракт тестами

**Files:**
- Modify: `tests/test_expert_reconciliation.py`
- Modify: `expert_reconciliation.py`

**Interfaces:**
- Consumes: `df.attrs['source_path']`, `df.attrs['source_name']`, Anthropic client.
- Produces: `run_independent_expert_analysis(df1, df2, client, model, settings=None) -> dict`.

- [ ] **Step 1: Написать падающие тесты загрузки и запроса**

Добавить fake-клиент с `beta.files.upload/delete` и `beta.messages.create`. Проверить, что upload получает байты исходных книг, сообщение содержит два `container_upload`, `betas=['files-api-2025-04-14']`, инструмент `code_execution_20250825`, JSON-схему и настройки, но не содержит сырые ячейки или стандартные выводы.

- [ ] **Step 2: Подтвердить RED**

Run: `python -m unittest tests.test_expert_reconciliation -v`

Expected: FAIL, потому что активный модуль вызывает `client.messages.create` и отправляет JSON ячеек.

- [ ] **Step 3: Реализовать минимальный Files API-путь**

Добавить безопасное имя загрузки, MIME для `.xls`/`.xlsx`, загрузку двух файлов, манифест настроек, `container_upload`, Code Execution и существующий structured output.

- [ ] **Step 4: Подтвердить GREEN**

Run: `python -m unittest tests.test_expert_reconciliation -v`

Expected: все активные тесты проходят.

### Task 2: Гарантировать очистку и безопасные ошибки

**Files:**
- Modify: `tests/test_expert_reconciliation.py`
- Modify: `expert_reconciliation.py`

**Interfaces:**
- Produces: `_delete_uploaded_files(client, file_ids) -> list[str]` и предупреждение `remote_file_cleanup_failed`.

- [ ] **Step 1: Написать падающие тесты очистки**

Проверить удаление обоих файлов после успеха, первого файла после ошибки второй загрузки и обоих файлов после ошибки Messages API. Проверить, что ошибка delete отражается предупреждением без утечки `file_id`.

- [ ] **Step 2: Подтвердить RED**

Run: `python -m unittest tests.test_expert_reconciliation -v`

Expected: FAIL на отсутствующем гарантированном delete.

- [ ] **Step 3: Реализовать очистку**

Собирать `file_id` сразу после upload и выполнять delete для каждого в финальной фазе. Не включать идентификаторы и тексты исключений в публичный результат или логи.

- [ ] **Step 4: Подтвердить GREEN**

Run: `python -m unittest tests.test_expert_reconciliation tests.test_expert_reconciliation_v2 -v`

Expected: активная и архивная реализации проходят.

### Task 3: Production-предупреждение

**Files:**
- Modify: `static/index.html`
- Modify: `tests/test_regressions.py`

**Interfaces:**
- Consumes: `/api/health.app_env`.
- Produces: содержимое и видимость `#env-badge`.

- [ ] **Step 1: Написать падающий регрессионный тест**

Проверить наличие точного текста и ветку `production/prod`, которая всегда показывает badge, а staging продолжает показывать `Текущий стенд`.

- [ ] **Step 2: Подтвердить RED**

Run: `python -m unittest tests.test_regressions -v`

Expected: FAIL, потому что production сейчас скрывает `env-badge`.

- [ ] **Step 3: Реализовать минимальную ветку интерфейса**

В `checkHealth()` заполнить production badge утверждённым текстом, иначе оставить существующую подпись окружения. Не добавлять новый компонент.

- [ ] **Step 4: Подтвердить GREEN**

Run: `python -m unittest tests.test_regressions -v`

Expected: regression suite проходит.

### Task 4: Интеграция, публикация и проверка

**Files:**
- Verify: `expert_reconciliation.py`
- Verify: `expert_reconciliation_v2.py`
- Verify: `main.py`
- Verify: `static/index.html`
- Verify: `tests/`

**Interfaces:**
- Produces: проверенный Git-коммит и Railway-развёртывания.

- [ ] **Step 1: Проверить реальный бинарный комплект без платного Claude-вызова**

На файлах ПРООПТ проверить, что локальный манифест не содержит ячеек, а fake Files API получает исходные размеры и расширения файлов.

- [ ] **Step 2: Запустить полную проверку**

Run: `python -m unittest discover -s tests -v`, `python -m py_compile expert_reconciliation.py expert_reconciliation_v2.py main.py`, проверка синтаксиса встроенного JavaScript.

Expected: zero failures.

- [ ] **Step 3: Проверить diff**

Run: `git diff --check` и `git status --short`.

Expected: только файлы текущей функции и сохранённой v2.

- [ ] **Step 4: Создать коммит и отправить Git**

Run: `git add ...`, `git commit -m "feat: analyze original spreadsheets with Claude"`, затем push в согласованные staging/production ветки.

- [ ] **Step 5: Проверить Railway**

Проверить `/api/health` staging и production до появления новой версии, затем выполнить безопасную smoke-проверку интерфейса. Реальный платный экспертный анализ не запускать без отдельной необходимости.

