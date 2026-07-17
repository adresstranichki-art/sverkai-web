# Repository and Railway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the current SverkAI application to `adresstranichki-art/sverkai-web` with sanitized Git history, production and staging Railway environments, and migrated persistent application data.

**Architecture:** Keep the FastAPI application and static frontend in one Railway service. Railway builds from `main` for production and `staging` for staging; each environment receives isolated secrets and a persistent volume mounted at `/app/data`. Historical commits are preserved after removing secret-bearing files and replacing historical Anthropic tokens.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, Railway Railpack, GitHub, Git filter-repo.

## Global Constraints

- Destination repository: `https://github.com/adresstranichki-art/sverkai-web.git`.
- Production deploys from `main`; staging deploys from `staging`.
- No API key, administrator secret, client spreadsheet, history file, or guest-usage file may be committed.
- Preserve non-secret Git history.
- Migrate `data/allowed_keys.json`, `data/guest_usage.json`, and `data/history_*.json` to persistent Railway storage.
- Use distinct generated `ADMIN_SECRET` values for production and staging.

---

### Task 1: Prepare the production repository tree

**Files:**
- Modify: `.gitignore`
- Modify: `main.py`
- Modify: `static/landing.html`
- Create: `railway.toml`
- Modify: `docs/deployment-stands.md`
- Create: `docs/superpowers/plans/2026-07-17-repository-railway-migration.md`

**Interfaces:**
- Consumes: the tracked source tree at commit `d0ec9a0` and the current local edits in `C:/Sverkai-web/main.py` and `C:/Sverkai-web/static/landing.html`.
- Produces: a deployable source tree whose `/` route serves `static/index.html` and whose Railway start command launches `main:app`.

- [ ] **Step 1: Copy the current runtime edits into the isolated worktree**

Copy `C:/Sverkai-web/main.py` to `main.py` and `C:/Sverkai-web/static/landing.html` to `static/landing.html` without copying untracked screenshots or spreadsheets.

- [ ] **Step 2: Expand repository exclusions**

Add explicit ignores for `.env*`, `*.xls`, `*.xlsx`, `*.pdf`, `tmp_*`, `.playwright-mcp/`, `data/history*.json`, and `data/guest_usage.json`, while retaining `data/allowed_keys.json` only until its values have been exported to Railway.

- [ ] **Step 3: Add Railway configuration**

Create `railway.toml` with Railpack, start command `uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-keep-alive 120`, health check `/api/health`, and restart-on-failure policy.

- [ ] **Step 4: Document production and staging configuration**

Update `docs/deployment-stands.md` to state that `/app/data` is a persistent volume and that secrets exist only in Railway Variables.

- [ ] **Step 5: Commit the deployable tree**

Run `git add .gitignore main.py static/landing.html railway.toml docs/deployment-stands.md docs/superpowers/plans/2026-07-17-repository-railway-migration.md` and commit with `chore: prepare Railway migration`.

### Task 2: Verify application behavior and repository safety

**Files:**
- Test: `tests/test_regressions.py`
- Verify: `main.py`
- Verify: `static/index.html`
- Verify: `.gitignore`

**Interfaces:**
- Consumes: the repository tree from Task 1 and eight local regression fixture spreadsheets copied temporarily from `C:/Sverkai-web`.
- Produces: evidence that tests pass, the HTTP root and health endpoint respond, and no committed secret patterns remain.

- [ ] **Step 1: Run regression tests**

Temporarily copy the eight fixture files referenced by `tests/test_regressions.py`, run `python -m pytest -q`, expect `28 passed`, then delete only those temporary copies.

- [ ] **Step 2: Run an HTTP smoke test**

Start Uvicorn on an unused localhost port, request `/` and `/api/health`, and require HTTP 200 from both endpoints.

- [ ] **Step 3: Scan the committed tree**

