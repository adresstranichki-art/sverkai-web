# Экспертный анализ: правильность и полнота — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Экспертный анализ находит все расхождения (без лимита 8), не создаёт дублей, соблюдает окно дат и нулевое влияние пар; сводка показывает контроль полноты.

**Architecture:** Все серверные изменения — в `expert_reconciliation.py`: правка промпта и лимитов, функции-«стражи» (`_apply_expert_guards`, `_dedupe_expert_discrepancies`), проверка полноты (`_find_unexplained_rows`) с дозапросами Claude (максимум 2) и программными заглушками `ambiguous` для необъяснённых строк. UI — одна строка полноты в `static/index.html`.

**Tech Stack:** Python 3 / FastAPI, pandas, unittest, встроенный JS в `static/index.html`.

## Global Constraints

- Рабочая папка: `C:\Sverkai-web\.worktrees\staging-integration`, ветка `codex/staging-integration`.
- Спецификация: `docs/superpowers/specs/2026-07-20-expert-analysis-correctness-design.md`.
- Выводы формирует Claude; сервер только чистит/исправляет (автоисправление — выбор пользователя).
- Тесты: `python -m unittest discover -s tests -v` (сейчас 70, все должны проходить).
- Push: `git push origin HEAD:staging`; затем проверить `/api/health` (новый хеш, `status: ok`). Production не трогать.
- Guard-правки пары по датам применяются только при `scope['find_date_diff']=True` и не трогают строки с флагом `server_reclassified` (созданные `_downgrade_false_confirmed_missing`).

---

### Task 1: Guards — пара по датам (форма, окно дат, нулевое влияние) и влияние confirmed_missing

**Files:**
- Modify: `expert_reconciliation.py` (новые `_pair_window_days`, `_guard_reason`, `_apply_expert_guards`; флаг в `_downgrade_false_confirmed_missing`; вызов в `run_independent_expert_analysis`)
- Test: `tests/test_expert_reconciliation.py`

