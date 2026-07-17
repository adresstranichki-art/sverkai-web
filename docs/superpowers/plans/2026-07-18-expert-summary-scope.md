# Expert Summary Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Передавать Claude названия организаций и настройки экспертного анализа, возвращать сгруппированный по критичности отчёт и показывать его в компактной двухколоночной «Сводке» без дублирования и блока рекомендаций.

**Architecture:** Сервер извлекает читаемое название стороны из строки «По данным …» над выбранной таблицей и сохраняет его в `DataFrame.attrs`. `expert_reconciliation.py` формирует независимый payload с `display_name` и `analysis_scope`, а также задаёт Claude строгий контракт категорий и порядка. Клиент перестраивает только представление: метрики программы слева, заключение Claude справа, ниже — сгруппированные кликабельные расхождения.

**Tech Stack:** Python 3.13, pandas, FastAPI, Anthropic structured outputs, vanilla HTML/CSS/JavaScript, `unittest`.

## Global Constraints

- Обычная сверка и её результаты не передаются Claude.
- Настройки передаются Claude, но ответ не фильтруется программой повторно по этим настройкам.
- `doc1`/`doc2` допустимы только внутри `evidence`; пользовательский текст использует `display_name`.
- `opening_balance_bridge` разрешён независимо от переключателей и `min_amount`.
- Порядок категорий: `confirmed_missing`, `sign_difference`, `amount_difference`, `opening_balance_bridge`, `likely_date_pair`, `ambiguous`.
- Переход из «Расхождений» в «Сравнение» должен сохраниться.
- Максимальные окна оплат и поставок остаются равными 120 дням.

---

### Task 1: Контракт независимого анализа и порядок категорий

**Files:**
- Modify: `tests/test_expert_reconciliation.py`
- Modify: `expert_reconciliation.py`

**Interfaces:**
- Produces: `build_expert_payload(df1, df2, settings=None) -> (payload, row_index)`.
- Produces: `run_independent_expert_analysis(df1, df2, client, model, settings=None) -> dict`.
- Produces: payload fields `documents[].display_name` and `analysis_scope`.
- Produces: report without `actions` and `limitations`, sorted by `EXPERT_CATEGORY_ORDER` and descending `abs(influence)`.

- [ ] **Step 1: Write failing tests for settings, names, schema, prompt and sorting**

Add tests that set:

```python
df1.attrs['display_name'] = 'ООО «ПРООПТ»'
df2.attrs['display_name'] = 'ООО «Автомир-Трейд»'
settings = {
    'find_missing': True,
    'find_amount_diff': False,
    'find_sign_mismatch': True,
    'find_date_diff': False,
    'date_window_payment': 45,
    'date_window_delivery': 60,
    'min_amount': 1000,
}
payload, _ = build_expert_payload(df1, df2, settings)
self.assertEqual(payload['documents'][0]['display_name'], 'ООО «ПРООПТ»')
self.assertEqual(payload['analysis_scope'], settings)
```

Also assert that the sent schema has no `actions`/`limitations`, the prompt contains all six categories in the approved order, and a mixed report is returned in that order with larger absolute influence first. Add a test proving that `find_date_diff=False` prevents the existing mirror-row safeguard from creating `likely_date_pair`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m unittest tests.test_expert_reconciliation -v`

Expected: FAIL because the functions do not accept `settings`, `display_name` is absent, the schema still requires `actions`/`limitations`, and reports are not sorted.

- [ ] **Step 3: Implement the minimal expert contract**

In `expert_reconciliation.py`:

```python
EXPERT_CATEGORY_ORDER = (
    'confirmed_missing',
    'sign_difference',
    'amount_difference',
    'opening_balance_bridge',
    'likely_date_pair',
    'ambiguous',
)

