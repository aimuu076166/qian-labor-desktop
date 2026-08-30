# Public Migration Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing public-migration pull request so the repository can prove that every fetched public Git object is free of high-confidence credentials, every Rust build uses committed dependency resolution, all source-integrity checks are blocking CI gates, and the final six-job matrix is reproducible from the public repository.

**Architecture:** Keep the existing Desktop-only product and validation layers unchanged. Add one standard-library Python scanner that traverses fetched Git objects and shares credential-pattern definitions with the current-tree scanner; add regression tests for security and build configuration; commit Cargo's lockfile; pin the validated Rust toolchain; and extend the existing GitHub Actions workflow without adding services or product behavior.

**Tech Stack:** Python 3.12 and pytest, Git plumbing commands, Rust/Cargo 1.98.0 with rustfmt, pnpm 10, Tauri 2, GitHub Actions, GitHub pull-request and branch-rule APIs.

**Spec:** `docs/superpowers/specs/2026-08-27-qian-labor-desktop-architecture-design.md`

## Global Constraints

- Continue only on the existing `chore/complete-public-migration` branch and Pull Request #1; use fast-forward commits and never create another pull request.
- Do not merge, close, rebase, rewrite history, use `filter-repo`, force-push, or modify `main` directly.
- Do not redesign the product, add product features, change R01-R20 semantics or source traceability, weaken the human-review boundary, add a server stack, or introduce real providers, real employee data, real API calls, real credentials, key storage, signing, notarization, an updater, or production release automation.
- Keep all credential-shaped test data synthetic and assemble it from runtime fragments so neither source nor Git history contains a complete real-looking secret.
- Keep failure output metadata-only: pattern name, short object identifier, and path or message location. Never echo matched content, Git object content, or command stderr that could contain a credential.
- Preserve all existing CI action major versions unless a directly required compatibility fix is demonstrated.
- Treat a shallow repository or an unreadable/incomplete Git object graph as a scanner execution error with exit code 2, never as a clean result.

---

### Task 1: Add a test-driven public-history sensitive-information scanner

**Files:**

- Create: `scripts/sensitive_patterns.py`
- Create: `scripts/scan_public_history.py`
- Create: `python/tests/regression/test_public_history_scan.py`
- Modify: `scripts/scan_sensitive.py`
- Modify: `python/tests/regression/test_sensitive_scan.py`

- [ ] Extract the existing high-confidence provider credential regexes and binary-content predicate into `scripts/sensitive_patterns.py`; retain current placeholder and synthetic-fixture exclusions and keep one authoritative pattern source for both scanners.
- [ ] Add tests that construct temporary Git repositories and synthetic credential strings only at runtime. Cover a clean repository; a credential in the current committed tree; a credential deleted by a later commit; a credential in a commit message; a credential in an annotated-tag message; permitted placeholder and synthetic examples; binary content; duplicate blobs; a shallow clone; a non-Git directory; and a missing repository path.
- [ ] Assert the public interface exactly: exit 0 and `PUBLIC_HISTORY_SENSITIVE_SCAN=PASS` for a clean complete history; exit 1 with `PUBLIC_HISTORY_SENSITIVE_SCAN_FAIL=<PATTERN>:<SHORT_OID>:<PATH_OR_COMMIT_MESSAGE>` findings for detected credentials; and exit 2 with a stable `PUBLIC_HISTORY_SENSITIVE_SCAN=ERROR` plus a non-secret reason code for incomplete or invalid inputs.
- [ ] Assert that captured stdout and stderr never contain the dynamically assembled credential, even on Git failures, and that identical blob object IDs are scanned/reported at most once.
- [ ] Run the focused new test file before implementation and record the expected failure caused by the missing scanner.

Run: `python/.venv/bin/python -m pytest -q python/tests/regression/test_public_history_scan.py`

Expected: FAIL because `scripts/scan_public_history.py` and its contract do not yet exist.

- [ ] Implement the minimal standard-library scanner. Validate the requested path and Git repository, reject `rev-parse --is-shallow-repository=true`, enumerate all objects reachable from all fetched refs, deduplicate blob IDs, skip binary blobs, scan commit messages and annotated-tag messages, and convert every Git/object-read failure into exit code 2 without forwarding raw Git stderr.
- [ ] Refactor `scripts/scan_sensitive.py` to consume the shared definitions without changing its command-line behavior, then add a regression assertion that both scanners expose the same pattern names.
- [ ] Run both focused security suites and verify all cases pass.

Run: `python/.venv/bin/python -m pytest -q python/tests/regression/test_public_history_scan.py python/tests/regression/test_sensitive_scan.py`

Expected: PASS.

- [ ] Run both scanners against the isolated worktree and confirm neither prints sensitive content.

Run: `python/.venv/bin/python scripts/scan_sensitive.py .`