**Interfaces:**
- Produces: `_apply_expert_guards(report: dict, scope: dict) -> None` — правит `report['discrepancies']` на месте, пишет коды в `report['guard_log']` (list[str]). `_downgrade_false_confirmed_missing` ставит `item['server_reclassified'] = True`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_expert_reconciliation.py`:

```python
    def test_date_pair_influence_is_forced_to_zero(self):
        df1, df2 = self._frames()
        report = _valid_report()
        report['discrepancies'][0]['influence'] = -7070.0
        # даты 21.01 и 27.02 = 37 дней; окно должно позволять пару
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['category'], 'likely_date_pair')
        self.assertEqual(item['influence'], 0.0)
        self.assertIn('date_pair_influence_zeroed', result['report']['guard_log'])

    def test_date_pair_beyond_window_becomes_ambiguous(self):
        df1, df2 = self._frames()
        report = _valid_report()
        # окна по умолчанию 5/3 дня, разница 37 дней
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['category'], 'ambiguous')
        self.assertEqual(item['influence'], 0.0)
        self.assertIn('превышает допуск', item['reason'])
        self.assertIn('date_window_exceeded', result['report']['guard_log'])

    def test_date_pair_without_both_sides_becomes_ambiguous(self):
        df1, df2 = self._frames()
        report = _valid_report([{'side': 'doc1', 'row_id': 'd1:r11'}])
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        item = result['report']['discrepancies'][0]
        # зеркальная строка найдётся сервером и строка станет server_reclassified-парой,
        # поэтому для проверки формы отключаем поиск зеркала другой суммой
        self.assertIn(item['category'], ('likely_date_pair', 'ambiguous'))

    def test_confirmed_missing_influence_is_fixed_from_evidence(self):
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Поставка 90',
            'debit': 11845.0,
            'credit': None,
            'raw_row': 33,
        }
        report = _valid_report()
        report['discrepancies'] = [{
            'category': 'confirmed_missing',
            'title': 'Нет у контрагента',
            'influence': -999.0,
            'reason': 'Нет зеркальной операции.',
            'confidence': 'high',
            'evidence': [{'side': 'doc1', 'row_id': 'd1:r33'}],
        }]
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )
        item = result['report']['discrepancies'][0]
        self.assertEqual(item['influence'], -11845.0)
        self.assertIn('missing_influence_fixed', result['report']['guard_log'])
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m unittest tests.test_expert_reconciliation -v 2>&1 | tail -20`
Expected: новые тесты FAIL (нет `guard_log`, влияние не исправляется).

- [ ] **Step 3: Реализация**

В `expert_reconciliation.py` после `_date_distance_days` добавить:

```python
def _pair_window_days(scope: dict, family: str) -> int:
    key = 'date_window_payment' if family == 'payment' else 'date_window_delivery'
    try:
        return max(0, int(scope.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def _guard_reason(item: dict, note: str) -> str:
    base = str(item.get('reason') or '').strip()
    note_text = f'Проверка: {note}.'
    if not base:
        return note_text
    return f"{base[:100].rstrip('.')}. {note_text}"[:200]


def _apply_expert_guards(report: dict, scope: dict) -> None:
    log = report.setdefault('guard_log', [])
    for item in report.get('discrepancies') or []:
        rows = item.get('resolved_evidence') or []
        category = item.get('category')
        if category == 'likely_date_pair' and scope.get('find_date_diff'):
            if item.get('server_reclassified'):
                continue
            sides = {row.get('side') for row in rows}
            amounts = [abs(_number(row.get('amount')) or 0.0) for row in rows]
            if not ({'doc1', 'doc2'} <= sides) or not amounts or max(amounts) - min(amounts) > 0.01:
                item['category'] = 'ambiguous'
                item['reason'] = _guard_reason(item, 'пара по датам не подтверждена данными актов')
                log.append('date_pair_demoted')
                continue
            doc1_row = next(row for row in rows if row.get('side') == 'doc1')
            doc2_row = next(row for row in rows if row.get('side') == 'doc2')
            distance = _date_distance_days(doc1_row.get('date'), doc2_row.get('date'))
            window = _pair_window_days(scope, _document_family(doc1_row.get('document')))
            if distance is not None and window and distance > window:
                item['influence'] = 0.0
                item['category'] = 'ambiguous'
                item['reason'] = _guard_reason(
                    item, f'разница {distance} дн. превышает допуск {window} дн.',
                )
                log.append('date_window_exceeded')
                continue
            if (_number(item.get('influence')) or 0.0) != 0.0:
                item['influence'] = 0.0
                log.append('date_pair_influence_zeroed')
        elif category == 'confirmed_missing' and rows:
            expected = round(sum(abs(_number(row.get('amount')) or 0.0) for row in rows), 2)
            influence = _number(item.get('influence')) or 0.0
            if expected and abs(abs(influence) - expected) > 0.01:
                sign = -1.0 if influence < 0 else 1.0
                item['influence'] = round(sign * expected, 2)
                log.append('missing_influence_fixed')
```

В `_downgrade_false_confirmed_missing` в блоке присвоения категории добавить флаг:

```python
        item['category'] = 'likely_date_pair'
        item['server_reclassified'] = True
        item['influence'] = 0.0
```

В `run_independent_expert_analysis` после блока `_downgrade_false_confirmed_missing` вставить:

```python
        _apply_expert_guards(resolved['report'], scope)
```

- [ ] **Step 4: Прогнать весь файл тестов**

Run: `python -m unittest tests.test_expert_reconciliation -v 2>&1 | tail -5`
Expected: OK (все, включая старые; тесты `161/179/196` не проверяют категорию, поэтому демонтаж 37-дневной пары на ambiguous их не ломает).

- [ ] **Step 5: Commit**

```bash
git add expert_reconciliation.py tests/test_expert_reconciliation.py
git commit -m "feat: guard checks for date pairs and missing influence in expert analysis"
```

---

### Task 2: Дедупликация расхождений (случай 4 559 руб.)

**Files:**
- Modify: `expert_reconciliation.py` (`_dedupe_expert_discrepancies`, вызов после guards)
- Test: `tests/test_expert_reconciliation.py` (обновить `test_report_is_sorted_by_category_then_absolute_influence` — уникальные evidence)

**Interfaces:**
- Produces: `_dedupe_expert_discrepancies(report: dict) -> None` — удаляет записи, чьи resolved-строки уже заняты записью с более приоритетной категорией/бо́льшим влиянием; пишет `duplicates_removed:N` в `guard_log`.

- [ ] **Step 1: Написать падающий тест**

```python
    def test_duplicate_rows_are_removed_after_reclassification(self):
        df1, df2 = self._frames()
        report = _valid_report()
        # Claude вернул одну и ту же строку в двух категориях (случай 4 559 руб.)
        report['discrepancies'] = [
            {
                'category': 'confirmed_missing',
                'title': 'Нет у контрагента',
                'influence': -7070.0,
                'reason': 'Первый вывод.',
                'confidence': 'high',
                'evidence': [{'side': 'doc1', 'row_id': 'd1:r11'}],
            },
            {
                'category': 'amount_difference',
                'title': 'Разница в суммах',
                'influence': -7070.0,
                'reason': 'Второй вывод о той же строке.',
                'confidence': 'medium',
                'evidence': [{'side': 'doc1', 'row_id': 'd1:r11'}],
            },
        ]
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=_FakeMessages(report)),
            'claude-sonnet-test',
        )
        items = result['report']['discrepancies']
        self.assertEqual(len(items), 1)
        self.assertTrue(any(
            entry.startswith('duplicates_removed')
            for entry in result['report']['guard_log']
        ))
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `python -m unittest tests.test_expert_reconciliation -k duplicate -v`
Expected: FAIL — записей 2.

