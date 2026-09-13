import { useState } from 'react';
import { readJson, safeErrorCode } from '../../lib/api';
import { describeOperationError } from '../../lib/errorMessages';

export function EmployeeDisplayName({ name, version, companyId, recordId, api, disabled, onSaved }: {
  name: string; version: number; companyId: string; recordId: string; disabled: boolean;
  api: (path: string, init?: RequestInit) => Promise<Response>; onSaved: () => void;
}) {
  const [draft, setDraft] = useState(name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save() {
    if (busy || disabled || !draft.trim() || draft.trim() === name) return;
    setBusy(true); setError(null);
    try {
      await readJson(api(`/api/company-workspaces/${companyId}/employees/${recordId}/display-name`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: draft.trim(), expected_record_version: version }),
      }));
      onSaved();
    } catch (failure) { setError(safeErrorCode(failure)); }
    finally { setBusy(false); }
  }
  return <form className="dashboard-actions" onSubmit={event => { event.preventDefault(); void save(); }}>
    <label>本地员工显示名<input aria-label="本地员工显示名" maxLength={100} value={draft}
      disabled={disabled || busy} onChange={event => setDraft(event.target.value)} /></label>
    <button type="submit" disabled={disabled || busy || !draft.trim() || draft.trim() === name}>
      {busy ? '正在保存…' : '保存显示名'}</button>
    <p className="muted">仅更正本地显示名，不改变员工归属、事实或已保存报告，不调用模型。</p>
    {error ? <p role="alert">{describeOperationError(error)} 输入已保留；版本冲突时请重新打开员工详情后核对。</p> : null}
  </form>;
}
