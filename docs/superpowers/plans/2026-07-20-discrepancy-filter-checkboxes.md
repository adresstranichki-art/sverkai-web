# Discrepancy Filter Checkboxes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить неявное состояние кнопок фильтра расхождений на нативные чекбоксы с массовым управлением через пункт «Все».

**Architecture:** Разметка фильтров остаётся компактной, но каждый фильтр становится `label` с `input type="checkbox"`. Чистая функция `nextFilterSelection` рассчитывает новое множество выбранных типов, а DOM-функции синхронизируют чекбоксы, оформление и видимость строк.

**Tech Stack:** HTML, CSS, vanilla JavaScript, Python unittest, Node.js для проверки чистой JavaScript-функции.

## Global Constraints

- Отмеченный тип показывается, неотмеченный скрывается.
- «Все» включает или отключает все доступные типы одним действием.
- При частичном выборе «Все» не отмечено.
- Экспертные типы показываются только при наличии соответствующих строк.
- После новой сверки отмечены все доступные типы.
- Используются нативные доступные с клавиатуры чекбоксы.

---

### Task 1: Контракт и поведение фильтров

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `static/index.html`

**Interfaces:**
- Consumes: `activeFilters: Set<string>`, DOM-элементы `.filter-btn[data-filter-type]`.
- Produces: `nextFilterSelection(current, type, checked, available): Set<string>`, `availableFilterTypes(): string[]`, `syncFilterControls(): void`.

- [ ] **Step 1: Добавить падающие тесты разметки**

Добавить проверки, что восемь фильтров содержат `input type="checkbox"`, изменение каждого чекбокса вызывает `toggleFilter(type, this.checked)`, а подпись находится в той же кликабельной области.

- [ ] **Step 2: Добавить падающий тест чистой функции выбора**

Извлечь `nextFilterSelection` из встроенного JavaScript и выполнить через Node.js три сценария: снятие одного типа сохраняет остальные; снятие «Все» возвращает пустое множество; включение «Все» возвращает все доступные типы.

- [ ] **Step 3: Запустить тесты и подтвердить ожидаемое падение**

Run: `python -m unittest tests.test_frontend_contract -v`

Expected: FAIL, потому что фильтры ещё являются кнопками и `nextFilterSelection` отсутствует.

- [ ] **Step 4: Реализовать разметку и стили**

Заменить кнопки на `label.filter-btn`, добавить нативные чекбоксы, видимый focus-состояние, `accent-color` и сохранить существующие цветовые состояния.

- [ ] **Step 5: Реализовать управление состоянием**

Добавить `FILTER_TYPES`, `nextFilterSelection`, `availableFilterTypes`, `syncFilterControls`; обновить `toggleFilter`, `applyDiscFilter`, `resetDiscFilters`, `syncExpertFilterButtons` и `decorateTypeButtons`. При каждом новом результате вызывать `resetDiscFilters()`.

- [ ] **Step 6: Запустить контрактные тесты**

Run: `python -m unittest tests.test_frontend_contract -v`

Expected: PASS.

### Task 2: Полная проверка и публикация

**Files:**
- Verify: `static/index.html`
- Verify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes: завершённую реализацию Task 1.
- Produces: проверенный коммит в `origin/staging`.

- [ ] **Step 1: Проверить синтаксис встроенного JavaScript**

Извлечь содержимое `<script>` из `static/index.html` и передать в `node --check -`.

- [ ] **Step 2: Запустить полный набор тестов**

Run: `python -m unittest discover -s tests -v`

Expected: zero failures.

- [ ] **Step 3: Проверить diff и пробелы**

Run: `git diff --check` и `git status --short`.

Expected: только план, тесты и интерфейс текущей задачи.

- [ ] **Step 4: Создать коммит и отправить в staging**

Run: `git add static/index.html tests/test_frontend_contract.py docs/superpowers/plans/2026-07-20-discrepancy-filter-checkboxes.md && git commit -m "feat: add checkbox discrepancy filters" && git push origin HEAD:staging`.

- [ ] **Step 5: Проверить Railway**

Опросить `https://sverkai-staging-staging.up.railway.app/api/health` до ответа `status=ok` с версией нового коммита.
