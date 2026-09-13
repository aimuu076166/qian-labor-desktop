# GLM-5.3-Flash Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `glm-5.3-flash` the only model used by the desktop Zhipu provider for both text and image analysis, including legacy configuration handling.

**Architecture:** Keep the existing frontend IPC shape and two internal model fields, but make both fields canonical and non-editable. Enforce the same constant in Rust before persistence or sidecar injection and in Python provider fallbacks; legacy configurations become unvalidated until saved and tested again.

**Tech Stack:** React 19/TypeScript/Vitest, Rust/Tauri 2, Python/pytest/httpx, GitHub Actions macOS packaging.

---

### Task 1: Lock the settings UI to one multimodal model

**Files:**
- Modify: `apps/desktop/tests/settings.test.tsx`
- Modify: `apps/desktop/src/features/settings/SettingsView.tsx`

- [ ] **Step 1: Write the failing frontend test**

Replace the editable-model expectation with assertions that only a read-only `分析模型` field exists and that saving submits the canonical value twice:

```tsx
expect(screen.getByLabelText('分析模型')).toHaveValue('glm-5.3-flash');
expect(screen.queryByLabelText('文本模型')).not.toBeInTheDocument();
expect(screen.queryByLabelText('视觉模型')).not.toBeInTheDocument();
expect(onSave).toHaveBeenCalledWith({
  apiKey: 'synthetic-ui-key-value',
  textModel: 'glm-5.3-flash',
  visionModel: 'glm-5.3-flash',
  baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
});
```

- [ ] **Step 2: Verify RED**

Run: `pnpm --dir apps/desktop test --run tests/settings.test.tsx`

Expected: FAIL because the current screen has editable `文本模型` and `视觉模型` fields.

- [ ] **Step 3: Implement the fixed display and payload**

Define `const ZHIPU_MODEL = 'glm-5.3-flash';`, remove model state/effects, render one read-only input, and submit:

```tsx
await onSave({
  apiKey,
  textModel: ZHIPU_MODEL,
  visionModel: ZHIPU_MODEL,
  baseUrl: status.baseUrl,
});
```

- [ ] **Step 4: Verify GREEN**

Run: `pnpm --dir apps/desktop test --run tests/settings.test.tsx`

Expected: PASS.

### Task 2: Enforce the canonical model in Rust and invalidate legacy validation

**Files:**
- Modify: `apps/desktop/src-tauri/src/credentials.rs`

- [ ] **Step 1: Write failing Rust tests**

Change the test helper to submit `glm-5.3-flash`, assert both injected environment values equal it, add a rejection test for any other model, and write a legacy JSON fixture whose old model values and `validated: true` return canonical status with `validated: false`.

```rust
assert_eq!(status.text_model, ZHIPU_MODEL);
assert_eq!(status.vision_model, ZHIPU_MODEL);
assert!(!status.validated);
assert_eq!(provider_environment(&store, &directory).unwrap(), Vec::new());
```

- [ ] **Step 2: Verify RED**

Run: `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml credentials`

Expected: FAIL because arbitrary model names are accepted and legacy validation is retained.

- [ ] **Step 3: Implement canonical validation and legacy normalization**

Add `const ZHIPU_MODEL: &str = "glm-5.3-flash";`. Require both submitted fields to equal it. In `provider_status`, expose canonical model values and set `validated` only when the stored provider, URL, both models, Key and pepper are current. In `provider_environment`, return an empty environment for legacy configuration; for current configuration inject `ZHIPU_MODEL` into both model variables.

- [ ] **Step 4: Verify GREEN**

Run: `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml credentials`

Expected: PASS.

### Task 3: Unify Python provider defaults

**Files:**
- Modify: `python/tests/regression/test_zhipu_provider.py`
- Modify: `python/tests/desktop/test_auth.py`
- Modify: `python/src/qian_labor/ai/provider_factory.py`

- [ ] **Step 1: Change expectations first**

Make both fallback tests require:

```python
assert provider.text_model == "glm-5.3-flash"
assert provider.vision_model == "glm-5.3-flash"
```

- [ ] **Step 2: Verify RED**

Run: `pytest -q python/tests/regression/test_zhipu_provider.py::test_provider_factory_defaults_zhipu_to_glm_5_3_flash_and_official_base python/tests/desktop/test_auth.py::test_desktop_runtime_uses_configured_zhipu_provider_and_call_limit`

Expected: FAIL with the old `glm-5.2`/`glm-4.6v` values.

- [ ] **Step 3: Implement one Python default**

Replace the two Zhipu defaults with:

```python
ZHIPU_DEFAULT_MODEL = "glm-5.3-flash"
text_model = settings.ai_text_model.strip() or ZHIPU_DEFAULT_MODEL
vision_model = settings.ai_vision_model.strip() or ZHIPU_DEFAULT_MODEL
```

- [ ] **Step 4: Verify GREEN**

Repeat the focused pytest command and expect both tests to PASS.

### Task 4: Update public documentation and run all gates

**Files:**
- Modify: `README.md`
- Modify: `docs/release/v0.1.0-rc.1-checklist.md` only if it names the old models

- [ ] **Step 1: Update the documented product contract**

State that the desktop application fixes both text and image analysis to `glm-5.3-flash`, and that the model is official native multimodal. Remove the obsolete claim that users can edit separate model names. Preserve the explicit `IMAGE_INPUT=NOT_RUN` release status until a real-image smoke is actually run.

- [ ] **Step 2: Scan production surfaces**

Run: `rg -n 'glm-5\.2|glm-4\.6v' README.md apps python scripts docs/release`

Expected: only intentional legacy-migration test fixtures may remain.

- [ ] **Step 3: Run complete verification**

Run:

```bash
pytest -q
pnpm --dir apps/desktop test --run
pnpm --dir apps/desktop lint
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop build
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml
cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml -- --check
python scripts/scan_sensitive.py
python scripts/scan_public_history.py
```

Expected: all repository-controlled gates PASS. Any missing local tool is recorded and its matching clean GitHub runner gate must PASS before completion.

- [ ] **Step 4: Commit, push, and rebuild exact-head RC**

Commit the implementation with `fix: unify zhipu model on glm-5.3-flash`, push `release/v0.1.0-rc-packaging`, monitor `desktop-ci` and `desktop-rc`, download the resulting artifact, verify manifest/checksums/bundle/architecture/signature, and repeat packaged app lifecycle smoke on the downloaded DMG.

- [ ] **Step 5: Preserve the PR gate**

Update PR #2 with the exact head and artifact evidence. Keep it Draft and unmerged until the user completes a synthetic real-provider smoke using their own Key.
