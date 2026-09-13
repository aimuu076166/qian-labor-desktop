# Local Private Provider Secret Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove macOS Keychain access and persist the Zhipu API Key and PII pepper in owner-only application data files without changing the analysis workflow.

**Architecture:** Replace the production `SystemSecretStore` with a data-directory-scoped `LocalSecretStore` that validates regular files, writes atomically, and enforces Unix mode `0600`. Keep the existing `SecretStore` interface and in-memory test double so Provider validation and sidecar environment boundaries remain unchanged.

**Tech Stack:** Rust/Tauri 2, Unix file permissions, React/TypeScript, Vitest, GitHub Actions macOS arm64 packaging.

---

### Task 1: Add failing local-secret-store tests

**Files:**
- Modify: `apps/desktop/src-tauri/src/credentials.rs`

- [ ] Add a Unix test that constructs `LocalSecretStore::new(&directory)`, stores `synthetic-test-key-value`, reads it back, and asserts `metadata.permissions().mode() & 0o777 == 0o600`.
- [ ] Add a Unix test that places a symlink at `zhipu-api-key.secret` and asserts `get(API_KEY_ACCOUNT)` returns `DESKTOP_CREDENTIAL_READ_FAILED`.
- [ ] Run `cargo test --locked local_secret_store -- --nocapture` and confirm compilation/test failure because `LocalSecretStore` does not exist.

### Task 2: Replace Keychain storage with owner-only files

**Files:**
- Modify: `apps/desktop/src-tauri/src/credentials.rs`
- Modify: `apps/desktop/src-tauri/src/sidecar.rs`
- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Modify: `apps/desktop/src-tauri/Cargo.lock`

- [ ] Implement `LocalSecretStore { data_dir: PathBuf }` with account-to-file mapping for `zhipu-api-key.secret` and `pii-hash-pepper.secret`.
- [ ] Implement `get` using `symlink_metadata`, rejecting symlinks, non-files, files larger than 4096 bytes, and Unix permissions with group/other bits set.
- [ ] Implement `set` with a randomly named same-directory temporary file, Unix creation mode `0600`, `sync_all`, explicit `0600` permissions, and atomic `rename`.
- [ ] Instantiate `LocalSecretStore::new(&data_dir)` in `provider_configuration_status`, `configure_zhipu_provider`, `mark_zhipu_provider_validated`, and `start_backend`.
- [ ] Remove the direct `security-framework` dependency and its now-unused lockfile entries.
- [ ] Run `cargo test --locked local_secret_store -- --nocapture`; expect both new tests to pass.
- [ ] Run `cargo test --locked`, `cargo check --locked`, and `cargo fmt --all --check`; expect all to pass.
- [ ] Commit as `fix: replace keychain with local secret files`.

### Task 3: Remove Keychain product claims

**Files:**
- Modify: `apps/desktop/src/features/settings/SettingsView.tsx`
- Modify: `apps/desktop/tests/settings.test.tsx`
- Modify: `apps/desktop/tests/app.test.tsx`
- Modify: `README.md`
- Modify: `docs/release/v0.1.0-rc.1-checklist.md`

- [ ] Change settings copy to `API Key 仅保存在本机当前用户的应用私有目录。连接测试不发送企业或员工材料。`.
- [ ] Update the focused UI assertion to require the new copy and reject `Keychain`/`钥匙串`.
- [ ] Rename the app test to describe locally persisted Zhipu configuration.
- [ ] Replace current README and RC-checklist Keychain statements with owner-only local secret-file statements and the explicit security trade-off.
- [ ] Run `pnpm --dir apps/desktop test -- --run`, lint, typecheck, and build; expect all to pass.
- [ ] Run `rg -n 'macOS Keychain|Keychain|钥匙串' apps/desktop README.md docs/release` and expect no current-product matches.
- [ ] Commit as `docs: describe local provider secret storage`.

### Task 4: Full verification and final RC

**Files:**
- Modify: PR #2 body only after verification.

- [ ] Run the complete Python suite, sensitive scan, public-history scan, frontend suite, and Rust suite on clean GitHub runners.
- [ ] Require final `desktop-ci` and `desktop-rc` runs to conclude `success` for the same exact head.
- [ ] Download the macOS arm64 artifact, verify the outer digest, `unzip -t`, internal SHA-256 files, and manifest commit.
- [ ] Mount the DMG read-only and run bundle structure, architecture, version, payload security, code-signature, packaged-app, LaunchServices, and abnormal-lifecycle checks.
- [ ] Update PR #2 with exact head, CI links, artifact link, hashes, and the remaining real-Provider gate.
- [ ] Keep PR #2 Draft and unmerged until the user completes a synthetic real-Provider analysis.
