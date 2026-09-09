import { useEffect, useRef, useState } from 'react';
import { readJson, safeErrorCode } from '../../lib/api';
import { describeOperationError } from '../../lib/errorMessages';
import type { DesktopRequest } from './useCompanyWorkspace';

type Handling = { id: string; observation_id: string; version: number; decision: string; reason: string };
type Observation = { id: string; run_id: string; file_id: string; filename: string; employee_id: string | null;
  assignment_status: string; issue: string; checks: string[]; next_action: string; unverified_references: string[];
  source: { location: Record<string, unknown>; excerpt: string; provenance: string }; requires_source_review: boolean;
  version: number; handling: Handling | null; is_latest: boolean; read_only: boolean };
type Payload = { runs: Array<{ id: string; filename: string; execution_status: string; is_latest: boolean }>;
  observations: Observation[]; page: number; pages: number; total: number; run_total: number;
  read_only: boolean; request_handling: Handling | null };
export type PendingAdvisory = { id: string; expected_version: number; decision: string; reason: string; inFlight: boolean };
export type AdvisoryRequests = Map<string, PendingAdvisory>;
export type AdvisoryDrafts = Map<string, { reason: string; decision: string }>;

const STATUS: Record<string, string> = { completed: '已执行条款观察（意见未核验）', not_executed: '未执行条款观察',
  partial: '仅部分内容完成条款观察', unreadable: '无法读取条款内容', not_applicable: '模型未识别为适用的合同内容，仍需人工核对' };
const DECISIONS: Record<string, string> = { pending: '待处理', checking: '正在核对', addressed: '已采取措施', dismissed: '暂不采纳' };

