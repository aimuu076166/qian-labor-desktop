# Sidecar Process Ownership Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task by task and `verification-before-completion` before claiming completion.

**Goal:** Remove bare-PID termination authority, make the desktop host own and clean the complete packaged backend process tree, rebuild every RC installer from the final PR head, independently verify the downloaded artifacts, and mark PR #2 Ready only after every required gate passes.

**Architecture:** The Python backend exposes a token-protected, idempotent internal shutdown route that requests Uvicorn shutdown and therefore runs the existing FastAPI lifespan cleanup. The Rust desktop host owns the spawned backend through an OS-level tree primitive established before backend code can create descendants: a dedicated process group on Unix and a kill-on-close Job Object with suspended process creation on Windows. READY-reported PIDs remain diagnostic evidence only. Shutdown first requests the authenticated endpoint, then waits for owned-process exit, and finally uses only the retained ownership primitive as a bounded fallback.

**Tech Stack:** Rust/Tauri, Python/FastAPI/Uvicorn, PyInstaller, pnpm/Vite, GitHub Actions, shell/PowerShell packaging smoke tests.

**Source specification:** User-provided RC process-ownership task book, plus `docs/superpowers/plans/2026-08-31-v0.1.0-rc-packaging-validation.md`.

## Global constraints

- Do not redesign the product or add unrelated functionality.
- Never use the READY-reported PID as termination authority.
- Never merge PR #2, create a tag, or publish a GitHub Release.
- Do not reuse artifacts or hashes from an earlier head.
- Preserve `/health` behavior and existing API token middleware behavior.
- Make stop idempotent and ensure a graceful-shutdown error cannot skip the owned fallback.
- Treat the packaged smoke result as passing only after backend cleanup completes.
- Keep dynamic CI/artifact facts in PR evidence and build manifests; keep only genuinely manual checks as `PENDING` in the checklist.

### Task 1: Lock the Python shutdown contract with failing tests

**Files:**

- Modify: `python/tests/desktop/test_auth.py`
- Modify: `python/tests/desktop/test_boot.py`
- Test: `python/tests/desktop/test_auth.py`
- Test: `python/tests/desktop/test_boot.py`

**Step 1: Add route authentication tests**

Add tests proving `POST /api/internal/shutdown` returns 401 without a token and with a malformed token, while the valid launch token returns a stable success payload.

**Step 2: Add idempotency and health-regression tests**

Call the shutdown route twice with the valid token and assert the callback is safe to repeat. Confirm `/health` stays public and unchanged.

**Step 3: Add entrypoint lifecycle tests**

Test the shutdown signal/controller separately from a real server thread: a repeated request sets one event, requests `server.should_exit`, and does not expose the launch token in output or errors.

**Step 4: Run the targeted tests and confirm RED**

Run:

```bash
python/.venv/bin/python -m pytest python/tests/desktop/test_auth.py python/tests/desktop/test_boot.py -q
```

Expected: new shutdown-route and entrypoint lifecycle assertions fail because the route/callback does not exist yet.

### Task 2: Implement graceful authenticated Python shutdown

**Files:**

- Modify: `python/src/qian_labor/desktop/app.py`
- Modify: `python/desktop_entrypoint.py`
- Test: `python/tests/desktop/test_auth.py`
- Test: `python/tests/desktop/test_boot.py`

**Step 1: Add a narrow shutdown callback contract**

Extend `create_desktop_app` with an optional no-argument shutdown callback. Register `POST /api/internal/shutdown` under the existing `/api/` token middleware and return a stable response after requesting shutdown.

**Step 2: Connect it to Uvicorn shutdown**

Create a `threading.Event` in `desktop_entrypoint.py`, pass its idempotent `set` callback into the app, and have the server-control loop set `server.should_exit = True`. Keep the route response independent from server-thread teardown timing.

**Step 3: Preserve lifespan cleanup**

Let Uvicorn exit normally so FastAPI lifespan finalization continues to shut down the processing queue and dispose the database.

**Step 4: Run targeted tests and confirm GREEN**

Run the same targeted pytest command and require all tests to pass.

### Task 3: Lock Rust shutdown ordering and idempotency with failing unit tests