Expected: `SENSITIVE_SCAN=PASS` and exit 0.

Run: `python/.venv/bin/python scripts/scan_public_history.py .`

Expected: `PUBLIC_HISTORY_SENSITIVE_SCAN=PASS` and exit 0.

- [ ] Review the staged diff for literal credential-shaped fixtures, validate formatting, and commit only the scanner and security tests.

Commit: `test(security): add public history secret scan`

---

### Task 2: Make Rust dependency and toolchain resolution reproducible

**Files:**

- Create: `apps/desktop/src-tauri/Cargo.lock`
- Create: `rust-toolchain.toml`
- Create: `python/tests/regression/test_reproducible_build_config.py`
- Modify: `.github/workflows/desktop-ci.yml`

- [ ] Write configuration regression tests that require a tracked `apps/desktop/src-tauri/Cargo.lock`, an explicit Rust `1.98.0` pin, `--locked` on every CI Cargo build/test/check command, a lockfile-diff gate after Cargo commands, `cargo fmt --check`, and Python `compileall`. Extend this test for the full-history checkout and sixth job in Task 3.
- [ ] Run the configuration test before changing lock/toolchain/workflow files and record the expected failure for the missing lockfile and missing CI gates.

Run: `python/.venv/bin/python -m pytest -q python/tests/regression/test_reproducible_build_config.py`

Expected: FAIL on the intentionally absent reproducibility controls.

- [ ] Generate `apps/desktop/src-tauri/Cargo.lock` with Rust/Cargo 1.98.0 from the existing manifest and verify `.gitignore` does not suppress it.

Run: `cargo generate-lockfile --manifest-path apps/desktop/src-tauri/Cargo.toml`

Expected: exit 0 and a tracked lockfile candidate.

- [ ] Add `rust-toolchain.toml` with channel `1.98.0`, the minimal profile, and the rustfmt component. Configure every Rust GitHub Actions job to use exactly the same toolchain and component.
- [ ] Change all CI Cargo test/check/build invocations, including the Tauri package builds, to use `--locked`; add `cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all -- --check`; add `git diff --exit-code -- apps/desktop/src-tauri/Cargo.lock` after Cargo dependency/build activity so lockfile mutation is a hard failure.
- [ ] Add `python -m compileall -q python/src python/tests scripts` as a blocking Python-sidecar step.
- [ ] Run the focused configuration test and Rust formatting/build suites.

Run: `python/.venv/bin/python -m pytest -q python/tests/regression/test_reproducible_build_config.py`

Run: `cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all -- --check`

Run: `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked`

Run: `cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked`

Expected: all commands PASS and the lockfile remains unchanged.

- [ ] Commit the lockfile, pin, workflow reproducibility changes, and their regression test together.

Commit: `build(rust): lock desktop dependencies and toolchain`

---

### Task 3: Add the sixth CI security job and document reproducible verification

**Files:**

- Modify: `.github/workflows/desktop-ci.yml`
- Modify: `README.md`
- Modify: `python/tests/regression/test_reproducible_build_config.py`

- [ ] Add a separate `public-history-security` job with `actions/checkout@v4` and `fetch-depth: 0`, Python 3.12 setup, the focused security regression tests, and `python scripts/scan_public_history.py .` as blocking steps.
- [ ] Keep the existing five job names stable (`frontend`, `python-sidecar`, `tauri-rust`, `macos-arm64-build`, `windows-x64-build`) and add only `public-history-security`, producing an exact six-job matrix.
- [ ] Make the workflow regression test assert the six exact job IDs/names, full-history checkout, scanner command, compile gate, format gate, toolchain pin, `--locked`, and lockfile-diff checks.
- [ ] Update the README prerequisites and verification commands to state that Rust 1.98.0 is pinned, Cargo.lock is committed, all Cargo verification is locked, compileall and rustfmt are gates, and the public-history scanner requires a complete fetched history.
- [ ] Run README/link and workflow regression checks plus both scanner entry points.

Run: `python/.venv/bin/python -m pytest -q python/tests/regression/test_reproducible_build_config.py python/tests/regression/test_public_history_scan.py python/tests/regression/test_sensitive_scan.py`

Run: `python/.venv/bin/python scripts/scan_sensitive.py .`

Run: `python/.venv/bin/python scripts/scan_public_history.py .`

Expected: all tests and scanners PASS.

- [ ] Commit the CI and documentation layer after confirming the diff contains no product logic.

Commit: `ci: enforce reproducible migration verification`

---

### Task 4: Run fresh local verification and fast-forward the existing remote branch

**Files:**

- Verify only; no new product files.

- [ ] Confirm the isolated worktree is based on the current remote PR head and that the proposed update is a strict fast-forward of `origin/chore/complete-public-migration`.

Run: `git merge-base --is-ancestor origin/chore/complete-public-migration HEAD`

