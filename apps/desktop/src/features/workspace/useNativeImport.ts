import { useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { readJson } from '../../lib/api';
import type { CurrentAnalysis, DesktopRequest } from './useCompanyWorkspace';
import type { WorkspacePayload } from './MaterialWorkspace';

export type ImportOutcome = { index: number; filename: string; file_id: string | null;
  status: 'imported' | 'duplicate' | 'error'; error_code: string | null };
export type NativeImportState = { analysisId: string; paths: string[]; phase: 'busy' | 'unknown' | 'retry' | 'done';
  results: ImportOutcome[]; error: boolean };
const ERROR_TEXT: Record<string, string> = {
  DESKTOP_IMPORT_FILE_NOT_FOUND: '文件已移动或不存在',
  DESKTOP_IMPORT_PERMISSION_DENIED: '没有读取权限',
  DESKTOP_IMPORT_NOT_REGULAR: '请选择普通文件',
  DESKTOP_IMPORT_TOO_LARGE: '文件超过大小限制',
  DESKTOP_IMPORT_EMPTY: '文件内容为空',
  DESKTOP_IMPORT_FORMAT_UNSUPPORTED: '文件格式不受支持',
  DESKTOP_IMPORT_CONTENT_INVALID: '文件内容损坏或未通过校验',
  BATCH_FILE_LIMIT: '材料数量已达上限', BATCH_SIZE_LIMIT: '材料总大小已达上限',
  DESKTOP_IMPORT_FAILED: '本项导入未完成',
};
export function importError(code: string | null) {
  const safe = code && code in ERROR_TEXT ? code : 'DESKTOP_IMPORT_FAILED';
  return ERROR_TEXT[safe] + '（' + safe + '）';
}
function checkedResults(value: unknown, analysisId: string, count: number): ImportOutcome[] {
  const body = value as { analysis_id?: unknown; results?: ImportOutcome[] } | null;
  if (!body || body.analysis_id !== analysisId || !Array.isArray(body.results) || body.results.length !== count
    || body.results.some((r, index) => !r || r.index !== index || typeof r.filename !== 'string'
      || !['imported', 'duplicate', 'error'].includes(r.status)
      || (r.status === 'error' ? r.file_id !== null || typeof r.error_code !== 'string'
        : typeof r.file_id !== 'string' || !r.file_id || r.error_code !== null))) {
    throw new Error('DESKTOP_IMPORT_RESULT_UNKNOWN');
  }
  return body.results;
}

// App-lifetime company ledger: unknown POSTs stay locked across navigation.
export function useNativeImport(api: DesktopRequest | null) {
  const client = useQueryClient();
  const ledger = useRef(new Map<string, NativeImportState>());
  const locks = useRef(new Set<string>());
  const [, render] = useState(0);
  const changed = () => render(n => n + 1);
  async function refresh(companyId: string, analysisId: string) {
    await Promise.all([
      client.invalidateQueries({ queryKey: ['desktop-workspace', analysisId] }),
      client.invalidateQueries({ queryKey: ['company-current', companyId] }),
    ]);
  }
  async function submit(companyId: string, analysisId: string, paths: string[]) {
    if (!api || locks.current.has(companyId) || ledger.current.get(companyId)?.phase === 'unknown') return;
    locks.current.add(companyId);
    const state: NativeImportState = { analysisId, paths, phase: 'busy', results: [], error: false };
    ledger.current.set(companyId, state); changed();
    try {
      const body = await readJson(api('/api/analyses/' + analysisId + '/import-paths', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths }),
      }));
      state.results = checkedResults(body, analysisId, paths.length);
      state.phase = 'done';
    } catch {
      state.phase = 'unknown';
    } finally {
      await refresh(companyId, analysisId);
      locks.current.delete(companyId); changed();
    }
  }
  async function reconcile(companyId: string) {
    const state = ledger.current.get(companyId);
    if (!api || !state || locks.current.has(companyId)) return;
    locks.current.add(companyId); state.error = false; changed();
    try {
      const current = await readJson<CurrentAnalysis | null>(api('/api/company-workspaces/' + companyId + '/current-analysis'));
      if (!current || current.company_id !== companyId || current.role !== 'current' || current.analysis_id !== state.analysisId)
        throw new Error('WORKSPACE_CURRENT_CONFLICT');
      const workspace = await readJson<WorkspacePayload>(api('/api/analyses/' + state.analysisId + '/workspace'));
      if (workspace.analysis.id !== state.analysisId) throw new Error('WORKSPACE_CURRENT_CONFLICT');
      client.setQueryData(['desktop-workspace', state.analysisId], workspace);
      await refresh(companyId, state.analysisId);
      // No per-selection claim can be reconstructed from names in the current corpus.
      state.phase = 'retry';
    } catch { state.error = true; }
    finally { locks.current.delete(companyId); changed(); }
  }
  return { submit, reconcile, state: (companyId: string | null) => companyId ? ledger.current.get(companyId) : undefined,
    busy: (companyId: string | null) => Boolean(companyId && locks.current.has(companyId)) };
}
