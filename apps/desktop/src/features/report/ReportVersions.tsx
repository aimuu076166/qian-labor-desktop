import { useEffect, useRef, useState } from 'react';
import type { DesktopRequest } from '../workspace/useCompanyWorkspace';
import { describeOperationError } from '../../lib/errorMessages';
import { ReportView, type ReportPayload } from './ReportView';

type Context = { input_revision: string; result_revision: string | null; review_revision: string; context_signature: string };
type Command = { request_id: string; expected_input_revision: string; expected_result_revision: string | null;
  expected_review_revision: string; expected_context_signature: string };
type Pending = Command & { company_id: string; analysis_id: string };
type Metadata = Context & { id: string; company_id: string; analysis_id: string; version: number; created_at: string; content_sha256: string };
type Snapshot = Metadata & { payload: ReportPayload };
type Listing = { items: Metadata[]; page: number; pages: number; total: number; current_context_available: boolean; current_context: Context | null; warning: string | null };
type Envelope = { request: (Pending & { outcome: 'accepted' | 'rejected'; version_id: string | null; content_sha256: string | null; error_code: string | null }) | null; snapshot: Snapshot | null };
const fields = ['request_id', 'company_id', 'analysis_id', 'expected_input_revision', 'expected_result_revision', 'expected_review_revision', 'expected_context_signature'] as const;
const same = (a: Pending, b: Pending) => fields.every(k => a[k] === b[k]);
const sha = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v);
function command(pending: Pending): Command {
  return { request_id: pending.request_id, expected_input_revision: pending.expected_input_revision,
    expected_result_revision: pending.expected_result_revision, expected_review_revision: pending.expected_review_revision,
    expected_context_signature: pending.expected_context_signature };
}
function restore(key: string, companyId: string, analysisId: string): Pending | null {
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  const value = JSON.parse(raw);
  if (!value || value.company_id !== companyId || value.analysis_id !== analysisId ||
    typeof value.request_id !== 'string' || !/^[0-9a-f-]{36}$/.test(value.request_id) ||
    !sha(value.expected_input_revision) || !sha(value.expected_review_revision) || !sha(value.expected_context_signature) ||
    !(value.expected_result_revision === null || typeof value.expected_result_revision === 'string') ||
    Object.keys(value).length !== fields.length) throw new Error('REPORT_JOURNAL_INVALID');
  return value;
}