Expected: exit 0.

- [ ] Run all fresh local validation commands with the pinned toolchain and frozen dependency resolution.

Run: `pnpm install --frozen-lockfile`

Run: `pnpm --filter @qian-labor/desktop test`

Run: `python/.venv/bin/python -m pytest -q python/tests`

Run: `python/.venv/bin/python -m compileall -q python/src python/tests scripts`

Run: `cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all -- --check`

Run: `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked`

Run: `cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked`

Run: `git diff --exit-code -- apps/desktop/src-tauri/Cargo.lock`

Run: `python/.venv/bin/python scripts/scan_sensitive.py .`

Run: `python/.venv/bin/python scripts/scan_public_history.py .`

Run: `git diff --check origin/chore/complete-public-migration...HEAD`

Expected: every command exits 0; frontend and Python test counts are recorded; neither scanner reports a finding; the lockfile and working tree remain unchanged.

- [ ] Push only `HEAD:chore/complete-public-migration` without force, then fetch and prove the remote branch head and tree match the verified local commit.

Run: `git push origin HEAD:chore/complete-public-migration`

Expected: a non-force fast-forward update of the existing branch.

---

### Task 5: Validate six GitHub Actions jobs, record branch-rule status, and update Pull Request #1

**Files:**

- Remote metadata only: existing Pull Request #1 body and one validation comment.
- Branch rules only if the repository exposes a safe supported administration path after all six checks exist and pass.

- [ ] Monitor the workflow triggered by the new remote head until all six jobs complete. Inspect failed step logs before any correction; make only a scoped follow-up commit and rerun the affected local gates if a job fails.
- [ ] Confirm exact successful conclusions for `frontend`, `python-sidecar`, `tauri-rust`, `macos-arm64-build`, `windows-x64-build`, and `public-history-security`. Confirm the macOS and Windows package artifacts were produced by their real build steps.
- [ ] Keep `rust-toolchain.toml` only if the 1.98.0 pin succeeds on Linux, macOS ARM64, and Windows x64. If the pin itself proves unavailable on any runner, remove the pin, retain the committed lockfile and `--locked` policy, document `stable` as the verified CI policy, rerun all six jobs, and report the evidence rather than claiming a fixed version.
- [ ] Re-query `main` protection and repository rules after the six checks exist. Configure only this minimum safe policy if a supported administration path is available: require pull requests with zero approvals; require the six exact GitHub Actions checks and an up-to-date branch; disallow force pushes and deletions; keep an administrator recovery path; add no push restriction or unrelated rule.
- [ ] If protection cannot be safely configured because the integration lacks administration access or the platform/plan does not support it, leave repository settings untouched and record `BRANCH_PROTECTION=NOT_CONFIGURED` with the exact observed reason. Never claim a configured rule that was not read back successfully.
- [ ] Update the existing PR body to preserve its current purpose and add the new reproducibility, full-history scanner, compile, format, lockfile, toolchain, six-job CI, and branch-protection evidence. Do not create a new pull request.
- [ ] Add one concise PR validation comment containing the new head SHA, local command results, six job URLs/conclusions, scanner status, committed lockfile and toolchain status, and exact branch-protection status/reason.

---

### Task 6: Independent final review and completion evidence

**Files:**

- Review the complete change range from the original PR head through the final remote head; edit only if a confirmed issue requires a scoped fix.

- [ ] Dispatch an independent reviewer with the architecture spec, this plan, original PR head `c20bc7f14fc4a430adebef58fdb2ec4c554d06e5`, and final head. Ask specifically about credential leakage, Git-history coverage, output secrecy, exit contracts, lockfile enforcement, cross-platform workflow validity, source-trace/R01-R20 preservation, and accidental scope expansion.
- [ ] Resolve every critical or important review finding with a focused commit, rerun all affected local commands, push by fast-forward, and require a fresh successful six-job matrix.
- [ ] Re-fetch Pull Request #1, verify it remains open, Ready for Review, unmerged, and points at the verified final head.
- [ ] Report only the requested final evidence fields: PR URL, exact commits, local verification, six Actions conclusions, public-history scan status, lockfile/toolchain status, branch-protection status and reason, and blockers/remaining risks.

## Explicitly Out of Scope

- No changes to desktop UX, reports, R01-R20 business rules, evidence/source mappings, risk-score meaning, insufficient-data behavior, or human-review controls.
- No server, cloud database, authentication service, telemetry, online updater, real model/provider integration, real employee dataset, or live API request.
- No signing certificate, macOS notarization, Windows code signing, secret storage, release publishing, or installer distribution beyond the existing unsigned CI build artifacts.
- No history rewrite or deletion of existing refs; the scanner detects problems but does not remediate them destructively.
- No merge of Pull Request #1 and no direct update to `main`.
