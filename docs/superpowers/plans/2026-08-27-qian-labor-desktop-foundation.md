# Qian Labor Desktop Foundation Implementation Plan

> 公开说明：本文记录历史实施过程。当前仓库已经包含构建所需源码，构建与使用不依赖任何外部 Web 仓库。

**Goal:** Build the first working desktop vertical slice of 企安用工: a Tauri 2 + React desktop shell that starts a bundled/local Python sidecar, persists data to SQLite, imports synthetic files, runs the existing deterministic business pipeline with Fake Provider, shows a minimal Dashboard and finding detail, deletes the analysis, and survives restart without Docker, Redis, PostgreSQL, or a cloud server.

**Architecture:** The desktop shell uses React + TypeScript + Vite inside Tauri 2. Tauri owns window lifecycle, native file selection, app-data path resolution, sidecar startup/shutdown, and the per-launch IPC token. The Python sidecar keeps a localhost-only FastAPI envelope for the first desktop release, uses SQLite + LocalStorage, and reuses the deterministic business core now contained in this repository.

**Tech Stack:** Tauri 2, Rust, React 19, TypeScript, Vite, TanStack Query, FastAPI, Python 3.12, SQLAlchemy 2, SQLite, pytest, Vitest, Rust tests, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-27-qian-labor-desktop-architecture-design.md`

## Global Constraints

- The historical Web baseline was read-only during migration; this repository remains Desktop only.
- Preserve the locked historical business-core snapshot used by this plan.
- Preserve R01—R20 business semantics exactly during migration; do not redesign legal rules in this plan.
- Preserve `资料不足 ≠ 无风险` and all existing human-review gates.
- Desktop runtime must not require Docker, Caddy, PostgreSQL, Redis, RQ, a domain, or a cloud server.
- First target platforms: macOS arm64 and Windows x64.
- First slice uses only synthetic data and Fake Provider; no real employee data or real model key.
- Sidecar must listen only on `127.0.0.1` and require a random per-launch token for all business endpoints.
- API keys must never be written to React localStorage, SQLite, logs, Git, screenshots, or fixtures.
- One active analysis job at a time in the first slice.
- The original source files selected by the user must not be modified; copy working files into the application-private data directory.
- Do not rewrite the Python business core in Rust in this plan.

---

## Locked File Structure

Create this structure during the plan; do not invent parallel locations later.

```text
/
  AGENTS.md
  README.md
  package.json
  pnpm-workspace.yaml
  .gitignore
  .github/workflows/desktop-ci.yml

  apps/desktop/
    index.html
    package.json
    tsconfig.json
    vite.config.ts
    src/
      main.tsx
      App.tsx
      styles.css
      lib/
        api.ts
        desktop.ts
      features/
        import/ImportPanel.tsx
        processing/ProcessingPanel.tsx
        dashboard/DashboardView.tsx
        findings/FindingDetail.tsx
    tests/
      app.test.tsx
      import-panel.test.tsx
      dashboard.test.tsx
    src-tauri/
      Cargo.toml
      build.rs
      tauri.conf.json
      capabilities/default.json
      src/main.rs
      src/lib.rs
      src/sidecar.rs
      binaries/.gitkeep

  python/
    pyproject.toml
    desktop_entrypoint.py
    src/qian_labor/
      __init__.py
      database.py
      settings.py
      models/core.py
      storage/local.py
      parsers/__init__.py
      parsers/protocols.py
      parsers/registry.py
      security/uploads.py
      security/masking.py
      security/local_redaction.py
      ai/schemas.py
      ai/providers.py
      matching/
      rules/
      services/analyses.py
      services/uploads.py
      services/risk_evaluation.py
      services/deletion.py
      services/dashboard.py
      desktop/
        __init__.py
        app.py
        auth.py
        import_service.py
        queue.py
        schemas.py
    tests/
      desktop/test_boot.py
      desktop/test_auth.py
      desktop/test_import.py
      desktop/test_queue.py
      desktop/test_vertical_slice.py
      regression/

  fixtures/synthetic/
    README.md

  scripts/
    import_web_core.py
    build_sidecar.py
    verify_desktop.py