Use Git grep and filename checks to reject `sk-ant-api`, `sverkai-admin-`, `.env`, client spreadsheets, history JSON, and guest-usage JSON in the staged tree.

### Task 3: Sanitize and publish Git history

**Files:**
- Rewrite: Git object history in a disposable mirror clone
- Remove from all history: files matching `Ключ API*.txt`
- Replace in all history: every historical token matching `sk-ant-[A-Za-z0-9_-]{20,}`

**Interfaces:**
- Consumes: the verified migration branch from Task 2.
- Produces: sanitized `main` and `staging` branches in `adresstranichki-art/sverkai-web`.

- [ ] **Step 1: Create a disposable mirror clone**

Clone the local repository as a mirror into a temporary directory so the user's original history remains untouched.

- [ ] **Step 2: Build a private replacement list**

Collect unique historical Anthropic token strings into a temporary file outside the repository; record only the count, never the values.

- [ ] **Step 3: Rewrite secret-bearing history**

Run Git filter-repo to remove historical API-key text files and replace each collected token with `***REMOVED_SECRET***`.

- [ ] **Step 4: Verify sanitized history**

Scan every rewritten commit and require zero matching Anthropic tokens and zero API-key text files.

- [ ] **Step 5: Push branches**

Force-push the sanitized migration tip to destination `main`, create destination `staging` at the same verified tip, and confirm the destination refs.

### Task 4: Configure Railway environments

**Files:**
- External configuration: Railway project, service, production environment, staging environment

**Interfaces:**
- Consumes: GitHub branches from Task 3 and user-authenticated Railway access.
- Produces: production and staging services with isolated variables and volumes.

- [ ] **Step 1: Authenticate Railway**

Use official browser/device authentication; do not request a Railway password or token in chat.

- [ ] **Step 2: Create the service from GitHub**

Connect `adresstranichki-art/sverkai-web`, configure production from `main`, and configure staging from `staging`.

- [ ] **Step 3: Create isolated variables**

Set `SVERKAI_ENV`, `ANTHROPIC_API_KEY`, `ADMIN_SECRET`, `SVERKAI_ALLOWED_KEY_HASHES`, `SVERKAI_GUEST_KEY_HASHES`, `SVERKAI_GUEST_RECONCILE_LIMIT=2`, `SVERKAI_GUEST_EXPERT_AUDIT_LIMIT=1`, `SVERKAI_GUEST_USAGE_WINDOW_DAYS=30`, `SVERKAI_GUEST_MAX_FILE_MB=2`, and `SVERKAI_USER_MAX_FILE_MB=10`. Generate different administrator secrets for each environment and keep them outside Git and chat.

- [ ] **Step 4: Attach persistent volumes**

Attach one environment-isolated volume at `/app/data` for production and one at `/app/data` for staging.

### Task 5: Migrate persistent data and validate deployment

**Files:**
- Source: `C:/Sverkai-web/data/allowed_keys.json`
- Source: `C:/Sverkai-web/data/guest_usage.json`
- Source: `C:/Sverkai-web/data/history_3920970290a54bb5b932333f.json`
- Destination: Railway `/app/data/`

**Interfaces:**
- Consumes: the source data files and Railway services from Task 4.
- Produces: production and staging deployments with migrated state and verified endpoints.

- [ ] **Step 1: Upload data without Git**

Transfer the three source JSON files directly to each Railway volume through the authenticated Railway connection; never stage or commit them.

- [ ] **Step 2: Validate server state**

Request `/api/health`, `/api/status`, the root page, and the administrator flow in each environment; require successful responses and correct `SVERKAI_ENV` reporting.

- [ ] **Step 3: Validate persistence**

Redeploy each environment once and confirm that the migrated files and allowlist remain present.

- [ ] **Step 4: Record the handoff**

Report the GitHub repository, production URL, staging URL, commit IDs, test result, data file counts, and any remaining user-only action such as entering the newly rotated Anthropic key.
