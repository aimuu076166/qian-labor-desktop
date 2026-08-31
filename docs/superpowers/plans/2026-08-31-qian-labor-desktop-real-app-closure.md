# Qian Labor Desktop Real App Closure Implementation Plan

> **For Codex:** Execute this plan task-by-task with strict red-green-refactor cycles. Do not add items outside the approved closure design.

**Goal:** Turn the current synthetic vertical slice into a genuinely usable Apple Silicon macOS application that securely accepts a user's Zhipu key, processes supported local materials with the real provider, resolves manual employee matches, runs R01—R20, and presents traceable Dashboard, employee-ledger, and printable-report results.

**Architecture:** Keep the existing Tauri host, authenticated loopback FastAPI sidecar, SQLite domain model, matching service, Dashboard service, parsers, privacy boundary, and deterministic rule engine. Add only the missing API contracts and React views. Tauri owns secrets and injects them into the sidecar process; React receives configuration status but never secret values.

**Tech Stack:** Rust/Tauri 2, macOS Keychain through the `security` framework or a narrowly scoped credential dependency, React 19/TypeScript, FastAPI/Pydantic/SQLAlchemy, Vitest/Testing Library, pytest, existing PyInstaller and GitHub Actions packaging.

---

## Task 1: Correct the analysis state contract

**Files:**
- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/features/dashboard/DashboardView.tsx`
- Modify: `apps/desktop/src/lib/api.ts`
- Test: `apps/desktop/tests/app.test.tsx`

1. Add a failing UI test proving `matching_review` renders a review screen and never renders “分析完成”.
2. Run `pnpm --dir apps/desktop test -- app.test.tsx` and confirm the expected failure.
3. Introduce explicit active, review, completed, failed state predicates and remove the hard-coded completion label.
4. Re-run the focused test and the full frontend suite.

## Task 2: Expose manual matching through the sidecar

**Files:**
- Modify: `python/src/qian_labor/desktop/schemas.py`
- Modify: `python/src/qian_labor/desktop/app.py`
- Modify: `python/src/qian_labor/matching/service.py`
- Test: `python/tests/desktop/test_matching_api.py`
- Test: `python/tests/regression/test_matching.py`

1. Write failing API tests for candidate listing, assign, create-unknown, merge, unmatched, stale decision, cross-analysis rejection, and automatic resume after the final decision.
2. Confirm the tests fail because routes/resume behavior are missing.
3. Add typed request/response schemas and token-protected routes.
4. After the last pending candidate, atomically move the analysis to queued/evaluating and submit/resume deterministic evaluation exactly once.
5. Re-run focused and full Python tests.

## Task 3: Implement the manual matching UI

**Files:**
- Create: `apps/desktop/src/features/matching/MatchingReview.tsx`
- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/lib/api.ts`
- Modify: `apps/desktop/src/styles.css`
- Test: `apps/desktop/tests/matching.test.tsx`
- Modify: `apps/desktop/tests/app.test.tsx`

1. Add failing component tests for masked evidence, candidate choices, each supported decision, error recovery, and transition back to processing.
2. Confirm failures are caused by the missing component/API.
3. Implement the smallest accessible review UI and API client needed by the tests.
4. Keep source identity values masked and prevent double submission.
5. Run focused, full test, lint, and typecheck commands.

## Task 4: Add secure Zhipu first-run settings

**Files:**
- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/desktop/src-tauri/src/sidecar.rs`
- Create: `apps/desktop/src-tauri/src/credentials.rs`
- Modify: `apps/desktop/src/lib/desktop.ts`
- Create: `apps/desktop/src/features/settings/SettingsView.tsx`
- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/styles.css`
- Test: Rust unit tests in `credentials.rs`/`sidecar.rs`
- Test: `apps/desktop/tests/settings.test.tsx`

1. Write failing Rust tests for credential status, write/delete semantics, generated pepper, sidecar environment injection, and redacted errors.
2. Write failing UI tests proving the Key value is write-only and analysis remains blocked until connection validation succeeds.
3. Implement Tauri commands that return only configured/validated metadata, never the secret.
4. Persist Key and pepper in macOS Keychain with service/account identifiers scoped to `cn.qianlabor.desktop`.
5. Restart the owned sidecar after configuration changes so provider settings are session-scoped.
6. Add a no-material provider connection-test command/endpoint using the configured text model.
7. Run Rust, frontend, secret-scan, and Python provider tests.