```

The executor may add package lockfiles or generated Tauri metadata required by the selected package manager, but may not move the responsibilities above into new top-level trees.

---

### Task 1: Repository governance and React/Vite desktop shell

**Files:**
- Modify: `README.md`
- Create: `AGENTS.md`
- Create: `package.json`
- Create: `pnpm-workspace.yaml`
- Create: `.gitignore`
- Create: `apps/desktop/package.json`
- Create: `apps/desktop/index.html`
- Create: `apps/desktop/tsconfig.json`
- Create: `apps/desktop/vite.config.ts`
- Create: `apps/desktop/src/main.tsx`
- Create: `apps/desktop/src/App.tsx`
- Create: `apps/desktop/src/styles.css`
- Create: `apps/desktop/tests/app.test.tsx`

**Interfaces:**
- Consumes: none.
- Produces: a Vite React app whose root component is `App`, with package scripts `dev`, `build`, `test`, `lint`, and `typecheck`.

- [ ] **Step 1: Write the failing React shell test**

Create `apps/desktop/tests/app.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '../src/App';

describe('App', () => {
  it('renders the desktop product identity without a web access-code gate', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: '企安用工' })).toBeInTheDocument();
    expect(screen.getByText('本地优先劳动用工风险体检')).toBeInTheDocument();
    expect(screen.queryByText('访问码')).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Create the workspace manifests and install dependencies**

Root `package.json` must contain:

```json
{
  "name": "qian-labor-desktop-workspace",
  "private": true,
  "packageManager": "pnpm@9.15.0",
  "scripts": {
    "test": "pnpm --dir apps/desktop test",
    "lint": "pnpm --dir apps/desktop lint",
    "typecheck": "pnpm --dir apps/desktop typecheck",
    "build:web": "pnpm --dir apps/desktop build"
  }
}
```

Root `pnpm-workspace.yaml`:

```yaml
packages:
  - apps/*
```

`apps/desktop/package.json` must use React 19 and include these dependencies by package name:

```text
react
react-dom
@tanstack/react-query
@tauri-apps/api
@tauri-apps/plugin-dialog
```

and these development dependencies:

```text
@tauri-apps/cli
@vitejs/plugin-react
vite
typescript
vitest
jsdom
@testing-library/react
@testing-library/jest-dom
eslint
```

Use current compatible Tauri 2 / Vite releases resolved by the package manager; do not mix Tauri 1 packages.

- [ ] **Step 3: Run the shell test and confirm it fails before implementation**

Run:

```bash
pnpm install
pnpm --dir apps/desktop test -- --run tests/app.test.tsx
```

Expected: FAIL because `src/App.tsx` does not yet export `App`.

- [ ] **Step 4: Implement the minimal desktop shell**

`apps/desktop/src/App.tsx`:

```tsx
export function App() {
  return (
    <main className="app-shell">
      <header>
        <p className="eyebrow">QIAN LABOR DESKTOP</p>
        <h1>企安用工</h1>
        <p>本地优先劳动用工风险体检</p>
      </header>
      <section aria-label="desktop-status">
        <p>正在准备本机分析服务…</p>
      </section>
    </main>
  );
}
```

`apps/desktop/src/main.tsx` must create a React root, wrap `App` in `QueryClientProvider`, and import `styles.css`.

- [ ] **Step 5: Run unit, type, and build checks**

Run:

```bash
pnpm --dir apps/desktop test -- --run
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop build
```

Expected: all PASS.

- [ ] **Step 6: Add repository rules for future agents**

`AGENTS.md` must state exactly these behavioral rules in prose:

```text
- This repository is Desktop only; do not modify the Web product from here.
- Read the approved desktop architecture spec and current implementation plan before coding.
- Use synthetic fixtures only in tests and screenshots.
- Preserve R01-R20 semantics and source traceability.
- No Docker/PostgreSQL/Redis/Caddy dependencies may be added to the desktop runtime.
- No real API key may be committed.
- Every feature change requires tests and a PR.
```

- [ ] **Step 7: Commit**

```bash
git add -- AGENTS.md README.md package.json pnpm-workspace.yaml .gitignore apps/desktop

git commit -m "feat: scaffold desktop React workspace"
```

---

### Task 2: Tauri 2 shell and native file-selection boundary

