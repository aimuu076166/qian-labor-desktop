import { useEffect, useRef, useState } from 'react';
import { readJson, safeErrorCode } from '../../lib/api';
import { describeOperationError } from '../../lib/errorMessages';
import { EmployeeLedger, type EmployeeLedgerPayload } from './EmployeeLedger';

export function EmployeeWorkspace({ analysisId, api, initialPayload, initialQuery, onSnapshot,
  onSelectEmployee, onBack }: {
  analysisId: string;
  api: (path: string, init?: RequestInit) => Promise<Response>;
  initialPayload: EmployeeLedgerPayload;
  initialQuery: string;
  onSnapshot: (payload: EmployeeLedgerPayload, query: string) => void;
  onSelectEmployee: (id: string) => void;
  onBack: () => void;
}) {
  const [payload, setPayload] = useState(initialPayload);
  const [draft, setDraft] = useState(initialQuery);
  const [appliedQuery, setAppliedQuery] = useState(initialQuery);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  useEffect(() => () => { sequence.current += 1; request.current?.abort(); }, []);

  async function load(page: number, query: string) {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    setBusy(true);
    setError(null);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: '25', query });
      const next = await readJson<EmployeeLedgerPayload>(api(
        `/api/analyses/${analysisId}/employees?${params}`, { signal: controller.signal },
      ));
      if (current !== sequence.current) return;
      if (!Array.isArray(next.items) || !Number.isInteger(next.page) || next.page < 1
          || !Number.isInteger(next.pages) || next.pages < 0 || !Number.isInteger(next.total) || next.total < 0) {
        throw new Error('DESKTOP_OPERATION_FAILED');
      }
      setPayload(next);
      setAppliedQuery(query);
      onSnapshot(next, query);
    } catch (failure) {
      if (current === sequence.current) setError(safeErrorCode(failure));
    } finally {
      if (current === sequence.current) { request.current = null; setBusy(false); }
    }
  }

  return <EmployeeLedger payload={payload} busy={busy} onBack={onBack} onSelectEmployee={onSelectEmployee}
    controls={<>
      <form className="dashboard-actions" onSubmit={event => { event.preventDefault(); void load(1, draft.trim()); }}>
        <label>搜索员工<input aria-label="搜索员工" placeholder="工号或员工名称" maxLength={100}
          value={draft} onChange={event => setDraft(event.target.value)} disabled={busy} /></label>
        <button type="submit" className="secondary-action" disabled={busy}>{busy ? '正在读取…' : '搜索'}</button>
      </form>
      {error ? <p role="alert">{describeOperationError(error)} 仍显示上次成功读取的列表，请重试搜索或翻页。</p> : null}
      <div className="dashboard-actions" aria-label="员工分页">
        <button type="button" className="text-action" disabled={busy || payload.page <= 1}
          onClick={() => void load(payload.page - 1, appliedQuery)}>上一页</button>
        <span>第 {payload.page} / {Math.max(1, payload.pages)} 页</span>
        <button type="button" className="text-action" disabled={busy || payload.page >= payload.pages}
          onClick={() => void load(payload.page + 1, appliedQuery)}>下一页</button>
      </div>
    </>} />;
}