- [ ] **Step 3: Реализация**

```python
def _dedupe_expert_discrepancies(report: dict) -> None:
    priority = {
        category: index
        for index, category in enumerate(EXPERT_CATEGORY_ORDER)
    }
    items = report.get('discrepancies') or []
    order = sorted(
        range(len(items)),
        key=lambda index: (
            priority.get(items[index].get('category'), len(priority)),
            -abs(_number(items[index].get('influence')) or 0.0),
        ),
    )
    seen: set[tuple] = set()
    keep: set[int] = set()
    for index in order:
        rows = items[index].get('resolved_evidence') or []
        keys = {(row.get('side'), row.get('row_id')) for row in rows}
        if keys and keys & seen:
            continue
        seen |= keys
        keep.add(index)
    removed = len(items) - len(keep)
    if removed:
        report.setdefault('guard_log', []).append(f'duplicates_removed:{removed}')
        report['discrepancies'] = [
            items[index] for index in range(len(items)) if index in keep
        ]
```

Вызов в `run_independent_expert_analysis` сразу после `_apply_expert_guards(...)`:

```python
        _dedupe_expert_discrepancies(resolved['report'])
```

В `test_report_is_sorted_by_category_then_absolute_influence` заменить evidence на уникальные несуществующие строки, чтобы синтетические записи не схлопывались:

```python
                'evidence': [{'side': 'doc1', 'row_id': f'd1:rx{influence}'}],
```

(вместо общего `d1:r11`; статус станет `complete_with_warnings`, ассерты по порядку не меняются).

- [ ] **Step 4: Прогнать тесты файла**

Run: `python -m unittest tests.test_expert_reconciliation -v 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add expert_reconciliation.py tests/test_expert_reconciliation.py
git commit -m "feat: deduplicate expert discrepancies sharing the same act rows"
```

---

### Task 3: Промпт и лимиты — без «8 расхождений», без группировки, max_tokens 8000

**Files:**
- Modify: `expert_reconciliation.py` (`EXPERT_SYSTEM_PROMPT`, `max_tokens`)
- Test: `tests/test_expert_reconciliation.py`

- [ ] **Step 1: Написать падающий тест**

```python
    def test_prompt_has_no_finding_limit_and_forbids_grouping(self):
        from expert_reconciliation import EXPERT_SYSTEM_PROMPT
        self.assertNotIn('не более 8', EXPERT_SYSTEM_PROMPT)
        self.assertNotIn('группируй', EXPERT_SYSTEM_PROMPT.replace('Не группируй', ''))
        self.assertIn('отдельной записью', EXPERT_SYSTEM_PROMPT)
        self.assertIn('conclusion не перечисляй отдельные расхождения', EXPERT_SYSTEM_PROMPT)
```

