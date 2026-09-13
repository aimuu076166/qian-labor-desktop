import { useEffect, useRef, useState } from 'react';
import { readJson, safeErrorCode } from '../../lib/api';
import type { DesktopRequest } from '../workspace/useCompanyWorkspace';
import type { AnalysisBinding } from '../workspace/useHistoryWorkspace';
import type { WorkspacePayload } from '../workspace/MaterialWorkspace';

export type TaskRun = { id: string; analysis_id: string; company_id: string | null; parent_run_id: string | null;
  state: 'queued' | 'running' | 'cancel_requested' | 'cancelled' | 'interrupted' | 'completed' | 'partial' | 'failed';
  version: number; in_flight: boolean; created_at: string; completed_at: string | null };
export type TaskMetadata = { analysis_id: string; company_id: string | null; business_status: string;
  run: TaskRun | null; read_only: boolean; history: TaskRun[]; limit: number; offset: number;
  resume_preview: { reusable_file_ids: string[]; extraction_file_ids: string[] }; usage_notice: string };
type Operation = 'start' | 'resume' | 'cancel';
type Command = { request_id: string; company_id: string | null; expected_run_id: string | null; expected_version: number | null };
type Pending = { analysisId: string; operation: Operation; body: Command };
type Attempt = { source: DesktopRequest; generation: number };
type Envelope = { analysis_id: string; company_id: string | null; outcome: 'unknown' | 'pending' | 'accepted' | 'rejected';
  request: null | { id: string; analysis_id: string; company_id: string | null; run_id: string | null;
    operation: Operation; expected_run_id: string | null; expected_version: number | null;
    outcome: string; error_code: string | null }; run: TaskRun | null };
export type TaskTarget = { analysisId: string; ownerId: string | null; uiCompanyId: string; legacy: boolean };
type State = { api: DesktopRequest | null; data?: TaskMetadata; error?: string; pending?: Pending;
  busy?: boolean; needsRead?: boolean; preview?: TaskMetadata; previewFiles?: WorkspacePayload['files']; confirmation?: 'resume' | 'retry' };
const ACTIVE = new Set(['queued', 'running', 'cancel_requested']);
const RECOVERABLE = new Set(['cancelled', 'interrupted', 'partial', 'failed']);
const keyOf = (target: TaskTarget) => `${target.ownerId ?? 'unbound'}/${target.analysisId}`;
const storageKey = (key: string) => `qian-task-request-v1:${key}`;
const sameRequest = (a: Pending | undefined, b: Pending) => a?.analysisId === b.analysisId && a.operation === b.operation
  && a.body.request_id === b.body.request_id && a.body.company_id === b.body.company_id
  && a.body.expected_run_id === b.body.expected_run_id && a.body.expected_version === b.body.expected_version;

// Only command identity/CAS is retained. No token, path, file content, or provider configuration.
function restored(target: TaskTarget): Pending | undefined {
  try {
    const item = JSON.parse(localStorage.getItem(storageKey(keyOf(target))) ?? 'null') as Pending | null;
    if (item?.analysisId === target.analysisId && item.body.company_id === target.ownerId
      && ['start', 'cancel', 'resume'].includes(item.operation) && typeof item.body.request_id === 'string'
      && /^[0-9a-f-]{36}$/i.test(item.body.request_id)) return item;
  } catch { /* Storage is checked again before any new POST. */ }
}