DEFAULT_EXPERT_SCOPE = {
    'find_missing': True,
    'find_amount_diff': True,
    'find_sign_mismatch': True,
    'find_date_diff': True,
    'date_window_payment': 5,
    'date_window_delivery': 3,
    'min_amount': 0.0,
}
```

Normalize only these fields into `analysis_scope`; preserve the already-normalized window values received from `main.py`. Use `df.attrs['display_name']`, then `source_name`, then `Документ 1/2` as the fallback. Remove `actions` and `limitations` from the JSON schema and report-shape validation.

Extend the system prompt with explicit setting semantics, the category order, descending absolute influence, and the rule that `doc1`/`doc2` may appear only in evidence. After evidence resolution, sort discrepancies with:

```python
priority = {category: index for index, category in enumerate(EXPERT_CATEGORY_ORDER)}
report['discrepancies'].sort(
    key=lambda item: (
        priority.get(item.get('category'), len(priority)),
        -abs(_number(item.get('influence')) or 0.0),
    )
)
```

Call `_downgrade_false_confirmed_missing` only when `analysis_scope['find_date_diff']` is true.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m unittest tests.test_expert_reconciliation -v`

Expected: all expert reconciliation tests pass.

- [ ] **Step 5: Commit the contract**

```powershell
git add expert_reconciliation.py tests/test_expert_reconciliation.py
git commit -m "feat: pass expert analysis scope"
```

### Task 2: Извлечение названий сторон и интеграция endpoint

**Files:**
- Modify: `tests/test_regressions.py`
- Modify: `main.py`

**Interfaces:**
- Produces: `_extract_party_display_name(raw, party_column, fallback) -> str`.
- Produces: parser metadata `party_column` and `display_name`.
- Consumes: `run_independent_expert_analysis(..., settings=cfg)` from Task 1.

- [ ] **Step 1: Write failing parser and endpoint tests**

Create an in-memory header with labels in columns 1 and 9:

```python
raw = pd.DataFrame([[None] * 15 for _ in range(8)])
raw.iloc[6, 1] = 'По данным ООО "ПРООПТ", руб.'
raw.iloc[6, 9] = (
    'По данным Общество с ограниченной ответственностью '
    '"Автомир-Трейд", руб.'
)
self.assertEqual(
    self.main._extract_party_display_name(raw, 1, 'first.xlsx'),
    'ООО «ПРООПТ»',
)
self.assertEqual(
    self.main._extract_party_display_name(raw, 9, 'second.xls'),
    'ООО «Автомир-Трейд»',
)
```

Add a fallback test for a header without «По данным». Add an integration assertion that candidate frames from the Proopt fixtures have `display_name`. Patch `run_independent_expert_analysis` in the endpoint-level test and assert the received settings equal the normalized request settings.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m unittest tests.test_regressions -v`

Expected: FAIL because the extraction helper and metadata do not exist and the endpoint does not pass `cfg`.

- [ ] **Step 3: Implement name extraction and metadata attachment**

Add `_extract_party_display_name` near `_attach_meta`. It scans the first 20 rows in `party_column`, accepts `ООО` and the full legal form, extracts the quoted name, normalizes it to `ООО «Название»`, and returns `fallback` when no reliable label exists.

In `parse_proopt` and `parse_two_sided_act`, attach `party_column=date_col`. In `_collect_parse_candidates.add_candidate`, after parsing and before `_make_parse_candidate`, set:

```python
party_column = df.attrs.get('party_column')
df.attrs['display_name'] = _extract_party_display_name(
    raw_preview,
    party_column,
    cache_name,
)
```

If no column metadata is available, the helper searches all first-20-row cells for the first «По данным» label before falling back to the filename. Pass `cfg` as the final argument to `run_independent_expert_analysis`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m unittest tests.test_regressions -v`

Expected: all regression tests pass, including actual Proopt candidate metadata.

- [ ] **Step 5: Commit parser integration**

```powershell
git add main.py tests/test_regressions.py
git commit -m "feat: label expert document parties"
```

### Task 3: Двухколоночная сводка и группы расхождений

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `static/index.html`