**Files:**
- Create: `apps/desktop/src-tauri/Cargo.toml`
- Create: `apps/desktop/src-tauri/build.rs`
- Create: `apps/desktop/src-tauri/tauri.conf.json`
- Create: `apps/desktop/src-tauri/capabilities/default.json`
- Create: `apps/desktop/src-tauri/src/main.rs`
- Create: `apps/desktop/src-tauri/src/lib.rs`
- Create: `apps/desktop/src/lib/desktop.ts`
- Create: `apps/desktop/features/import/ImportPanel.tsx`
- Test: `apps/desktop/tests/import-panel.test.tsx`

**Interfaces:**
- Consumes: Task 1 React app.
- Produces: `selectEmploymentFiles(): Promise<string[]>` in `src/lib/desktop.ts`; the selection comes from Tauri's native dialog and returns real file-system paths on Windows/macOS.

- [ ] **Step 1: Write the failing import-panel test**

Test behavior:

```tsx
it('shows one primary material-import action and the supported formats', () => {
  render(<ImportPanel onSelected={() => undefined} />);
  expect(screen.getByRole('button', { name: '选择企业材料' })).toBeInTheDocument();
  expect(screen.getByText(/Excel.*Word.*PDF.*图片/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Initialize Tauri 2 with Rust plugins**

`Cargo.toml` must include Tauri 2 plus these plugins:

```toml
[dependencies]
tauri = { version = "2", features = [] }
tauri-plugin-dialog = "2"
tauri-plugin-shell = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
rand = "0.9"
```

`src/lib.rs` must initialize `tauri_plugin_dialog::init()` and `tauri_plugin_shell::init()`.

`tauri.conf.json` must set:

```json
{
  "build": {
    "beforeDevCommand": "pnpm dev",
    "devUrl": "http://localhost:5173",
    "beforeBuildCommand": "pnpm build",
    "frontendDist": "../dist"
  },
  "productName": "企安用工",
  "identifier": "cn.qianlabor.desktop"
}
```

Do not configure an `externalBin` until Task 4, when a real sidecar binary name exists.

- [ ] **Step 3: Implement native file selection**

`apps/desktop/src/lib/desktop.ts`:

```ts
import { open } from '@tauri-apps/plugin-dialog';

const extensions = ['csv', 'xls', 'xlsx', 'docx', 'pdf', 'png', 'jpg', 'jpeg', 'webp'];

export async function selectEmploymentFiles(): Promise<string[]> {
  const selected = await open({
    multiple: true,
    directory: false,
    filters: [{ name: '企业劳动用工材料', extensions }],
  });
  if (!selected) return [];
  return Array.isArray(selected) ? selected : [selected];
}
```

- [ ] **Step 4: Implement `ImportPanel` and wire it into `App`**

The component must call `selectEmploymentFiles()` only after an explicit user click and call `onSelected(paths)` with the returned paths. It must never recursively scan a directory.

- [ ] **Step 5: Run frontend tests and Rust compile check**

```bash
pnpm --dir apps/desktop test -- --run
pnpm --dir apps/desktop typecheck
cd apps/desktop/src-tauri && cargo check
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -- apps/desktop/src-tauri apps/desktop/src/lib/desktop.ts apps/desktop/src/features/import apps/desktop/tests/import-panel.test.tsx apps/desktop/src/App.tsx