export function ReportVersions({ api, companyId, analysisId, selections, onBack, livePayload }: {
  api: DesktopRequest; companyId: string; analysisId: string; selections: Map<string, string>;
  onBack: () => void; livePayload?: ReportPayload;
}) {
  const scope = `${companyId}/${analysisId}`;
  const key = `qian-report-request-v1:${scope}`;
  const base = `/api/company-workspaces/${companyId}/analyses/${analysisId}/report-versions`;
  const [listing, setListing] = useState<Listing | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [stale, setStale] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [locked, setLocked] = useState(true);
  const [journalInvalid, setJournalInvalid] = useState(false);
  const [listLoading, setListLoading] = useState(true), [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<{ id: string; code: string } | null>(null);
  const live = useRef({ api, scope }); live.current = { api, scope };
  const lifetime = useRef(0), attempt = useRef(0), readAttempt = useRef(0), detailAttempt = useRef(0);
  const busyRef = useRef(false);

  function session() {
    const epoch = lifetime.current;
    return () => live.current.api === api && live.current.scope === scope && lifetime.current === epoch;
  }
  function owns(p: Pending) {
    try { const stored = restore(key, companyId, analysisId); return stored !== null && same(stored, p); }
    catch { return false; }
  }
  async function read(path: string) {
    const response = await api(path);
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail?.code ?? 'REPORT_READ_FAILED');
    return data;
  }
  async function select(id: string) {
    const current = session(), sequence = ++detailAttempt.current;
    setDetailLoading(true); setDetailError(null);
    try {
      const data = await read(`${base}/${id}`);
      if (!current() || sequence !== detailAttempt.current) return;
      if (!data.snapshot || data.snapshot.id !== id || data.snapshot.company_id !== companyId || data.snapshot.analysis_id !== analysisId || !sha(data.snapshot.content_sha256)) throw new Error('REPORT_RESPONSE_INVALID');
      selections.set(scope, id); setSnapshot(data.snapshot); setStale(data.stale || !data.current_context_available);
    } catch (cause) { if (current() && sequence === detailAttempt.current) setDetailError({ id, code: cause instanceof Error ? cause.message : 'REPORT_READ_FAILED' }); }
    finally { if (current() && sequence === detailAttempt.current) setDetailLoading(false); }
  }
  async function refresh(page = 1) {
    const current = session(), sequence = ++readAttempt.current;
    setLocked(true); setListLoading(true); setListError(null);
    try {
      const data: Listing = await read(`${base}?page=${page}`);
      if (!current() || sequence !== readAttempt.current) return;
      if (!Array.isArray(data.items) || (data.current_context_available && !data.current_context)) throw new Error('REPORT_RESPONSE_INVALID');
      setListing(data); setLocked(false);
    } catch (cause) { if (current() && sequence === readAttempt.current) setListError(cause instanceof Error ? cause.message : 'REPORT_READ_FAILED'); }
    finally { if (current() && sequence === readAttempt.current) setListLoading(false); }
  }
  function resolve(data: Envelope, submitted: Pending): boolean {
    if (!data.request) return false; // Unknown, not a rejection or permission to replace UUID.
    const receipt = data.request;
    if (!same(receipt, submitted)) throw new Error('REPORT_RESPONSE_INVALID');
    if (receipt.outcome === 'accepted') {
      const saved = data.snapshot;
      if (!saved || saved.company_id !== companyId || saved.analysis_id !== analysisId || saved.payload?.analysis_id !== analysisId ||
        saved.id !== receipt.version_id || !sha(saved.content_sha256) || saved.content_sha256 !== receipt.content_sha256 ||
        saved.input_revision !== submitted.expected_input_revision || saved.result_revision !== submitted.expected_result_revision ||
        saved.review_revision !== submitted.expected_review_revision || saved.context_signature !== submitted.expected_context_signature) throw new Error('REPORT_RESPONSE_INVALID');
    } else if (receipt.outcome !== 'rejected' || !['REPORT_VERSION_CONFLICT', 'REPORT_SOURCE_INVALID'].includes(receipt.error_code ?? '') || receipt.version_id !== null || receipt.content_sha256 !== null || data.snapshot !== null) {
      throw new Error('REPORT_RESPONSE_INVALID');
    }
    if (!owns(submitted)) return false;
    localStorage.removeItem(key); // Removal failure keeps the unresolved lock.
    setPending(null);
    if (receipt.outcome === 'accepted') {
      selections.set(scope, data.snapshot!.id); setSnapshot(data.snapshot); setStale(false); setError(null);
      void select(data.snapshot!.id);
    } else setError(receipt.error_code);
    void refresh();
    return true;
  }
  async function reconcile(submitted: Pending) {
    if (busyRef.current) return;
    const current = session(), sequence = ++attempt.current;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      const data = await read(`${base}/requests/${submitted.request_id}`);
      if (current() && sequence === attempt.current && owns(submitted)) resolve(data, submitted);
    } catch (cause) { if (current() && sequence === attempt.current) setError(cause instanceof Error ? cause.message : 'REPORT_READ_FAILED'); }
    finally { if (current() && sequence === attempt.current) { busyRef.current = false; setBusy(false); } }
  }
  async function submit(submitted: Pending) {
    if (busyRef.current || !owns(submitted)) return;
    const current = session(), sequence = ++attempt.current;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      const response = await api(base, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(command(submitted)) });
      const data = await response.json();
      if (!current() || sequence !== attempt.current || !owns(submitted)) return;
      if (!(response.ok || response.status === 409) || !data?.request) throw new Error(data?.detail?.code ?? 'REPORT_RESPONSE_INVALID');
      resolve(data, submitted);
    } catch (cause) { if (current() && sequence === attempt.current) setError(cause instanceof Error ? cause.message : 'REPORT_READ_FAILED'); }
    finally { if (current() && sequence === attempt.current) { busyRef.current = false; setBusy(false); } }
  }
  function generate() {
    if (locked || journalInvalid || busyRef.current || pending || !listing?.current_context_available || !listing.current_context) return;
    try {
      if (localStorage.getItem(key)) throw new Error('REPORT_JOURNAL_INVALID');
      const c = listing.current_context;
      const submitted = { company_id: companyId, analysis_id: analysisId, request_id: crypto.randomUUID(),
        expected_input_revision: c.input_revision, expected_result_revision: c.result_revision,
        expected_review_revision: c.review_revision, expected_context_signature: c.context_signature };
      localStorage.setItem(key, JSON.stringify(submitted));
      if (!owns(submitted)) throw new Error('REPORT_JOURNAL_INVALID');
      setPending(submitted); void submit(submitted);
    } catch { setError('REPORT_JOURNAL_INVALID'); setJournalInvalid(true); setLocked(true); }
  }
  useEffect(() => {
    lifetime.current++; busyRef.current = false; setBusy(false); setListing(null); setSnapshot(null); setError(null); setLocked(true);
    setListLoading(true); setDetailLoading(false); setListError(null); setDetailError(null);
    try { const saved = restore(key, companyId, analysisId); setJournalInvalid(false); setPending(saved); if (saved) void reconcile(saved); }
    catch { setError('REPORT_JOURNAL_INVALID'); setJournalInvalid(true); setPending(null); }
    void refresh();
    const selected = selections.get(scope); if (selected) void select(selected);
    return () => { lifetime.current++; attempt.current++; readAttempt.current++; detailAttempt.current++; };
    // Scope/API renewal establishes a new async lifetime; no POST is effect-driven.
  }, [api, scope]);

  return <section aria-label="已保存报告版本">
    <div className="print-hidden report-version-controls">
      <h3>报告版本</h3>
      <p>生成只保存当前复核草稿，不调用模型、不重新体检。历史材料的报告保存时间不是历史体检时间。</p>
      {listLoading || detailLoading ? <p role="status" aria-label="报告读取状态">{listLoading ? '正在读取报告版本和当前依据…' : '正在读取已保存报告…'}</p> : null}
      {listError || detailError ? <div role="alert"><p>报告读取失败，原已保存内容仍保留：{describeOperationError(listError ?? detailError!.code)}</p>
        <button type="button" disabled={listLoading || detailLoading} onClick={() => { if (listError) void refresh(listing?.page ?? 1); if (detailError) void select(detailError.id); }}>重试读取报告</button></div> : null}
      <button type="button" onClick={generate} disabled={locked || journalInvalid || busy || !!pending || !listing?.current_context_available}>生成并保存报告版本</button>
      <button type="button" disabled={busy || listLoading || detailLoading} onClick={() => { void refresh(); if (snapshot) void select(snapshot.id); }}>刷新报告版本</button>
      {pending ? <div role="status"><p>报告请求结果尚待核对；保留原请求和生成依据，不能创建新请求。</p>
        <button type="button" disabled={busy} onClick={() => { void reconcile(pending); }}>核对报告请求</button>
        <button type="button" disabled={busy} onClick={() => { void submit(pending); }}>重试原报告请求</button>
      </div> : null}
      {error ? <p role="alert">{describeOperationError(error)}</p> : null}
      {listing && !listing.current_context_available ? <p role="alert">当前来源无法核验；已保存内容仍可阅读，已停止生成新版本。</p> : null}
      {listing?.items.map(item => <button type="button" key={item.id} aria-pressed={snapshot?.id === item.id} onClick={() => { void select(item.id); }}>查看版本 {item.version}</button>)}
      {listing && listing.pages > 1 ? <div><button disabled={listing.page <= 1} onClick={() => { void refresh(listing.page - 1); }}>上一页报告</button>
        <span>第 {listing.page} / {listing.pages} 页</span><button disabled={listing.page >= listing.pages} onClick={() => { void refresh(listing.page + 1); }}>下一页报告</button></div> : null}
      {stale && snapshot ? <p role="status">当前材料或复核状态已变化，或无法核验；以下为原已保存版本，内容没有更新。</p> : null}
    </div>
    {snapshot ? <ReportView key={snapshot.id} payload={snapshot.payload} saved={snapshot} onBack={onBack} /> : livePayload ?
      <><p>报告草稿（读取时生成，尚未锁定版本）</p><ReportView payload={livePayload} onBack={onBack} /></> : !listLoading && !detailLoading ? <p className="print-hidden">请选择已保存版本，或明确生成当前草稿版本。</p> : null}
  </section>;
}