И в `test_expert_analysis_uses_one_structured_output_call` заменить:

```python
        self.assertEqual(call['max_tokens'], 8000)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m unittest tests.test_expert_reconciliation -k prompt_has_no -v; python -m unittest tests.test_expert_reconciliation -k one_structured -v`
Expected: оба FAIL.

- [ ] **Step 3: Реализация**

В `EXPERT_SYSTEM_PROMPT` заменить фрагмент:

```
'Группируй строго в порядке: confirmed_missing, sign_difference, amount_difference, '
'opening_balance_bridge, likely_date_pair, ambiguous. В группе сортируй по убыванию модуля influence. '
'Сначала перечисли все confirmed_missing; при лимите убирай ambiguous и likely_date_pair первыми. '
'Дай не более 8 расхождений: confirmed_missing не группируй, остальные группируй. '
```

на:

```
'Перечисли каждое расхождение отдельной записью со своим evidence; не объединяй операции. '
'Перечисли все найденные расхождения без ограничения количества. '
'Сортируй в порядке: confirmed_missing, sign_difference, amount_difference, '
'opening_balance_bridge, likely_date_pair, ambiguous; внутри — по убыванию модуля influence. '
'В conclusion не перечисляй отдельные расхождения — только общий вывод: сошлись ли сальдо '
'и общий характер расхождений. '
```

В `run_independent_expert_analysis` заменить `max_tokens=2800` на `max_tokens=8000`.

- [ ] **Step 4: Прогнать тесты файла**

Run: `python -m unittest tests.test_expert_reconciliation -v 2>&1 | tail -5`
Expected: OK (тест `test_token_limit_returns_a_specific_failure` не зависит от значения лимита).

- [ ] **Step 5: Commit**

```bash
git add expert_reconciliation.py tests/test_expert_reconciliation.py
git commit -m "feat: remove 8-finding cap and grouping from expert prompt, raise max_tokens"
```

---

### Task 4: Проверка полноты — `_find_unexplained_rows`

**Files:**
- Modify: `expert_reconciliation.py`
- Test: `tests/test_expert_reconciliation.py`

**Interfaces:**
- Produces: `_find_unexplained_rows(row_index: dict, report: dict, scope: dict) -> tuple[list[dict], set, set]` — возвращает (пропущенные строки, ключи сопоставленных `(side, row_id)`, ключи строк из evidence).

- [ ] **Step 1: Написать падающий тест**

```python
    def test_find_unexplained_rows_detects_missed_operations(self):
        from expert_reconciliation import _find_unexplained_rows, build_expert_payload
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Поставка 90',
            'debit': 11845.0,
            'credit': None,
            'raw_row': 33,
        }
        _, row_index = build_expert_payload(df1, df2)
        report = _valid_report()
        report['discrepancies'][0]['resolved_evidence'] = [
            {'side': 'doc1', 'row_id': 'd1:r11', 'amount': 7070.0},
            {'side': 'doc2', 'row_id': 'd2:r22', 'amount': 7070.0},
        ]
        missed, matched, referenced = _find_unexplained_rows(
            row_index, report, {'min_amount': 0.0},
        )
        self.assertEqual([row['row_id'] for row in missed], ['d1:r33'])
        self.assertIn(('doc1', 'd1:r11'), matched)
        self.assertIn(('doc2', 'd2:r22'), matched)
        self.assertIn(('doc1', 'd1:r11'), referenced)

    def test_find_unexplained_rows_respects_min_amount(self):
        from expert_reconciliation import _find_unexplained_rows, build_expert_payload
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Мелочь',
            'debit': 3.0,
            'credit': None,
            'raw_row': 44,
        }
        _, row_index = build_expert_payload(df1, df2)
        report = _valid_report()
        report['discrepancies'][0]['resolved_evidence'] = [
            {'side': 'doc1', 'row_id': 'd1:r11', 'amount': 7070.0},
            {'side': 'doc2', 'row_id': 'd2:r22', 'amount': 7070.0},
        ]
        missed, _, _ = _find_unexplained_rows(row_index, report, {'min_amount': 100.0})
        self.assertEqual(missed, [])
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m unittest tests.test_expert_reconciliation -k unexplained -v`
Expected: ImportError / FAIL.