export function useAnalysisTask(api: DesktopRequest | null, target: TaskTarget | null) {
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const states = useRef(new Map<string, State>());
  const submissions = useRef(new Set<string>());
  const reads = useRef(new Map<string, number>());
  const attempts = useRef(new Map<string, Attempt>());
  const reconciliations = useRef(new Map<string, Attempt>());
  const [revision, render] = useState(0);
  const live = useRef({ api, target }); live.current = { api, target };
  const key = target ? keyOf(target) : '';
  const current = key ? states.current.get(key) : undefined;
  const state: State = current?.api === api ? current : { api, pending: target ? restored(target) : undefined };
  const update = (scope: TaskTarget, source: DesktopRequest, patch: Partial<State>) => {
    if (!mounted.current || live.current.api !== source) return;
    const id = keyOf(scope);
    const old = states.current.get(id);
    states.current.set(id, { ...(old?.api === source ? old : { pending: restored(scope) }), api: source, ...patch });
    render(n => n + 1);
  };
  function scoped(value: { analysis_id: string; company_id: string | null }, scope: TaskTarget) {
    if (value.analysis_id !== scope.analysisId || value.company_id !== scope.ownerId) throw new Error('TASK_SCOPE_INVALID');
  }
  function begin(scope: TaskTarget, source: DesktopRequest) {
    const id = keyOf(scope), attempt = { source, generation: (attempts.current.get(id)?.generation ?? 0) + 1 };
    attempts.current.set(id, attempt);
    return attempt;
  }
  function owns(scope: TaskTarget, attempt: Attempt, pending?: Pending) {
    return mounted.current && live.current.api === attempt.source && attempts.current.get(keyOf(scope)) === attempt
      && (!pending || sameRequest(states.current.get(keyOf(scope))?.pending, pending) && sameRequest(restored(scope), pending));
  }
  async function read(scope = target, source = api, explicit = true) {
    if (!scope || !source) return;
    const id = keyOf(scope), generation = (reads.current.get(id) ?? 0) + 1;
    reads.current.set(id, generation);
    try {
      const data = await readJson<TaskMetadata>(source(`/api/analyses/${scope.analysisId}/task?${new URLSearchParams(scope.ownerId ? { company_id: scope.ownerId } : {})}`));
      scoped(data, scope); if (data.run) scoped(data.run, scope);
      if (!data.resume_preview || !Array.isArray(data.history)) throw new Error('TASK_RESPONSE_INVALID');
      if (reads.current.get(id) === generation && mounted.current && live.current.api === source) {
        update(scope, source, { data, error: undefined, ...(explicit ? { needsRead: false, preview: undefined, confirmation: undefined } : {}) });
        return data;
      }
    } catch (cause) { if (reads.current.get(id) === generation) update(scope, source, { error: safeErrorCode(cause), needsRead: true }); }
  }
  function settle(value: Envelope, pending: Pending, scope: TaskTarget, attempt: Attempt): boolean {
    if (!owns(scope, attempt, pending)) return false;
    scoped(value, scope);
    const receipt = value.request;
    if (!receipt) return false;
    scoped(receipt, scope);
    if (receipt.id !== pending.body.request_id || receipt.operation !== pending.operation
      || receipt.expected_run_id !== pending.body.expected_run_id || receipt.expected_version !== pending.body.expected_version
      || receipt.outcome !== value.outcome) throw new Error('TASK_RESPONSE_INVALID');
    if (value.run) { scoped(value.run, scope); if (value.run.id !== receipt.run_id) throw new Error('TASK_RESPONSE_INVALID'); }
    if (!['accepted', 'rejected'].includes(value.outcome)) return false;
    localStorage.removeItem(storageKey(keyOf(scope)));
    update(scope, attempt.source, { pending: undefined, preview: undefined, confirmation: undefined,
      error: value.outcome === 'rejected' ? receipt.error_code ?? 'TASK_SUBMISSION_FAILED' : undefined,
      needsRead: value.outcome === 'rejected' });
    return true;
  }
  async function reconcile(scope = target, source = api) {
    if (!scope || !source) return;
    const old = states.current.get(keyOf(scope));
    const pending = old?.pending ?? restored(scope);
    if (!pending || submissions.current.has(keyOf(scope)) || reconciliations.current.get(keyOf(scope))?.source === source || old?.api === source && old.busy) return;
    const attempt = begin(scope, source);
    reconciliations.current.set(keyOf(scope), attempt);
    update(scope, source, { busy: true, pending });
    try {
      const value = await readJson<Envelope>(source(`/api/analyses/${scope.analysisId}/task/requests/${pending.body.request_id}?${new URLSearchParams(scope.ownerId ? { company_id: scope.ownerId } : {})}`));
      const settled = settle(value, pending, scope, attempt);
      if (settled && value.outcome === 'accepted') await read(scope, source);
    } catch (cause) { if (owns(scope, attempt, pending)) update(scope, source, { error: safeErrorCode(cause) }); }
    finally {
      if (reconciliations.current.get(keyOf(scope)) === attempt) reconciliations.current.delete(keyOf(scope));
      if (owns(scope, attempt)) update(scope, source, { busy: false });
    }
  }
  // API identity is part of ownership of a response; a restarted host must be read afresh.
  useEffect(() => {
    if (!api || !target) return;
    update(target, api, { data: undefined, error: undefined, busy: false, preview: undefined, confirmation: undefined, needsRead: true });
    void read(target, api);
    if (restored(target)) void reconcile(target, api);
    // Only a changed target or backend instance starts this read; never a render or command.
  }, [api, key, target?.uiCompanyId]);
  useEffect(() => {
    if (!api || !target || state.error || state.busy || submissions.current.has(key) || state.confirmation || (!state.pending && !ACTIVE.has(state.data?.run?.state ?? ''))) return;
    const timer = setTimeout(() => { if (state.pending) void reconcile(target, api); else void read(target, api, false); }, 1000);
    return () => clearTimeout(timer);
  }, [api, key, revision]);

  async function command(operation: Operation, stillSelected: () => boolean = () => true, original = false) {
    if (!api || !target) return false;
    const source = api, scope = target, old = states.current.get(key);
    if (!old || old.api !== source || old.busy || submissions.current.has(key) || reconciliations.current.get(key)?.source === source || old.data?.read_only) return false;
    const originalRequest = original ? old.pending : undefined;
    if (original ? !originalRequest || old.confirmation !== 'retry' : !old.data || old.pending || old.needsRead) return false;
    if (originalRequest && originalRequest.operation !== operation) return false;
    const run = operation === 'resume' ? old.preview?.run : old.data?.run;
    if (!original) {
      if (operation === 'resume' && (!old.preview || !run || !RECOVERABLE.has(run.state))) return false;
      if (operation === 'cancel' && (!run || !['queued', 'running'].includes(run.state))) return false;
      if (operation === 'start' && run && !['completed', 'partial', 'failed'].includes(run.state)) return false;
    }
    submissions.current.add(key);
    const attempt = begin(scope, source);
    update(scope, source, { busy: true, error: undefined });
    let pending: Pending | undefined;
    let registered = false;
    try {
      if (scope.legacy) {
        const binding = await readJson<AnalysisBinding>(source(`/api/company-workspaces/${scope.uiCompanyId}/analyses/${scope.analysisId}/binding`));
        if (binding.bound || binding.company_id !== scope.uiCompanyId || binding.analysis_id !== scope.analysisId) throw new Error('WORKSPACE_HISTORICAL_READ_ONLY');
        if (!mounted.current || !stillSelected() || live.current.api !== source || live.current.target?.uiCompanyId !== scope.uiCompanyId || keyOf(live.current.target) !== keyOf(scope)) return false;
      }
      pending = originalRequest ?? { analysisId: scope.analysisId, operation, body: { request_id: crypto.randomUUID(), company_id: scope.ownerId,
        expected_run_id: run?.id ?? null, expected_version: run?.version ?? null } };
      try {
        if (!original && localStorage.getItem(storageKey(key)) !== null) throw new Error('TASK_JOURNAL_WRITE_FAILED');
        localStorage.setItem(storageKey(key), JSON.stringify(pending));
      }
      catch { throw new Error('TASK_JOURNAL_WRITE_FAILED'); }
      update(scope, source, { pending, preview: undefined, confirmation: undefined });
      registered = true;
      const response = await source(`/api/analyses/${scope.analysisId}/task/${operation}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pending.body) });
      let value: Envelope;
      try { value = await readJson<Envelope>(Promise.resolve(response)); }
      catch (cause) {
        // Reviewed service checks this UUID's receipt before immutable run/version CAS.
        // Other HTTP errors (including admission failures) do not establish the original outcome.
        if (response.status === 409 && safeErrorCode(cause) === 'TASK_VERSION_CONFLICT' && owns(scope, attempt, pending)) {
          localStorage.removeItem(storageKey(key));
          update(scope, source, { pending: undefined, confirmation: undefined, needsRead: true, error: safeErrorCode(cause) });
          return false;
        }
        throw cause;
      }
      const resolved = settle(value, pending, scope, attempt);
      if (resolved && value.outcome === 'accepted') { await read(scope, source); return live.current.api === source; }
      return false;
    } catch (cause) { if (owns(scope, attempt, registered ? pending : undefined)) update(scope, source, { error: safeErrorCode(cause), ...(!registered ? { needsRead: true } : {}) }); return false; }
    finally {
      submissions.current.delete(key);
      if (owns(scope, attempt)) update(scope, source, { busy: false });
      if (mounted.current && live.current.api !== source) render(n => n + 1);
    }
  }
  const run = state.data?.run;
  const busy = Boolean(state.busy || submissions.current.has(key) || api && reconciliations.current.get(key)?.source === api);
  const locked = !state.data || Boolean(busy || state.pending || state.needsRead || state.data.read_only);
  async function showPreview(retry = false) {
    if (!api || !target || (retry ? !state.pending || busy : locked)) return;
    const source = api, scope = target;
    update(scope, source, { busy: true, preview: undefined, previewFiles: undefined, confirmation: undefined });
    const fresh = await read(scope, source);
    try {
      if (!fresh || fresh.read_only || !retry && (!fresh.run || !RECOVERABLE.has(fresh.run.state))) return;
      const workspace = await readJson<WorkspacePayload>(source(`/api/analyses/${scope.analysisId}/workspace`));
      if (workspace.analysis.id !== scope.analysisId || !Array.isArray(workspace.files)) throw new Error('TASK_SCOPE_INVALID');
      update(scope, source, { preview: fresh, previewFiles: workspace.files, confirmation: retry ? 'retry' : 'resume' });
    } catch (cause) { update(scope, source, { error: safeErrorCode(cause), needsRead: true }); }
    finally { update(scope, source, { busy: false }); }
  }
  function prepareRetry() {
    if (!api || !target || !state.pending || busy) return;
    if (state.pending.operation === 'resume') { void showPreview(true); return; }
    update(target, api, { confirmation: 'retry', preview: undefined });
  }
  return { ...state, busy, target, run, active: ACTIVE.has(run?.state ?? ''), locked,
    canResume: Boolean(run && RECOVERABLE.has(run.state)),
    canCancel: Boolean(run && ['queued', 'running'].includes(run.state)),
    read: () => read(), reconcile: () => reconcile(), command,
    showPreview: () => showPreview(), prepareRetry,
    retryOriginal: (stillSelected?: () => boolean) => state.pending ? command(state.pending.operation, stillSelected, true) : Promise.resolve(false),
    hidePreview: () => { if (api && target) update(target, api, { preview: undefined, confirmation: undefined }); } };
}
export type AnalysisTask = ReturnType<typeof useAnalysisTask>;
