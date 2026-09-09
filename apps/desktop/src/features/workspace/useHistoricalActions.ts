import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { readJson, safeErrorCode } from '../../lib/api';
import type { Company, DesktopRequest } from './useCompanyWorkspace';
import type { HistoricalAnalysis } from './useHistoryWorkspace';
import type { WorkspacePayload } from './MaterialWorkspace';
import type { CurrentCorpus } from './useCurrentCorpus';

type Result = { source_file_id: string; file_id: string | null; status: 'imported' | 'duplicate' | 'error'; error_code: string | null };
type CopyResponse = { analysis_id: string; source_analysis_id: string; results: Result[]; requires_explicit_start: boolean; provider_quota_notice: string };
type Copy = { company: Company; analysis: HistoricalAnalysis; files: WorkspacePayload['files']; selection: string[];
  busy: boolean; loaded: boolean; uncertain: boolean; error: string | null; notice: string | null; response?: CopyResponse };
type Deletion = { companyId: string; analysis: HistoricalAnalysis; busy: boolean; uncertain: boolean; error: string | null; notice: string | null };
export function permittedHistorical(item: HistoricalAnalysis, companyId: string | null, currentId: string | null) {
  return item.id !== currentId && Boolean(companyId) && (item.relation === 'historical' && item.company_id === companyId || item.relation === 'unbound' && item.company_id === null);
}
export function useHistoricalActions(api: DesktopRequest | null, company: Company | null | undefined, currentId: string | null,
  corpus: CurrentCorpus, onDeleted: (companyId: string, id: string) => void) {
  const client = useQueryClient();
  const [copy, setCopy] = useState<Copy | null>(null);
  const [deletion, setDeletion] = useState<Deletion | null>(null);
  // Share the operation gate for reads as well as writes: the other workflow must not invalidate
  // an outstanding read's epoch and leave its retained panel permanently busy.
  const epoch = useRef(0); const mutation = useRef(false);
  const unresolvedCopies = useRef(new Map<string, Copy>());
  const unresolvedDeletions = useRef(new Map<string, Deletion>());
  const deletedCallback = useRef(onDeleted); deletedCallback.current = onDeleted;
  const companyId = company?.id ?? null;
  useEffect(() => { epoch.current++; setCopy(null); setDeletion(null); }, [companyId]);
  async function refresh(id: string) {
    await Promise.all(['company-history', 'company-current', 'history-employee-options'].map(key => client.invalidateQueries({ queryKey: [key, id] })));
    await client.invalidateQueries({ queryKey: ['desktop-workspace'] });
  }
  async function openCopy(analysis: HistoricalAnalysis) {
    if (!api || !company || mutation.current || !permittedHistorical(analysis, companyId, currentId) || analysis.relation !== 'historical') return;
    const unresolved = unresolvedCopies.current.get(company.id);
    if (unresolved) { setCopy(unresolved); return; }
    mutation.current = true;
    const ticket = ++epoch.current;
    const target: Copy = { company, analysis, files: [], selection: [], busy: true, loaded: false, uncertain: false, error: null, notice: null };
    setCopy(target);
    try {
      const payload = await readJson<WorkspacePayload>(api(`/api/analyses/${analysis.id}/workspace`));
      if (payload.analysis.id !== analysis.id) throw new Error('WORKSPACE_HISTORICAL_SOURCE_INVALID');
      if (ticket === epoch.current) setCopy({ ...target, files: payload.files, loaded: true, busy: false });
    } catch (cause) { if (ticket === epoch.current) setCopy({ ...target, busy: false, error: safeErrorCode(cause) }); }
    finally { mutation.current = false; }
  }
  async function submitCopy() {
    if (!api || !copy || copy.busy || copy.uncertain || !copy.loaded || !copy.selection.length || copy.selection.length > 100 || mutation.current) return;
    const target = copy; const ticket = epoch.current; mutation.current = true;
    unresolvedCopies.current.set(target.company.id, { ...target, busy: false, uncertain: true });
    setCopy({ ...target, busy: true, response: undefined, error: null, notice: null });
    let posted = false;
    try {
      const id = await corpus.resolve(target.company);
      // A late current creation must not start a copy after a company change or cancelled selection.
      if (ticket !== epoch.current) { void refresh(target.company.id); return; }
      posted = true;
      const response = await readJson<CopyResponse>(api(`/api/company-workspaces/${target.company.id}/current-analysis/import-historical`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_analysis_id: target.analysis.id, file_ids: target.selection }),
      }));
      if (response.analysis_id !== id || response.source_analysis_id !== target.analysis.id || response.results.length !== target.selection.length ||
        response.results.some((result, index) => result.source_file_id !== target.selection[index] || !['imported', 'duplicate', 'error'].includes(result.status))) throw new Error('WORKSPACE_COPY_RESPONSE_UNKNOWN');
      if (ticket === epoch.current) setCopy({ ...target, busy: false, response, error: null, notice: null });
      unresolvedCopies.current.delete(target.company.id);
      void refresh(target.company.id);
    } catch (cause) {
      if (!posted && !corpus.pending(target.company.id)?.uncertain) unresolvedCopies.current.delete(target.company.id);
      if (ticket === epoch.current) setCopy({ ...target, busy: false, response: undefined,
        uncertain: posted || Boolean(corpus.pending(target.company.id)?.uncertain), error: safeErrorCode(cause),
        notice: '结果尚未核实。请先读取当前材料状态，再明确决定是否重试；不会自动重复提交。' });
    } finally { mutation.current = false; }
  }
  async function reconcileCopy() {
    if (!api || !copy || copy.busy || mutation.current) return;
    mutation.current = true;
    const target = copy; const ticket = epoch.current; setCopy({ ...target, busy: true });
    try {
      const current = await corpus.reconcile(target.company.id);
      if (current) {
        const payload = await readJson<WorkspacePayload>(api(`/api/analyses/${current.analysis_id}/workspace`));
        if (payload.analysis.id !== current.analysis_id) throw new Error('WORKSPACE_CURRENT_CONFLICT');
      }
      if (ticket === epoch.current) setCopy({ ...target, busy: false, uncertain: false, error: null,
        notice: '当前材料状态已读取，不能据文件名称判断原选择是否成功。可明确重试原选择，已有相同内容会按重复材料处理。' });
      unresolvedCopies.current.delete(target.company.id);
      void refresh(target.company.id);
    } catch (cause) { if (ticket === epoch.current) setCopy({ ...target, busy: false, uncertain: true, error: safeErrorCode(cause) }); }
    finally { mutation.current = false; }
  }
  function openDelete(analysis: HistoricalAnalysis) {
    if (!companyId || mutation.current || !permittedHistorical(analysis, companyId, currentId) || !['completed', 'partial', 'failed'].includes(analysis.status)) return;
    const unresolved = unresolvedDeletions.current.get(analysis.id);
    if (unresolved) { setDeletion({ ...unresolved, companyId }); return; }
    epoch.current++; setDeletion({ companyId, analysis, busy: false, uncertain: false, error: null, notice: null });
  }
  async function completed(target: Deletion, ticket: number) {
    unresolvedDeletions.current.delete(target.analysis.id);
    if (copy?.analysis.id === target.analysis.id && copy.company.id === target.companyId && ticket === epoch.current) setCopy(null);
    if (ticket === epoch.current) setDeletion(null);
    deletedCallback.current(target.companyId, target.analysis.id); await refresh(target.companyId);
  }
  async function confirmDelete() {
    if (!api || !deletion || deletion.busy || deletion.uncertain || mutation.current || !permittedHistorical(deletion.analysis, companyId, currentId)) return;
    const target = deletion; const ticket = epoch.current; mutation.current = true; setDeletion({ ...target, busy: true, error: null });
    unresolvedDeletions.current.set(target.analysis.id, { ...target, busy: false, uncertain: true });
    try {
      const result = await readJson<{ id: string; status: string }>(api(`/api/analyses/${target.analysis.id}`, { method: 'DELETE' }));
      if (result.id !== target.analysis.id || result.status !== 'deleted') throw new Error('WORKSPACE_DELETE_RESPONSE_UNKNOWN');
      await completed(target, ticket);
    } catch (cause) { if (ticket === epoch.current) setDeletion({ ...target, busy: false, uncertain: true, error: safeErrorCode(cause), notice: '删除结果尚未核实；先读取状态，不会自动再次删除。' }); }
    finally { mutation.current = false; }
  }
  async function reconcileDelete() {
    if (!api || !deletion || deletion.busy || mutation.current) return;
    mutation.current = true;
    const target = deletion; const ticket = epoch.current; setDeletion({ ...target, busy: true });
    try {
      const response = await api(`/api/analyses/${target.analysis.id}/workspace`);
      if (response.status === 404) { await completed(target, ticket); return; }
      const payload = await readJson<WorkspacePayload>(Promise.resolve(response));
      if (payload.analysis.id !== target.analysis.id) throw new Error('WORKSPACE_DELETE_RESPONSE_UNKNOWN');
      if (payload.analysis.status === 'deleted') { await completed(target, ticket); return; }
      if (payload.analysis.status !== 'deleting') unresolvedDeletions.current.delete(target.analysis.id);
      if (ticket === epoch.current) setDeletion({ ...target, busy: false, uncertain: payload.analysis.status === 'deleting', error: null,
        notice: payload.analysis.status === 'deleting' ? '仍在删除中，请稍后再次读取状态。' : '记录仍存在。请明确决定取消或再次确认删除。' });
    } catch (cause) { if (ticket === epoch.current) setDeletion({ ...target, busy: false, uncertain: true, error: safeErrorCode(cause) }); }
    finally { mutation.current = false; }
  }
  return { copy: copy?.company.id === companyId ? copy : null, deletion: deletion?.companyId === companyId ? deletion : null,
    openCopy, submitCopy, reconcileCopy, openDelete, confirmDelete, reconcileDelete,
    select: (id: string) => setCopy(old => old && !old.busy && !old.uncertain ? { ...old, response: undefined, selection: old.selection.includes(id) ? old.selection.filter(item => item !== id) : [...old.selection, id] } : old),
    cancelCopy: () => { if (!mutation.current && !copy?.busy && !copy?.uncertain) { epoch.current++; setCopy(null); } },
    cancelDelete: () => { if (!mutation.current && !deletion?.busy && !deletion?.uncertain) { epoch.current++; setDeletion(null); } } };
}
export type HistoricalActions = ReturnType<typeof useHistoricalActions>;