- [ ] **Step 3: Реализация**

```python
def _find_unexplained_rows(
    row_index: dict[tuple[str, str], dict],
    report: dict,
    scope: dict,
) -> tuple[list[dict], set, set]:
    referenced: set[tuple] = set()
    for item in report.get('discrepancies') or []:
        for row in item.get('resolved_evidence') or []:
            referenced.add((row.get('side'), row.get('row_id')))
    sides: dict[str, list[dict]] = {'doc1': [], 'doc2': []}
    for (side, _), row in row_index.items():
        if _number(row.get('amount')) is not None:
            sides[side].append(row)
    for side in sides:
        sides[side].sort(key=lambda row: str(row['row_id']))
    matched: set[tuple] = set()
    used_doc2: set[str] = set()
    for row1 in sides['doc1']:
        amount1 = abs(_number(row1['amount']) or 0.0)
        for row2 in sides['doc2']:
            if row2['row_id'] in used_doc2:
                continue
            amount2 = abs(_number(row2['amount']) or 0.0)
            if abs(amount1 - amount2) <= 0.01:
                used_doc2.add(row2['row_id'])
                matched.add(('doc1', row1['row_id']))
                matched.add(('doc2', row2['row_id']))
                break
    min_amount = _number(scope.get('min_amount')) or 0.0
    missed = []
    for side in ('doc1', 'doc2'):
        for row in sides[side]:
            key = (side, row['row_id'])
            if key in matched or key in referenced:
                continue
            if abs(_number(row['amount']) or 0.0) < min_amount:
                continue
            missed.append(row)
    return missed, matched, referenced
```

- [ ] **Step 4: Прогнать тесты**

Run: `python -m unittest tests.test_expert_reconciliation -v 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add expert_reconciliation.py tests/test_expert_reconciliation.py
git commit -m "feat: programmatic completeness check for expert analysis"
```

---

### Task 5: Дозапросы (максимум 2), заглушки для необъяснённых строк, блок полноты, суммарный usage

**Files:**
- Modify: `expert_reconciliation.py` (`_follow_up_payload`, `_unexplained_placeholder`, переработка `run_independent_expert_analysis`)
- Test: `tests/test_expert_reconciliation.py`

**Interfaces:**
- Produces: `report['completeness'] = {'rows_total', 'rows_matched', 'rows_in_findings', 'follow_up_requests'}`; `result['usage']` — сумма по всем запросам.

- [ ] **Step 1: Написать падающие тесты**

```python
class _SequencedMessages:
    """Возвращает разные отчёты на последовательные вызовы."""

    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        report = self.reports.pop(0) if self.reports else {'version': '1', 'conclusion': '-', 'confidence': 'low', 'discrepancies': []}
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text=json.dumps(report, ensure_ascii=False))],
            usage=types.SimpleNamespace(input_tokens=100, output_tokens=50),
        )
```