git commit -m "feat: add Tauri desktop shell and native import"
```

---

### Task 3: Python desktop package, SQLite persistence, and localhost token auth

**Files:**
- Create: `python/pyproject.toml`
- Create: `python/desktop_entrypoint.py`
- Create: `python/src/qian_labor/__init__.py`
- Create: `python/src/qian_labor/database.py`
- Create: `python/src/qian_labor/desktop/__init__.py`
- Create: `python/src/qian_labor/desktop/auth.py`
- Create: `python/src/qian_labor/desktop/app.py`
- Create: `python/src/qian_labor/desktop/schemas.py`
- Create: `python/tests/desktop/test_boot.py`
- Create: `python/tests/desktop/test_auth.py`

**Interfaces:**
- Consumes: environment variables `QIAN_DESKTOP_DATA_DIR`, `QIAN_DESKTOP_TOKEN`, optional `QIAN_DESKTOP_PORT=0`.
- Produces: a sidecar process that binds `127.0.0.1`, creates `<data-dir>/qian-labor.db`, prints exactly one machine-readable ready line `QIAN_DESKTOP_READY={...}`, exposes `/health` without auth, and requires header `X-Qian-Desktop-Token` on `/api/*`.

- [ ] **Step 1: Write failing boot and auth tests**

`test_boot.py` must assert `create_desktop_app()` creates a SQLite file under the provided temporary data directory and that `/health` returns:

```json
{"status":"ok","service":"qian-labor-desktop-sidecar"}
```

`test_auth.py` must assert:

```text
GET /api/status without header -> 401
GET /api/status with wrong token -> 401
GET /api/status with exact token -> 200
```

- [ ] **Step 2: Run tests and verify failure**

```bash
cd python
python -m pytest tests/desktop/test_boot.py tests/desktop/test_auth.py -q
```

Expected: FAIL because desktop app modules do not exist.

- [ ] **Step 3: Implement minimal SQLite `Database`**

Use the existing baseline behavior:

```python
class Database:
    def __init__(self, url: str, *, create_schema: bool = False) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        if url.startswith("sqlite"):
            event.listen(
                self.engine,
                "connect",
                lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
            )
```

The initial Task 3 schema may contain only a private `desktop_meta` table used to prove persistence. The full business models replace it in Task 5.

- [ ] **Step 4: Implement constant-time token middleware**

`desktop/auth.py` must compare the request header using `hmac.compare_digest`; no token value may appear in exception text or logs.

- [ ] **Step 5: Implement the localhost sidecar entrypoint**

`desktop_entrypoint.py` must:

```text
1. require QIAN_DESKTOP_DATA_DIR and QIAN_DESKTOP_TOKEN
2. create the data directory
3. choose port 0 / an ephemeral local port
4. bind only 127.0.0.1
5. print QIAN_DESKTOP_READY JSON containing host, port, and pid but not token
6. start uvicorn
```

The ready JSON shape is locked:

```json
{"host":"127.0.0.1","port":12345,"pid":1234}
```

- [ ] **Step 6: Run tests**

```bash
cd python
python -m pytest tests/desktop -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -- python

git commit -m "feat: add authenticated local Python sidecar"
```

---

### Task 4: Tauri-owned sidecar lifecycle and ephemeral IPC token

**Files:**
- Create: `apps/desktop/src-tauri/src/sidecar.rs`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Modify: `apps/desktop/src-tauri/tauri.conf.json`
- Modify: `apps/desktop/src-tauri/capabilities/default.json`
- Create: `apps/desktop/src/lib/api.ts`
- Create: `scripts/build_sidecar.py`
- Create: `apps/desktop/src-tauri/binaries/.gitkeep`

**Interfaces:**
- Consumes: Python Task 3 sidecar binary.
- Produces: Tauri commands `desktop_backend_info() -> { baseUrl, token }` and managed sidecar lifecycle. React API calls use `createDesktopApi(info)` and never persist the token.

- [ ] **Step 1: Add a Rust unit test for ready-line parsing**

Define:

```rust
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq)]
pub struct SidecarReady {
    pub host: String,
    pub port: u16,
    pub pid: u32,
}
```

Test parsing exactly:

```text
QIAN_DESKTOP_READY={"host":"127.0.0.1","port":43123,"pid":77}
```

and reject host values other than `127.0.0.1`.

- [ ] **Step 2: Build the Python sidecar with PyInstaller**

`scripts/build_sidecar.py` must execute PyInstaller for `python/desktop_entrypoint.py`, produce a binary named `qian-sidecar` (or `qian-sidecar.exe` on Windows), determine the Rust host tuple, and copy/rename it to:

```text
apps/desktop/src-tauri/binaries/qian-sidecar-<TARGET_TRIPLE>[.exe]
```

This naming follows the Tauri 2 external-binary contract.

- [ ] **Step 3: Configure Tauri external binary and permissions**

`tauri.conf.json` bundle section:

```json
{
  "externalBin": ["binaries/qian-sidecar"]
}
```

`capabilities/default.json` must grant only sidecar spawn permission for `binaries/qian-sidecar`; do not add broad arbitrary shell execution.

- [ ] **Step 4: Implement Rust sidecar startup**

Startup sequence in `sidecar.rs`:

```text
1. resolve app_data_dir
2. create qian-labor-desktop directory
3. generate a cryptographically random 32-byte token and encode it
4. spawn sidecar with QIAN_DESKTOP_DATA_DIR and QIAN_DESKTOP_TOKEN
5. read stdout until a valid READY line arrives
6. reject non-loopback host
7. store base URL, token, and child handle in managed state
```

Use Tauri shell sidecar APIs; do not run Python through a user shell command.

- [ ] **Step 5: Implement React API factory**

`src/lib/api.ts`:

```ts
export type DesktopBackendInfo = { baseUrl: string; token: string };

export function createDesktopApi(info: DesktopBackendInfo) {
  return async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${info.baseUrl}${path}`, {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        'X-Qian-Desktop-Token': info.token,
      },
    });
    if (!response.ok) throw new Error(`DESKTOP_API_${response.status}`);
    return response.json() as Promise<T>;
  };
}
```

Do not save `info.token` to localStorage/sessionStorage.

- [ ] **Step 6: Verify sidecar lifecycle locally**

Run:

```bash
python scripts/build_sidecar.py
cd apps/desktop/src-tauri && cargo test
pnpm --dir apps/desktop tauri dev
```

Expected: desktop opens only after sidecar health becomes available; closing the app terminates the child process.

- [ ] **Step 7: Commit**

```bash
git add -- apps/desktop/src-tauri apps/desktop/src/lib/api.ts scripts/build_sidecar.py

git commit -m "feat: manage desktop sidecar lifecycle"
```

---

### Task 5: Recover the locked business core without Redis/PostgreSQL coupling

**Files:**
- Create: `scripts/import_web_core.py`
- Create/replace under: `python/src/qian_labor/...` according to the locked file structure
- Create: `python/tests/regression/test_web_baseline_contract.py`
- Modify: `python/pyproject.toml`

**Interfaces:**
- Consumes: the locked historical business-core snapshot now contained in this repository.
- Produces: desktop Python package exposing the existing `Database`, models, parser registry, Fake provider, matching, R01—R20 rule catalog/evaluator, sources, and deletion service, while the desktop runtime has no `redis` or `rq` dependency.

- [ ] **Step 1: Write a migration contract test before copying code**

The test must assert:

```python
from qian_labor.rules.catalog import RULE_IDS


def test_desktop_starts_with_exact_web_r01_r20_catalog():
    assert len(RULE_IDS) == 20
    assert set(RULE_IDS) == {
        'CONTRACT_MISSING_ACTIVE',
        'CONTRACT_EXPIRING_30D',
        'CONTRACT_EXPIRED_STILL_ACTIVE',
        'CONTRACT_ENTITY_MISMATCH',
        'CONTRACT_TERM_MISSING_OR_UNREADABLE',
        'PROBATION_TERM_MISMATCH',
        'PROBATION_DUPLICATE_SUSPECT',
        'PROBATION_ENDING_NO_ASSESSMENT',
        'PAY_CONTRACT_ACTUAL_MISMATCH',
        'ACTIVE_NOT_IN_SOCIAL_INSURANCE',
        'SOCIAL_INSURANCE_ENTITY_MISMATCH',
        'SOCIAL_INSURANCE_WAIVER_LANGUAGE',
        'OVERTIME_WITHOUT_PAY_EVIDENCE',
        'ATTENDANCE_PAYROLL_MISMATCH',
        'ACTIVE_WITHOUT_ATTENDANCE_RECORD',
        'TERMINATED_STILL_PAID_OR_INSURED',
        'TERMINATION_MISSING_NOTICE_OR_DELIVERY',
        'TERMINATION_MISSING_SETTLEMENT_RECORD',
        'EMPLOYEE_IDENTITY_AMBIGUOUS',
        'MATERIAL_COVERAGE_LOW',
    }
```

Also assert `python/pyproject.toml` does not contain `redis` or `rq`.

- [ ] **Step 2: Implement a locked-source recovery script**

`import_web_core.py` is a historical recovery utility, not a current build dependency. When a maintainer supplies an explicit source checkout, it must verify the locked revision before copying and exit non-zero on a mismatch.

The recovery scope is limited to these business-core areas under `python/src/qian_labor/`:

```text
database.py
models/
storage/
parsers/
security/uploads.py
security/masking.py
security/local_redaction.py
ai/schemas.py
ai/providers.py
matching/
rules/
services/analyses.py
services/uploads.py
services/risk_evaluation.py
services/deletion.py
services/dashboard.py
```

Do not copy:

```text
jobs/queue.py
worker.py
access_session.py
Web FastAPI main.py
Caddy/Compose/deploy files
```

- [ ] **Step 3: Remove server-only package dependencies from the desktop pyproject**

The desktop Python dependencies for this phase may include:

```text
fastapi
uvicorn
httpx
pydantic-settings
sqlalchemy
openpyxl
pillow
pymupdf
python-docx
python-multipart
xlrd
charset-normalizer
tenacity
```

Do not include `psycopg`, `redis`, or `rq` in the desktop package.

- [ ] **Step 4: Restore selected business-core regression tests**

Copy/adapt tests for:

```text
parsers
upload validation
masking/local redaction
matching
risk evaluation R01-R20
deletion
```

Only change fixture paths and database URLs necessary for the new repository layout; do not weaken assertions to make them pass.

- [ ] **Step 5: Run regression tests on SQLite**

```bash
cd python
python -m pytest tests/regression -q
```

Expected: PASS with SQLite; no Redis or PostgreSQL service running.

- [ ] **Step 6: Commit**

```bash
git add -- python scripts/import_web_core.py

git commit -m "feat: migrate pinned labor-risk core to desktop"
```

---

### Task 6: Desktop file import service and single-worker processing queue

**Files:**
- Create: `python/src/qian_labor/desktop/import_service.py`
- Create: `python/src/qian_labor/desktop/queue.py`
- Extend: `python/src/qian_labor/desktop/app.py`
- Create: `python/tests/desktop/test_import.py`
- Create: `python/tests/desktop/test_queue.py`

**Interfaces:**
- Consumes: explicit user-selected paths from Tauri, application-private data directory, migrated `UploadService`, `ProcessingPipeline`, `FakeAIProvider`.
- Produces endpoints:
  - `POST /api/analyses` -> new analysis
  - `POST /api/analyses/{id}/import-paths` with `{ "paths": ["..."] }`
  - `POST /api/analyses/{id}/process` -> 202 quickly
  - `GET /api/analyses/{id}/processing` -> status/progress

- [ ] **Step 1: Write import tests**

Tests must prove:

```text
- a selected source file is copied into the app-private analysis directory
- the original source bytes remain unchanged
- unsupported extensions are rejected
- a path not supplied in the explicit request is never scanned/imported
```

- [ ] **Step 2: Write queue tests**

Tests must prove:

```text
- POST process returns before the full pipeline completes
- only one active analysis job can run at a time
- a second concurrent start returns conflict code DESKTOP_ANALYSIS_BUSY
- the executor uses max_workers=1
- status can be polled until completed/partial/matching_review
```

- [ ] **Step 3: Implement `DesktopImportService`**

Signature:

```python
class DesktopImportService:
    def __init__(self, database: Database, data_dir: Path) -> None: ...

    def import_paths(self, analysis_id: str, paths: list[Path]) -> list[UploadedFile]: ...
```

For each path:

```text
read bytes
validate_upload(filename, MIME, content)
copy via existing UploadService/LocalStorage into app-private storage
never write to the original path
```

- [ ] **Step 4: Implement `DesktopProcessingQueue`**

Signature:

```python
class DesktopProcessingQueue:
    def __init__(self, pipeline_factory: Callable[[], ProcessingPipeline]) -> None: ...
    def submit(self, analysis_id: str) -> dict[str, object]: ...
    def shutdown(self) -> None: ...
```

Use `ThreadPoolExecutor(max_workers=1)` and one guarded active future.

- [ ] **Step 5: Run tests**

```bash
cd python
python -m pytest tests/desktop/test_import.py tests/desktop/test_queue.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -- python/src/qian_labor/desktop python/tests/desktop

git commit -m "feat: add local import and desktop processing queue"
```

---

### Task 7: Synthetic vertical slice API and persisted dashboard data

**Files:**
- Create: `fixtures/synthetic/README.md`
- Create: `python/tests/desktop/test_vertical_slice.py`
- Extend: `python/src/qian_labor/desktop/app.py`
- Create: `python/src/qian_labor/desktop/schemas.py`
- Create: `scripts/verify_desktop.py`

**Interfaces:**
- Consumes: migrated business core + Task 6 local import/queue.
- Produces API responses for a minimal Dashboard and finding detail that match existing product semantics.

- [ ] **Step 1: Create synthetic fixture-generation contract**

The vertical test may reuse the synthetic fixture generator logic already present in this repository, but the generated material must be created at runtime in a temporary directory and marked fictional. No binary employee fixture should be committed if it contains realistic personal identifiers.

- [ ] **Step 2: Write the end-to-end Python-side vertical test**

The test must perform:

```text
create analysis
→ generate/import synthetic CSV/DOCX/PDF/image inputs
→ start Fake Provider processing
→ poll to terminal state
→ fetch dashboard
→ assert at least one risk/data-quality finding exists
→ fetch one finding and assert source locator exists
→ delete analysis
→ assert source/content endpoints no longer return personal analysis data
→ recreate app against same SQLite file and assert deleted state remains deleted
```

- [ ] **Step 3: Add minimal desktop API schemas**

Lock response shapes needed by React:

```python
class DashboardSummary(BaseModel):
    analysis_id: str
    status: str
    employee_count: int
    finding_count: int
    high_count: int
    medium_count: int
    insufficient_data_count: int

class FindingSummary(BaseModel):
    id: str
    rule_id: str
    title: str
    severity: str
    assessment_status: str
    requires_human_review: bool
```

Do not expose internal raw ORM objects directly.

- [ ] **Step 4: Implement `scripts/verify_desktop.py`**

It must start the sidecar in a temporary data directory, use a random token, execute the vertical API sequence with synthetic data, and print only:

```text
SIDECAR_BOOT=PASS
SQLITE_PERSISTENCE=PASS
SYNTHETIC_IMPORT=PASS
FAKE_PROVIDER_PIPELINE=PASS
R01_R20_REGRESSION=PASS
SOURCE_TRACE=PASS
DELETE_CLEANUP=PASS
```

No document body, token, identifier, or API key may be printed.

- [ ] **Step 5: Run the vertical verification**

```bash
cd python && python -m pytest tests/desktop/test_vertical_slice.py -q
cd .. && python scripts/verify_desktop.py
```

Expected: all PASS lines.

- [ ] **Step 6: Commit**

```bash
git add -- fixtures/synthetic python scripts/verify_desktop.py

git commit -m "test: prove desktop synthetic analysis vertical slice"
```

---

### Task 8: React desktop vertical UI

**Files:**
- Modify: `apps/desktop/src/App.tsx`
- Modify: `apps/desktop/src/lib/api.ts`
- Modify: `apps/desktop/src/features/import/ImportPanel.tsx`
- Create: `apps/desktop/src/features/processing/ProcessingPanel.tsx`
- Create: `apps/desktop/src/features/dashboard/DashboardView.tsx`
- Create: `apps/desktop/src/features/findings/FindingDetail.tsx`
- Create: `apps/desktop/tests/dashboard.test.tsx`
- Modify: `apps/desktop/tests/app.test.tsx`

**Interfaces:**
- Consumes: Tauri backend info + Task 7 sidecar API.
- Produces: one user-visible path from material selection through processing to dashboard and finding detail.

- [ ] **Step 1: Write UI tests for product semantics**

Tests must assert:

```text
- one primary material import button
- processing status shown in Chinese business terms
- dashboard distinguishes suspected risk from insufficient data
- insufficient data never renders as “无风险”
- a high-impact finding displays “需要人工复核”
- finding detail exposes source file/location text
```

- [ ] **Step 2: Implement a small desktop state machine in `App`**

Use these UI states only:

```ts
type DesktopView =
  | { kind: 'booting' }
  | { kind: 'import' }
  | { kind: 'processing'; analysisId: string }
  | { kind: 'dashboard'; analysisId: string }
  | { kind: 'finding'; analysisId: string; findingId: string }
  | { kind: 'error'; code: string };
```

Do not add routing libraries unless the implementation demonstrably needs them for this first slice.

- [ ] **Step 3: Implement processing polling with TanStack Query**

Poll only while status is non-terminal; stop polling on:

```text
completed
partial
matching_review
failed
```

- [ ] **Step 4: Implement minimal Dashboard cards and finding list**

The first slice does not need the entire Web visual polish. It must clearly show:

```text
高风险
中风险
资料不足
员工数
发现数
```

and a clickable finding list.

- [ ] **Step 5: Implement finding detail**

Show:

```text
风险名称
风险等级
判断状态
是否需要人工复核
规则 ID
来源文件
页码/行号/单元格/摘录（when available）
```

- [ ] **Step 6: Run frontend tests/build**

```bash
pnpm --dir apps/desktop test -- --run
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop build
```

Expected: PASS.

- [ ] **Step 7: Manual dev verification**

```bash
python scripts/build_sidecar.py
pnpm --dir apps/desktop tauri dev
```

Using only synthetic materials, verify:

```text
select → import → process → dashboard → finding detail → delete
```

- [ ] **Step 8: Commit**

```bash
git add -- apps/desktop

git commit -m "feat: add desktop analysis vertical UI"
```

---

### Task 9: Cross-platform CI and release-blocking verification

**Files:**
- Create: `.github/workflows/desktop-ci.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: CI evidence that the code tests on Linux and compiles/builds the desktop application on macOS arm64-capable runner and Windows x64 runner without Docker services.

- [ ] **Step 1: Add a Linux core-test job**

The job must run:

```text
pnpm install --frozen-lockfile
frontend unit tests
frontend typecheck
Python pytest
scripts/verify_desktop.py
secret scan / grep policy for committed keys
```

It must not start PostgreSQL or Redis services.

- [ ] **Step 2: Add macOS build smoke**

On a GitHub macOS runner:

```text
install Python 3.12
install Rust stable (>=1.84 recommended so host-tuple is available)
install Node 22 + pnpm 9.15.0
install Python desktop package and PyInstaller
build sidecar
cargo test
pnpm tauri build --debug (or equivalent non-signing build smoke)
```

Expected status marker:

```text
MACOS_ARM64_BUILD=PASS
```

If GitHub's selected hosted runner architecture is not arm64, do not falsely label it arm64; either select an arm64 runner or report the architecture explicitly and keep the arm64 criterion open.

- [ ] **Step 3: Add Windows x64 build smoke**

Run the equivalent sidecar + Tauri debug build on `windows-latest`; assert the host tuple begins `x86_64-pc-windows` before reporting:

```text
WINDOWS_X64_BUILD=PASS
```

- [ ] **Step 4: Add README run instructions**

README must clearly distinguish:

```text
Developer mode: requires Node/Rust/Python
Packaged end user: must not require Docker/PostgreSQL/Redis/Python installation
```

and state that real model support is not part of this foundation plan.

- [ ] **Step 5: Run all available checks and commit**

```bash
pnpm test
pnpm lint
pnpm typecheck
cd python && python -m pytest -q
cd .. && python scripts/verify_desktop.py
cd apps/desktop/src-tauri && cargo test
```

Expected: PASS before opening the implementation PR.

```bash
git add -- .github/workflows/desktop-ci.yml README.md

git commit -m "ci: validate desktop foundation on macOS and Windows"
```

---

## Plan Completion Gate

This plan is complete only when the implementation PR can truthfully report:

```text
SIDECAR_BOOT=PASS
LOCALHOST_ONLY=PASS
IPC_TOKEN_AUTH=PASS
SQLITE_PERSISTENCE=PASS
SYNTHETIC_IMPORT=PASS
FAKE_PROVIDER_PIPELINE=PASS
R01_R20_REGRESSION=PASS
DASHBOARD=PASS
SOURCE_TRACE=PASS
DELETE_CLEANUP=PASS
NO_DOCKER_REQUIRED=true
NO_POSTGRES_REQUIRED=true
NO_REDIS_REQUIRED=true
NO_CLOUD_SERVER_REQUIRED=true
MACOS_BUILD=<PASS or explicitly open with runner architecture reason>
WINDOWS_X64_BUILD=PASS
REAL_PROVIDER_SMOKE=NOT_IN_SCOPE
```

Do not begin GLM provider work, production API-key persistence, auto-update, signing/notarization, or full Web UI parity until this gate is green or maintainers document a separate priority change.

## Self-Review Result

- Spec coverage: foundation, sidecar lifecycle, localhost token, SQLite, local file import, removal of Redis/RQ, locked business-core migration, Fake Provider, R01—R20, source traceability, deletion, first UI path, macOS/Windows build smoke are covered.
- Intentionally deferred to later plans: GLM real provider, OS credential-store persistence, bundled Tesseract/OCR cross-platform packaging, complete matching-review UI parity, complete report UI parity, updater, code signing/notarization, backup/import format.
- Placeholder scan: no TODO/TBD placeholders are used as implementation instructions.
- Type/interface consistency: `DesktopBackendInfo`, `SidecarReady`, `DesktopImportService`, `DesktopProcessingQueue`, API routes, and UI states are defined before downstream use.
