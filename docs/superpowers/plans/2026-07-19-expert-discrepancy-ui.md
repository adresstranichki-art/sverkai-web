# Expert Discrepancy UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Унифицировать типы, фильтры и подсветку расхождений программного и независимого экспертного анализа.

**Architecture:** Claude продолжает возвращать стабильные enum-категории. Фронтенд преобразует их в канонические программные типы для подписей, фильтров и цветов, сохраняя исходную категорию для экспертной сортировки и сводки.

**Tech Stack:** Python 3, Anthropic structured output, HTML/CSS/vanilla JavaScript, unittest.

## Global Constraints

- Не менять JSON enum-категории экспертного анализа.
- Использовать термин «Зеркальный КСФ».
- Сохранить переход из строки расхождения к исходной строке «Сравнения».
- Отправить проверенный результат в `origin/staging`.

---

### Task 1: Frontend contract

**Files:**
- Modify: `tests/test_frontend_contract.py`
- Modify: `static/index.html`

**Interfaces:**
- Consumes: `summary.expert_report.discrepancies[*].category` and `resolved_evidence`.
- Produces: `standardTypeForExpertItem(item, resolved)` and normalized `type` on table rows.

- [ ] **Step 1: Write failing contract tests**

Add assertions that the discrepancy header omits «Документ», the table uses seven cells, expert categories map to standard types, expert-only filters exist, «Зеркальный КСФ» is the only sign label, and opening-balance evidence maps to the green highlight bucket.

- [ ] **Step 2: Run the frontend contract tests and verify failure**

Run: `python -m unittest tests.test_frontend_contract -v`

Expected: failures for the old eight-column table, missing normalization, old label, missing filters, and red opening-balance mapping.

- [ ] **Step 3: Implement the minimal frontend behavior**

Remove the document header/cell, change all colspans to seven, add `standardTypeForExpertItem`, keep `expert_category`, filter on normalized `type`, add expert-only filter buttons, rename the sign label, and route `opening_balance_bridge` rows to `expert1`/`expert2`.

- [ ] **Step 4: Run the frontend contract tests**

Run: `python -m unittest tests.test_frontend_contract -v`

Expected: all frontend contract tests pass.

### Task 2: Claude terminology contract

**Files:**
- Modify: `tests/test_expert_reconciliation.py`
- Modify: `expert_reconciliation.py`

**Interfaces:**
- Consumes: `EXPERT_SYSTEM_PROMPT`.
- Produces: a fixed Russian user-facing term dictionary inside the prompt while leaving `EXPERT_CATEGORIES` unchanged.

- [ ] **Step 1: Write a failing prompt test**

Assert that the system prompt requires all seven approved Russian terms in `conclusion`, `title`, and `reason`, including side-specific missing labels.

- [ ] **Step 2: Run the prompt test and verify failure**

Run: `python -m unittest tests.test_expert_reconciliation.ExpertReconciliationTests.test_prompt_uses_standard_user_facing_terms -v`

Expected: failure because the current prompt does not define the dictionary.

- [ ] **Step 3: Add the terminology instruction**

Extend `EXPERT_SYSTEM_PROMPT` with the exact mapping from enum categories to approved Russian terms and forbid alternative category names in user-facing fields.

- [ ] **Step 4: Run expert-analysis tests**

Run: `python -m unittest tests.test_expert_reconciliation -v`

Expected: all expert reconciliation tests pass.

### Task 3: Verification and staging deployment

**Files:**
- Verify: `static/index.html`
- Verify: `expert_reconciliation.py`
- Verify: `tests/test_frontend_contract.py`
- Verify: `tests/test_expert_reconciliation.py`

**Interfaces:**
- Consumes: completed Tasks 1–2.
- Produces: tested Git commit deployed from `origin/staging`.

- [ ] **Step 1: Run the complete test suite**

Run: `python -m unittest discover -s tests -v`

Expected: zero failures.

- [ ] **Step 2: Inspect the diff and whitespace**

Run: `git diff --check` and `git diff --stat`.

Expected: no whitespace errors; only approved files changed.

- [ ] **Step 3: Commit and push**

Run: `git add static/index.html expert_reconciliation.py tests/test_frontend_contract.py tests/test_expert_reconciliation.py docs/superpowers/specs/2026-07-19-expert-discrepancy-ui-design.md docs/superpowers/plans/2026-07-19-expert-discrepancy-ui.md && git commit -m "feat: unify expert discrepancy types" && git push origin HEAD:staging`.

- [ ] **Step 4: Verify Railway**

Poll `https://sverkai-staging-staging.up.railway.app/api/health` until `version` starts with the new commit hash and `status` equals `ok`.