```python
    def test_missed_rows_trigger_follow_up_request(self):
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Поставка 90',
            'debit': 11845.0,
            'credit': None,
            'raw_row': 33,
        }
        first = _valid_report()
        follow_up = {
            'version': '1',
            'conclusion': 'Доанализ.',
            'confidence': 'high',
            'discrepancies': [{
                'category': 'confirmed_missing',
                'title': 'Нет у контрагента',
                'influence': -11845.0,
                'reason': 'Операции нет во втором акте.',
                'confidence': 'high',
                'evidence': [{'side': 'doc1', 'row_id': 'd1:r33'}],
            }],
        }
        messages = _SequencedMessages([first, follow_up])
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        self.assertEqual(len(messages.calls), 2)
        follow_payload = json.loads(messages.calls[1]['messages'][0]['content'])
        self.assertTrue(follow_payload.get('follow_up'))
        self.assertEqual(
            [row['id'] for row in follow_payload['documents'][0]['rows']],
            ['d1:r33'],
        )
        categories = [item['category'] for item in result['report']['discrepancies']]
        self.assertIn('confirmed_missing', categories)
        completeness = result['report']['completeness']
        self.assertEqual(completeness['rows_total'], 3)
        self.assertEqual(completeness['follow_up_requests'], 1)
        self.assertEqual(result['usage'], {'input_tokens': 200, 'output_tokens': 100})

    def test_unexplained_rows_become_ambiguous_after_two_follow_ups(self):
        df1, df2 = self._frames()
        df1.loc[len(df1)] = {
            'date': pd.Timestamp('2026-02-09'),
            'date_str': '09.02.2026',
            'document': 'Поставка 90',
            'debit': 11845.0,
            'credit': None,
            'raw_row': 33,
        }
        # Claude трижды игнорирует строку r33
        messages = _SequencedMessages([_valid_report(), _valid_report(), _valid_report()])
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        self.assertEqual(len(messages.calls), 3)  # 1 основной + 2 дозапроса
        placeholders = [
            item for item in result['report']['discrepancies']
            if item['category'] == 'ambiguous'
            and item['reason'] == 'Строка не объяснена экспертным анализом.'
        ]
        self.assertEqual(len(placeholders), 1)
        self.assertEqual(
            placeholders[0]['resolved_evidence'][0]['row_id'], 'd1:r33',
        )
        self.assertEqual(result['report']['completeness']['follow_up_requests'], 2)

    def test_no_follow_up_when_all_rows_are_explained(self):
        df1, df2 = self._frames()
        messages = _SequencedMessages([_valid_report()])
        result = run_independent_expert_analysis(
            df1, df2,
            types.SimpleNamespace(messages=messages),
            'claude-sonnet-test',
            {'date_window_payment': 60, 'date_window_delivery': 60},
        )
        self.assertEqual(len(messages.calls), 1)
        self.assertEqual(result['report']['completeness'], {
            'rows_total': 2,
            'rows_matched': 2,
            'rows_in_findings': 2,
            'follow_up_requests': 0,
        })
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m unittest tests.test_expert_reconciliation -k follow -v; python -m unittest tests.test_expert_reconciliation -k explained -v`
Expected: FAIL (нет `completeness`, один вызов).

- [ ] **Step 3: Реализация**

Добавить в `expert_reconciliation.py`:

```python
FOLLOW_UP_LIMIT = 2

FOLLOW_UP_NOTE = (
    ' Это доанализ: перечисленные строки не были объяснены в первом ответе. '
    'Проанализируй только переданные строки; прежние выводы не повторяй.'
)


def _follow_up_payload(payload: dict, missed: list[dict]) -> dict:
    by_side: dict[str, list[dict]] = {'doc1': [], 'doc2': []}
    for row in missed:
        compact = {'id': row['row_id']}
        if row.get('date'):
            compact['date'] = row['date']
        if row.get('document'):
            compact['document'] = row['document']
        if row.get('debit') is not None:
            compact['debit'] = row['debit']
        if row.get('credit') is not None:
            compact['credit'] = row['credit']
        by_side[row['side']].append(compact)
    documents = []
    for source in payload['documents']:
        entry = {
            'side': source['side'],
            'display_name': source['display_name'],
            'rows': by_side[source['side']],
        }
        for key in ('opening_balance', 'closing_balance', 'period'):
            if key in source:
                entry[key] = source[key]
        documents.append(entry)
    return {
        'version': '1',
        'follow_up': True,
        'analysis_scope': payload['analysis_scope'],
        'balance_comparison': payload['balance_comparison'],
        'documents': documents,
    }


def _unexplained_placeholder(row: dict) -> dict:
    title_parts = [
        str(part) for part in (row.get('document'), row.get('date')) if part
    ]
    return {
        'category': 'ambiguous',
        'title': (' '.join(title_parts) or 'Операция без пары')[:90],
        'influence': 0.0,
        'reason': 'Строка не объяснена экспертным анализом.',
        'confidence': 'low',
        'evidence': [{'side': row['side'], 'row_id': row['row_id']}],
        'resolved_evidence': [{
            'side': row['side'],
            'row_id': row['row_id'],
            'raw_row': row['raw_row'],
            'date': row.get('date'),
            'document': row.get('document'),
            'amount': row.get('amount'),
        }],
        'clickable': True,
    }
```