function ClauseCard({ row, api, base, requests, drafts, readonly, refresh }: { row: Observation; api: DesktopRequest;
  base: string; requests: AdvisoryRequests; drafts: AdvisoryDrafts; readonly: boolean; refresh: () => Promise<void> }) {
  const key = `${base}/${row.id}`;
  const [reason, setReason] = useState(requests.get(key)?.reason ?? drafts.get(key)?.reason ?? row.handling?.reason ?? '');
  const [decision, setDecision] = useState(requests.get(key)?.decision ?? drafts.get(key)?.decision ?? row.handling?.decision ?? 'checking');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const pending = requests.get(key);
  const lifetime = useRef(0);
  const live = useRef({ api, key }); live.current = { api, key };
  useEffect(() => () => { lifetime.current++; }, [api, key]);
  function guard(target: PendingAdvisory) {
    const epoch = lifetime.current;
    return () => epoch === lifetime.current && live.current.api === api && live.current.key === key && requests.get(key) === target;
  }
  async function reconcile() {
    if (busy) return;
    const target = requests.get(key); if (!target) return;
    const owns = guard(target), operationEpoch = lifetime.current;
    setBusy(true); setError('');
    try {
      const response = await readJson<Payload>(api(`${base}?request_id=${encodeURIComponent(target.id)}`));
      if (!owns()) return;
      if (!response.request_handling) {
        setMessage('请求仍在等待响应，暂未查到记录；请稍后再次核对。');
        return;
      }
      const saved = response.request_handling;
      if (saved.id !== target.id || saved.observation_id !== row.id || saved.version !== target.expected_version + 1 || saved.decision !== target.decision) throw new Error('ADVISORY_SOURCE_INVALID');
      await refresh(); if (!owns()) return;
      requests.delete(key); drafts.delete(key); setMessage('条款处理已核实保存。');
    } catch (failure) { if (owns()) setError(describeOperationError(safeErrorCode(failure))); }
    finally { if (operationEpoch === lifetime.current && live.current.api === api && live.current.key === key) setBusy(false); }
  }
  async function save(original = false) {
    const previous = requests.get(key);
    if (busy || readonly || (original ? !previous || previous.inFlight : Boolean(previous) || !reason.trim())) return;
    const target = original ? previous! : { id: crypto.randomUUID(), expected_version: row.version, decision, reason: reason.trim(), inFlight: true };
    target.inFlight = true;
    requests.set(key, target); setBusy(true); setMessage(''); setError('');
    const owns = guard(target), operationEpoch = lifetime.current;
    try {
      const body = { id: target.id, expected_version: target.expected_version, decision: target.decision, reason: target.reason };
      const saved = await readJson<Observation>(api(`${base}/${row.id}/handling`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
      if (!owns()) return;
      if (saved.id !== row.id || saved.handling?.id !== target.id || saved.handling.observation_id !== row.id) throw new Error('ADVISORY_SOURCE_INVALID');
      await refresh(); if (!owns()) return;
      requests.delete(key); drafts.delete(key); setMessage('条款处理已保存。');
    } catch (failure) { if (owns()) setError(describeOperationError(safeErrorCode(failure))); }
    finally { target.inFlight = false; if (operationEpoch === lifetime.current && live.current.api === api && live.current.key === key) setBusy(false); }
  }
  return <article className="status-card" aria-label="条款观察详情">
    <h4>{row.issue}</h4><p>{row.filename} · {row.is_latest ? '最新观察' : '历史观察，只读'}</p>
    {row.assignment_status !== 'assigned' ? <p>员工归属{row.assignment_status === 'unmatched' ? '未匹配' : '待确认'}；请在材料的员工匹配中处理。</p> : null}
    {row.source.provenance === 'locally_located' ? <>
      <p>本地已定位：{Object.entries(row.source.location).map(([k, v]) => `${({ paragraph: '段落', page: '页', table: '表', row: '行', column: '列', block: '文本块' } as Record<string, string>)[k] ?? k} ${String(v)}`).join(' · ')}</p>
      <blockquote>{row.source.excerpt}</blockquote>
    </> : <p>原文未定位，需核对原材料；模型声称的摘录不能作为来源证明。</p>}
    {row.requires_source_review ? <p>来源位置或归属仍需人工核对。</p> : null}
    <p>需核对：{row.checks.join('；')}</p><p>建议下一步：{row.next_action}</p>
    {row.unverified_references.length ? <p>模型引用（未经核验）：{row.unverified_references.join('；')}</p> : null}
    {row.handling ? <p>人工处理：{DECISIONS[row.handling.decision] ?? '待核对'} · {row.handling.reason}（版本 {row.handling.version}）</p> : null}
    {!readonly ? <form onSubmit={e => { e.preventDefault(); void save(); }}>
      <label>条款处理决定<select aria-label="条款处理决定" value={decision} disabled={busy || Boolean(pending)} onChange={e => {
        setDecision(e.target.value); drafts.set(key, { reason, decision: e.target.value });
      }}>
        {Object.entries(DECISIONS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>条款处理说明<textarea aria-label="条款处理说明" value={reason} maxLength={500} disabled={busy || Boolean(pending)} onChange={e => {
        setReason(e.target.value); drafts.set(key, { reason: e.target.value, decision });
      }} /></label>
      <button type="submit" disabled={busy || Boolean(pending) || !reason.trim()}>保存条款处理</button>
      <button type="button" disabled={busy || Boolean(pending) || !drafts.has(key)} onClick={() => {
        drafts.delete(key); setReason(row.handling?.reason ?? ''); setDecision(row.handling?.decision ?? 'checking');
        setMessage('未保存的条款修改已放弃。');
      }}>放弃未保存修改</button>
    </form> : <p>历史条款观察及处理记录只读。</p>}
    {pending ? <><p>提交结果尚未核实；先核对已保存结果，不会自动重复提交。</p>
      <button type="button" disabled={busy} onClick={() => void reconcile()}>核对条款处理结果</button>
      <button type="button" disabled={busy || pending.inFlight || readonly} onClick={() => void save(true)}>重试原条款处理请求</button></> : null}
    {message ? <p role="status">{message}</p> : null}{error ? <p role="alert">{error}</p> : null}
  </article>;
}

export function ContractAdvisoryPanel({ companyId, analysisId, fileId, recordId, api, requests, drafts, readOnly = false }:
  { companyId: string; analysisId: string; fileId?: string; recordId?: string; api: DesktopRequest; requests: AdvisoryRequests; drafts: AdvisoryDrafts; readOnly?: boolean }) {
  const base = `/api/company-workspaces/${companyId}/analyses/${analysisId}/contract-advisories`;
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState('');
  const [page, setPage] = useState(1);
  const [history, setHistory] = useState(false);
  const params = new URLSearchParams({ page: String(page), page_size: '20', history: String(history) });
  if (fileId) params.set('file_id', fileId);
  if (recordId) params.set('record_id', recordId);
  const url = `${base}?${params}`;
  const live = useRef({ api, url }); live.current = { api, url };
  const lifetime = useRef(0), readAttempt = useRef(0);
  async function refresh() {
    const epoch = lifetime.current, sequence = ++readAttempt.current;
    const response = await readJson<Payload>(api(url));
    if (!Array.isArray(response.observations) || !Array.isArray(response.runs)) throw new Error('DESKTOP_OPERATION_FAILED');
    if (epoch !== lifetime.current || sequence !== readAttempt.current || live.current.api !== api || live.current.url !== url) return;
    setData(response); setError('');
  }
  useEffect(() => {
    let alive = true; const controller = new AbortController(), sequence = ++readAttempt.current;
    void readJson<Payload>(api(url, { signal: controller.signal })).then(response => {
      if (!Array.isArray(response.observations) || !Array.isArray(response.runs)) throw new Error('DESKTOP_OPERATION_FAILED');
      if (alive && sequence === readAttempt.current) { setData(response); setError(''); }
    }).catch(failure => { if (alive && sequence === readAttempt.current) setError(describeOperationError(safeErrorCode(failure))); });
    return () => { alive = false; lifetime.current++; readAttempt.current++; controller.abort(); };
  }, [api, url]);
  return <section aria-label="合同条款观察">
    <h3>合同条款观察</h3><p>AI 辅助意见及引用未经法律核验，不计入确定性规则的高、中风险数量。定位仅证明文字位置，人工处理也不改变原始观察。</p>
    <label><input type="checkbox" checked={history} onChange={e => { setHistory(e.target.checked); setPage(1); }} />查看历次条款观察</label>
    {error ? <><p role="alert">条款观察读取失败：{error}</p><button type="button" onClick={() => void refresh().catch(failure => setError(describeOperationError(safeErrorCode(failure))))}>重读条款观察</button></> : null}
    {!data ? <p>正在读取已保存的条款观察…</p> : <>
      {data.runs.map(run => <p key={run.id}>{run.filename}：{STATUS[run.execution_status] ?? '执行状态待核对'}{run.is_latest ? '' : ' · 历史记录'}</p>)}
      {!data.runs.length ? <p>尚无可显示的条款审阅记录。旧版提取可能未执行条款观察；更新须在材料页明确开始分析，可能消耗额度。</p> : null}
      {!data.observations.length ? <p>未返回条款观察，不代表合同合法、无风险或审阅完整。</p> : null}
      {data.observations.map(row => <ClauseCard key={`${base}/${row.id}`} row={row} api={api} base={base} requests={requests} drafts={drafts}
        readonly={readOnly || data.read_only || row.read_only} refresh={refresh} />)}
      {data.pages > 1 ? <div><button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页条款</button>
        <span>条款第 {page} / {data.pages} 页</span><button type="button" disabled={page >= data.pages} onClick={() => setPage(page + 1)}>下一页条款</button></div> : null}
    </>}
  </section>;
}