## Task 5: Make real-provider behavior honest and recoverable

**Files:**
- Modify: `python/src/qian_labor/settings.py`
- Modify: `python/src/qian_labor/ai/provider_factory.py`
- Modify: `python/src/qian_labor/ai/zhipu_provider.py`
- Modify: `python/src/qian_labor/desktop/app.py`
- Modify: `python/src/qian_labor/jobs/processing.py`
- Test: `python/tests/desktop/test_provider_status_api.py`
- Modify: `python/tests/regression/test_zhipu_provider.py`
- Modify: `python/tests/desktop/test_prepared_provider_pipeline.py`

1. Add failing tests for missing configuration, invalid credentials, network/model errors, no silent Fake fallback, and safe user-facing error codes.
2. Add provider-status and connection-test contracts without returning secrets.
3. Preserve detailed per-file processing errors and allow retry from failed analyses without duplicating durable facts.
4. Run focused and full Python tests plus sensitive-data scans.

## Task 6: Expose and render the complete Dashboard and employee ledger

**Files:**
- Modify: `python/src/qian_labor/desktop/schemas.py`
- Modify: `python/src/qian_labor/desktop/app.py`
- Modify: `python/src/qian_labor/services/dashboard.py` only if a tested contract gap requires it
- Modify: `apps/desktop/src/lib/api.ts`
- Modify: `apps/desktop/src/features/dashboard/DashboardView.tsx`
- Create: `apps/desktop/src/features/employees/EmployeeLedger.tsx`
- Create: `apps/desktop/src/features/employees/EmployeeDetail.tsx`
- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/styles.css`
- Test: `python/tests/desktop/test_dashboard_api.py`
- Test: `apps/desktop/tests/dashboard.test.tsx`
- Test: `apps/desktop/tests/employees.test.tsx`

1. Add failing API and UI tests for coverage, affected employees, review counts, categories, employee filters, employee detail, and empty/insufficient-data states.
2. Expose existing `DashboardService` payloads through typed routes.
3. Render the approved fields without changing R01—R20 semantics.
4. Ensure zero counts are contextual and never described as “无风险”.
5. Run focused and full suites.

## Task 7: Add the in-app printable report

**Files:**
- Create: `python/src/qian_labor/services/report.py`
- Modify: `python/src/qian_labor/desktop/app.py`
- Modify: `python/src/qian_labor/desktop/schemas.py`
- Create: `apps/desktop/src/features/report/ReportView.tsx`
- Modify: `apps/desktop/src/lib/api.ts`
- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/styles.css`
- Test: `python/tests/desktop/test_report_api.py`
- Test: `apps/desktop/tests/report.test.tsx`

1. Add failing tests proving report totals match Dashboard/ledger data and source/review/insufficient-data sections are retained.
2. Implement a read-only report payload assembled from existing services.
3. Render print CSS and a native/browser print action suitable for macOS “Save as PDF”.
4. Do not add Word templates or an editor.
5. Run focused and full suites.

## Task 8: End-to-end real-app verification

**Files:**
- Modify: `scripts/verify_desktop.py`
- Create or modify only necessary synthetic fixtures under existing fixture locations
- Modify: `docs/release/v0.1.0-rc.1-checklist.md`

1. Extend the synthetic end-to-end verifier to cover review → resume → Dashboard → ledger → report → restart → delete.
2. Run real-provider smoke with synthetic data only and a locally entered Key; if no Key is available, record this gate as blocked rather than substituting Fake.
3. Run full frontend, Python, Rust, packaging, source-history, and secret-scan gates.
4. Build the exact-head Apple Silicon bundle and verify ad-hoc signature, architecture, bundled sidecar, install, launch, analysis flow, persistence, deletion, and process cleanup.
5. Independently compare artifact hashes/manifest to the exact commit.
6. Update PR #2 with truthful evidence; do not merge. Convert to Ready only after every required gate passes.

## Required final commands

```bash
pnpm test
pnpm lint
pnpm typecheck
pnpm build:web
python/.venv/bin/python -m pytest python/tests -q
python/.venv/bin/python -m compileall -q python/src python/tests scripts
cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --check
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
python/.venv/bin/python scripts/verify_desktop.py
python/.venv/bin/python scripts/scan_sensitive.py
python/.venv/bin/python scripts/scan_public_history.py
```

Do not report the app complete from test output alone. Completion also requires a fresh installed-app macOS acceptance run against the exact artifact head.