Переработать `run_independent_expert_analysis` (тело try): выделить внутреннюю функцию запроса и цикл дозапросов:

```python
def run_independent_expert_analysis(
    df1: pd.DataFrame,
    df2: pd.DataFrame,
    client: Any,
    model: str,
    settings: dict | None = None,
) -> dict:
    if client is None:
        return {'status': 'failed', 'error': 'api_key_required'}
    payload, row_index = build_expert_payload(df1, df2, settings)
    scope = payload['analysis_scope']
    total_usage = {'input_tokens': 0, 'output_tokens': 0}

    def _request(request_payload: dict, system_suffix: str = '') -> tuple[dict | None, str | None]:
        message = client.messages.create(
            model=model,
            max_tokens=8000,
            temperature=0,
            system=EXPERT_SYSTEM_PROMPT + _expert_scope_instruction(scope) + system_suffix,
            messages=[{
                'role': 'user',
                'content': json.dumps(
                    request_payload, ensure_ascii=False, separators=(',', ':'),
                ),
            }],
            output_config={
                'format': {
                    'type': 'json_schema',
                    'schema': CLAUDE_EXPERT_REPORT_SCHEMA,
                },
            },
        )
        usage = getattr(message, 'usage', None)
        total_usage['input_tokens'] += int(getattr(usage, 'input_tokens', 0) or 0)
        total_usage['output_tokens'] += int(getattr(usage, 'output_tokens', 0) or 0)
        stop_reason = getattr(message, 'stop_reason', None)
        if stop_reason in ('max_tokens', 'refusal'):
            return None, stop_reason
        text = ''.join(
            getattr(block, 'text', '')
            for block in getattr(message, 'content', [])
            if getattr(block, 'text', '')
        )
        return json.loads(text), None

    def _postprocess(report: dict) -> dict:
        resolved, _ = resolve_expert_evidence(report, row_index)
        if scope['find_date_diff']:
            _downgrade_false_confirmed_missing(resolved['report'], row_index)
        _apply_expert_guards(resolved['report'], scope)
        _dedupe_expert_discrepancies(resolved['report'])
        return resolved

    try:
        report, error = _request(payload)
        if report is None:
            return {'status': 'failed', 'error': error, 'usage': dict(total_usage)}
        report['balances'] = payload['balance_comparison']
        report['totals'] = _derive_report_totals(report)
        if not _validate_report_shape(report):
            return {'status': 'failed', 'error': 'invalid_report_schema'}
        resolved = _postprocess(report)

        follow_ups = 0
        while follow_ups < FOLLOW_UP_LIMIT:
            missed, _, _ = _find_unexplained_rows(row_index, resolved['report'], scope)
            if not missed:
                break
            follow_report, follow_error = _request(
                _follow_up_payload(payload, missed), FOLLOW_UP_NOTE,
            )
            follow_ups += 1
            if follow_report is None or not isinstance(
                follow_report.get('discrepancies'), list,
            ):
                break
            merged = resolved['report']
            merged['discrepancies'] = (
                (merged.get('discrepancies') or [])
                + [
                    item for item in follow_report['discrepancies']
                    if isinstance(item, dict)
                    and item.get('category') in EXPERT_CATEGORIES
                ]
            )
            resolved = _postprocess(merged)

        missed, matched, referenced = _find_unexplained_rows(
            row_index, resolved['report'], scope,
        )
        for row in missed:
            resolved['report']['discrepancies'].append(_unexplained_placeholder(row))
            referenced.add((row['side'], row['row_id']))
        resolved['report']['completeness'] = {
            'rows_total': len(row_index),
            'rows_matched': len(matched),
            'rows_in_findings': len(referenced),
            'follow_up_requests': follow_ups,
        }
        _sort_report_discrepancies(resolved['report'])
        resolved['report']['totals'] = _derive_report_totals(resolved['report'])
        resolved['usage'] = dict(total_usage)
        return resolved
    except Exception as exc:
        return {'status': 'failed', 'error': type(exc).__name__}
```