**Files:**

- Modify: `apps/desktop/src-tauri/src/sidecar.rs`
- Test: `apps/desktop/src-tauri/src/sidecar.rs`

**Step 1: Introduce an injectable owned-process control seam in tests**

Define a small internal trait/helper covering owned-process status, bounded wait, and owned fallback cleanup. Keep arbitrary PID termination out of that interface.

**Step 2: Add unit tests before implementation**

Add tests proving:

- graceful request happens before fallback;
- graceful failure still invokes owned fallback;
- graceful timeout invokes owned fallback;
- a clean graceful exit skips destructive fallback;
- repeated state stop is stable and does not act twice;
- a READY-reported PID is diagnostic only;
- a simulated reused/unrelated READY PID is never targeted;
- startup error cleanup invokes the retained ownership primitive;
- one cleanup error is combined with, rather than hiding, the primary lifecycle error.

**Step 3: Run targeted Rust tests and confirm RED**

Run:

```bash
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml sidecar -- --nocapture
```

Expected: new ownership/shutdown tests fail until the coordinator and state behavior are implemented.

### Task 4: Implement cross-platform owned process-tree control

**Files:**

- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Modify: `apps/desktop/src-tauri/src/sidecar.rs`
- Optionally add: `apps/desktop/src-tauri/src/sidecar_process.rs`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Test: `apps/desktop/src-tauri/src/sidecar.rs`
- Test: platform-specific tests in the ownership module

**Step 1: Remove bare-PID termination**

Delete `terminate_ready_process` and every call path that uses `sidecar_pid` as authority. Retain the READY PID only in diagnostic metadata and smoke evidence.

**Step 2: Establish Unix ownership before exec**

Spawn the sidecar into a new process group before `exec`. Retain both the direct child handle and process-group identity. Send group signals only while the retained direct child/ownership state still proves the group belongs to this launch. Use TERM followed by a bounded wait and KILL only if needed.

**Step 3: Establish Windows ownership without a descendant race**

Create a kill-on-close Job Object, create the sidecar process suspended, assign it to the Job Object before it can create descendants, and only then resume the primary thread. Retain process and Job handles for status/wait/fallback cleanup. Close all handles on every failure path.

**Step 4: Add authenticated loopback graceful shutdown**

Send a bounded HTTP `POST /api/internal/shutdown` to `127.0.0.1:<ready-port>` with the launch token. Parse only the HTTP status, use stable redacted errors, and never log the token.

**Step 5: Coordinate shutdown and failure cleanup**

On normal stop: request graceful shutdown, wait for owned exit, then invoke the OS-owned fallback on failure or timeout. On startup timeout, invalid READY data, or early exit: invoke the ownership fallback and preserve both the primary and cleanup errors. Make `BackendState::stop` idempotent.

**Step 6: Add real process-tree tests**

On Unix, launch a synthetic parent that creates a descendant in its dedicated group, clean the owned group, and verify an unrelated process remains alive. On Windows CI, launch an equivalent parent/child tree in the Job Object and verify the Job cleanup removes both while an unrelated process remains alive.

**Step 7: Run Rust tests and static checks**

Run:

```bash
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --all-targets
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --all-targets
cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all -- --check
```

Expected: all pass; no code path terminates the READY PID.

### Task 5: Make packaged smoke success contingent on cleanup

**Files:**

- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/desktop/src-tauri/src/sidecar.rs`
- Modify: `scripts/smoke_packaged_app.py`
- Modify: `.github/workflows/desktop-rc.yml`
- Test: Rust unit tests and packaged smoke parser tests, if present

**Step 1: Stop before recording smoke success**

Have the packaged-smoke path call the idempotent backend stop and require it to succeed before writing a passing result and exiting the app.

**Step 2: Strengthen result evidence**

Add a stable `cleanup_complete` result field. Update `smoke_packaged_app.py` to require it and continue independently polling the diagnostic sidecar PID only to confirm absence, never to terminate it.

**Step 3: Add an abnormal lifecycle scenario**

Exercise forced app/backend interruption or startup failure in CI and require the owned tree to leave no backend residue. Keep test cleanup limited to the launched test app tree.

**Step 4: Run smoke-related tests**

Run targeted Python/Rust tests and require all success paths to include actual cleanup evidence.

### Task 6: Repair release checklist evidence routing

**Files:**

- Modify: `docs/release/v0.1.0-rc.1-checklist.md`
- Modify: `.github/workflows/desktop-rc.yml`
- Test: workflow/checklist inspection

**Step 1: Classify checklist items**

Label each item as an automated gate, manual gate, or out-of-scope item. Replace dynamic run/hash placeholders with `SEE_PR_EVIDENCE` or `SEE_BUILD_MANIFEST`.

**Step 2: Preserve only real manual pending items**

Keep `PENDING` solely where a human must perform a check that automation cannot establish. Remove stale fields that appear to contradict current CI evidence.

**Step 3: Confirm manifest contents**

Ensure the RC workflow emits final commit SHA, platform/architecture, filenames, byte sizes, and SHA-256 hashes for all installers and supporting bundles.

### Task 7: Run the full local verification matrix

**Files:**

- Verify only; modify code/tests only for failures caused by this change

**Step 1: Python setup and verification**

Run the task-book-required Python environment setup, compileall, sensitive-data scan, full pytest suite, desktop verification script, and real-provider smoke. Record real-provider smoke as `NOT_RUN` only when required credentials are unavailable, with no fabricated pass.

**Step 2: Frontend verification**

Run `pnpm install --frozen-lockfile`, frontend tests, lint, typecheck, and build.

**Step 3: Rust verification**

Run full Cargo tests/checks and formatting check.

**Step 4: Inspect the diff and secret surface**

Review `git diff`, tracked-file status, workflow permissions, generated files, and sensitive strings. Require a clean worktree after committing.

### Task 8: Commit and publish the exact final head

**Files:**

- Commit all intended source, tests, workflow, checklist, and plan changes

**Step 1: Create focused commits**

Commit the process-ownership implementation and its tests/evidence documentation with descriptive messages. Do not amend unrelated history.

**Step 2: Push only the PR branch**

Publish `release/v0.1.0-rc-packaging` without changing `main`, creating tags, or publishing releases. If direct Git transport remains unavailable, use the authenticated GitHub API while preserving the verified local tree and commit content.

**Step 3: Verify the remote head**

Confirm PR #2 still targets `main`, remains open and Draft, and its remote head tree exactly matches the locally verified tree before starting final CI evidence collection.

### Task 9: Monitor exact-head CI and independently verify fresh RC artifacts

**Files:**

- No repository changes unless a genuine failure requires a new fix/head

**Step 1: Wait for all required workflows**

Monitor every required check for the exact final PR head. Any new commit invalidates prior run and artifact evidence.

**Step 2: Require all RC platform jobs**

Require macOS app bundle/archive, macOS DMG, and Windows installer jobs to pass, including packaged-smoke cleanup and abnormal-lifecycle cleanup assertions.

**Step 3: Download artifacts independently**

Download the fresh artifacts produced from the final head into a temporary verification directory, not the repository.

**Step 4: Recompute and cross-check evidence**

Independently recompute SHA-256 and byte sizes for every deliverable. Cross-check filenames, platforms, architectures, commit SHA, sizes, and hashes against each build manifest. Inspect archive contents and installer naming conventions.

**Step 5: Update PR evidence**

Post a concise evidence record containing the exact head, workflow/run links or identifiers, job conclusions, artifact filenames, sizes/hashes, smoke results, abnormal cleanup result, and any explicitly manual remaining checks. Do not include secrets.

### Task 10: Apply the final PR gate without merging

**Files:**

- No repository files

**Step 1: Run verification-before-completion audit**

Re-check the exact head, clean local tree, all required CI conclusions, independent artifact hashes/manifests, PR base/head, and unresolved review conversations.

**Step 2: Convert PR #2 to Ready only if every gate passes**

If any automated gate, artifact verification, or required review condition is incomplete, leave the PR Draft and report the exact blocker. If all pass, convert only PR #2 from Draft to Ready for review.

**Step 3: Confirm forbidden actions did not occur**

Verify PR #2 is still open and unmerged, `main` was not changed by this task, and no tag or GitHub Release was created.