**Interfaces:**
- Consumes: `summary.expert_report.balances`, `conclusion`, `confidence`, and ordered `discrepancies`.
- Preserves: `buildExpertDiscrepancies`, resolved evidence, `openDiscrepancy` navigation.

- [ ] **Step 1: Write failing frontend contract tests**

Assert that the HTML contains:

```python
self.assertIn('class="expert-summary-layout"', self.html)
self.assertIn('class="expert-summary-metrics"', self.html)
self.assertIn('class="expert-summary-conclusion"', self.html)
self.assertIn('EXPERT_CATEGORY_ORDER', self.html)
self.assertIn('expert-group-heading', self.html)
```

Assert that the independent rendering function no longer creates «Подтверждено / к проверке», no longer renders «Рекомендации и ограничения», and does not map `report.actions` or `report.limitations`. Keep existing evidence and navigation assertions.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m unittest tests.test_frontend_contract -v`

Expected: FAIL because the new layout and group headings do not exist.

- [ ] **Step 3: Implement layout, responsive CSS and grouped rows**

For independent mode, make `#summary-cards` render:

```html
<div class="expert-summary-layout">
  <div class="expert-summary-metrics">…three metric cards…</div>
  <section class="expert-summary-conclusion">…Claude conclusion/confidence…</section>
</div>
```

Use a two-column CSS grid with a compact left track and flexible right track; at the existing mobile breakpoint switch to one column. Use the existing green palette and accessible heading semantics.

Remove duplicate metric/formula/conclusion markup from `renderIndependentExpertAnalysis`. Keep the lower formula in `renderSummaryDetails`. Hide/remove the recommendations rendering for independent mode.

Define the same category order and labels in JavaScript. Sort only a copy of `report.discrepancies`, then insert a `.expert-group-heading` before each non-empty category group. Reuse resolved evidence unchanged so row navigation continues to work.

- [ ] **Step 4: Run frontend and JavaScript checks**

Run:

```powershell
python -m unittest tests.test_frontend_contract -v
$html = Get-Content -Raw static/index.html
$script = [regex]::Matches($html, '<script>([\s\S]*?)</script>')[-1].Groups[1].Value
$script | node --check -
```

Expected: frontend tests pass and Node exits 0.

- [ ] **Step 5: Commit the interface**

```powershell
git add static/index.html tests/test_frontend_contract.py
git commit -m "feat: redesign expert summary layout"
```

### Task 4: Полная проверка и staging

**Files:**
- Verify: `main.py`
- Verify: `expert_reconciliation.py`
- Verify: `static/index.html`
- Verify: `tests/`

**Interfaces:**
- Validates the complete user workflow from upload settings to Claude payload and rendered report.

- [ ] **Step 1: Run the complete automated suite**

Run:

```powershell
python -m unittest discover -s tests
python -m py_compile main.py expert_reconciliation.py
git diff --check
```

Expected: all tests pass, compilation succeeds, and `git diff --check` emits no errors.

- [ ] **Step 2: Run the primary workflow locally**

Start the application on an unused local port, upload the two Proopt fixtures, enable expert mode, and verify:

- payload names are `ООО «ПРООПТ»` and `ООО «Автомир-Трейд»`;
- disabling «Разница в датах» sends `find_date_diff=false`;
- the top summary is two-column on desktop and one-column on mobile;
- the recommendations block is absent;
- group headings follow the approved order;
- clicking a discrepancy opens and highlights source rows.

- [ ] **Step 3: Commit any verification-only test adjustments**

If no adjustment is needed, do not create an empty commit. Otherwise:

```powershell
git add tests
git commit -m "test: cover expert summary workflow"
```

- [ ] **Step 4: Push and verify staging**

Fetch `origin/staging`, confirm no remote divergence, push the current HEAD to `staging`, wait for Railway `SUCCESS`, and verify `/api/health` reports the new commit hash. Perform one public expert request with the Proopt fixtures and confirm `result_mode=independent_expert` and `expert_analysis_status=complete`.