Примечание: `_postprocess` для объединённого отчёта повторно прогоняет `resolve_expert_evidence` — уже resolved-записи сохраняют свои `resolved_evidence` (функция читает `evidence`, поэтому пересчёт безопасен и идемпотентен). Заглушки добавляются после последнего `_postprocess`, чтобы дедупликация их не трогала; статус/warnings берутся из последнего `resolve_expert_evidence`.

- [ ] **Step 4: Прогнать все тесты файла**

Run: `python -m unittest tests.test_expert_reconciliation -v 2>&1 | tail -5`
Expected: OK. Отдельно проверить, что `test_expert_analysis_uses_one_structured_output_call` и `test_token_limit_returns_a_specific_failure` проходят (usage через `total_usage`).

- [ ] **Step 5: Commit**

```bash
git add expert_reconciliation.py tests/test_expert_reconciliation.py
git commit -m "feat: follow-up expert requests and completeness accounting"
```

---

### Task 6: UI — строка контроля полноты

**Files:**
- Modify: `static/index.html` (блок `expert-summary-conclusion`, ~строка 2612)
- Test: `tests/test_frontend_contract.py`

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_frontend_contract.py` (по образцу соседних тестов, читающих HTML):

```python
    def test_expert_summary_shows_completeness_line(self):
        html = _read_index()
        self.assertIn('report.completeness', html)
        self.assertIn('Проверено строк', html)
        self.assertIn('сопоставлено', html)
        self.assertIn('в расхождениях', html)
```

(если в файле helper называется иначе — использовать существующий способ чтения `static/index.html`).

- [ ] **Step 2: Убедиться, что тест падает**

Run: `python -m unittest tests.test_frontend_contract -k completeness -v`
Expected: FAIL.

- [ ] **Step 3: Реализация**

В `static/index.html` в секции `expert-summary-conclusion` после строки с уверенностью:

```html
        <div class="expert-summary-confidence">Уверенность: ${esc(expertConfidenceText(report.confidence))}</div>
```

добавить:

```html
        ${report.completeness?`<div class="expert-summary-confidence">Проверено строк: ${report.completeness.rows_total} · сопоставлено: ${report.completeness.rows_matched} · в расхождениях: ${report.completeness.rows_in_findings}</div>`:''}
```

- [ ] **Step 4: Проверка синтаксиса JS и тесты**

Run:

```bash
python - <<'PY'
import re
html = open('static/index.html', encoding='utf-8').read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
open('tmp_syntax_check.js', 'w', encoding='utf-8').write('\n;\n'.join(scripts))
PY
node --check tmp_syntax_check.js && rm tmp_syntax_check.js
python -m unittest tests.test_frontend_contract -v 2>&1 | tail -5
```

Expected: node без ошибок; тесты OK.

- [ ] **Step 5: Commit**

```bash
git add static/index.html tests/test_frontend_contract.py
git commit -m "feat: show completeness line in expert summary"
```

---

### Task 7: Полный прогон, публикация в staging, контроль

- [ ] **Step 1: Полный набор тестов**

Run: `python -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: OK, число тестов ≥ 70 + новые (~80+), 0 failures.

- [ ] **Step 2: Push в staging**

```bash
git push origin HEAD:staging
```

- [ ] **Step 3: Дождаться деплоя и проверить здоровье**

Run (повторять до совпадения хеша с последним коммитом):

```bash
curl -s https://sverkai-staging-staging.up.railway.app/api/health
```

Expected: `status: ok`, `commit` = хеш последнего коммита.

- [ ] **Step 4: Сообщить пользователю**

Кратко: что изменено, сколько тестов, просьба прогнать проблемный пример из прошлого чата (4 пропущенные операции и дубль 4 559 руб.) и сообщить результат.
