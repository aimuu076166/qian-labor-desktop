import { useEffect, useMemo, useState } from 'react';
import { readJson, safeErrorCode, type AssessmentRevision } from '../../lib/api';
import { describeOperationError } from '../../lib/errorMessages';
import type { DesktopRequest } from '../workspace/useCompanyWorkspace';
import { AssessmentRevisionStatus } from './AssessmentRevisionStatus';

type FactValue = string | number | boolean | null | string[] | [string, string][];
type ValueSpec = { editable: boolean; input_type: 'read_only' | 'date' | 'boolean' | 'enum' | 'text' | 'list' | 'periods';
  options?: string[]; max_items?: number; max_length?: number; nullable?: boolean };
type Source = { id: string; file_id: string; locator_type: string; location: Record<string, unknown>; excerpt: string; provenance: string };
type Revision = { id: string; fact_id: string; version: number; kind: 'correct' | 'confirm'; value: FactValue;
  reason: string; employee_id: string; record_id: string; actor: string; created_at: string; context_signature: string };
type Fact = { id: string; analysis_id: string; employee_id: string; record_id: string; file_id: string; fact_type: string;
  filename: string; original_value: FactValue; effective_value: FactValue; verification_status: string; version: number;
  human_confirmed: boolean; revision_valid: boolean; latest_revision: Revision | null; sources: Source[];
  owner_signature: string; source_signature: string; context_signature: string; selected_support: boolean;
  basis_pending: boolean; source_valid: boolean; confirmation_context: string; read_only: boolean; value_spec: ValueSpec };
type Page<T> = { items: T[]; total: number; page: number; page_size: number; pages: number };
type FactsPayload = Page<Fact> & { read_only: boolean; request_revision: Revision | null; assessment_revision: AssessmentRevision };
type Decision = { id: string; kind: 'check_date' | 'current_contract'; version: number; record_id: string | null;
  employee_id: string | null; value: string; reason: string; actor: string; created_at: string; dependency_signature: string };
type DecisionsPayload = Page<Decision> & { request_decision: Decision | null; read_only: boolean; check_date: string;
  check_date_version: number; current_contract: Decision | null; current_contract_version: number; current_contract_valid: boolean;
  contract_dependency_signature: string; contract_file_ids: string[]; assessment_revision: AssessmentRevision };
type Result = { id: string; result_revision: string; analysis_id: string; input_revision: string; check_date: string; evaluated_at: string };
type ResultsPayload = Page<Result> & { request_result: Result | null; assessment_revision: AssessmentRevision };

type FactBasis = Pick<Fact, 'version' | 'owner_signature' | 'source_signature' | 'context_signature'>;
const factBasis = (fact: Fact): FactBasis => ({ version: fact.version, owner_signature: fact.owner_signature,
  source_signature: fact.source_signature, context_signature: fact.context_signature });
export type FactRevisionDraft = { value: FactValue; reason: string; reconsidered?: boolean; rawList?: string; basis?: FactBasis };
export type FactRevisionDrafts = Map<string, FactRevisionDraft>;
export type FactRevisionRequests = Map<string, { id: string; body: Record<string, unknown>; inFlight: boolean }>;
export type AssessmentDecisionDrafts = Map<string, { value: string; reason: string; reconsidered?: boolean; serverValue?: string; basisSignature?: string }>;
export type AssessmentDecisionRequests = Map<string, { id: string; body: Record<string, unknown>; inFlight: boolean }>;
export type ReevaluationRequests = Map<string, { id: string; expectedInputRevision: string; inFlight: boolean }>;

const FACT_LABELS: Record<string, string> = {
  'employment.contract.employer': '合同用人单位', 'employment.contract.end_date': '合同结束日期',
  'employment.contract.exists': '书面劳动合同', 'employment.contract.start_date': '合同开始日期',
  'employment.contract.term_readable': '合同期限可识别', 'employment.contract.type': '合同类型',
  'employment.entities': '材料中的用工主体', 'employment.entity_mismatch_explained': '用工主体差异已有说明',
  'employment.evidence_after_contract_end': '合同期满后仍有用工证据', 'employment.probation.assessment_exists': '试用期考核记录',
  'employment.probation.end_date': '试用期结束日期', 'employment.probation.periods': '历次试用期区间',
  'employment.probation.start_date': '试用期开始日期', 'employment.social_insurance.entity': '社保缴纳主体',
  'employment.social_insurance.period_matches': '社保期间与在职期间一致', 'employment.social_insurance.present': '存在社保记录',
  'employment.social_insurance.waiver_language': '存在放弃社保表述', 'employment.start_date': '实际入职日期',
  'employment.status': '用工状态', 'employment.termination.delivery_exists': '解除通知送达记录',
  'employment.termination.notice_exists': '解除通知', 'employment.termination.occurred': '劳动关系已解除',
  'employment.termination.settlement_materials': '离职交接材料',
};
export const OPTION_LABELS: Record<string, string> = {
  active: '在职', probation: '试用期', terminated: '离职', unknown: '待确认', fixed: '固定期限',
  fixed_term: '固定期限', indefinite: '无固定期限', open_ended: '无固定期限', non_fixed: '无固定期限',
  task: '以完成任务为期限', project: '项目期限', completion_of_task: '以完成任务为期限',
  final_pay: '工资结清材料', separation_certificate: '离职证明', handover: '交接材料',
  item_handover: '物品交接', work_handover: '工作交接',
};

export function factLabel(type: string) { return FACT_LABELS[type] ?? `已识别事实（${type}）`; }
function valueText(value: FactValue): string {
  if (value === null) return '未知';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) {
    if (value.every(row => Array.isArray(row))) return (value as [string, string][]).map(row => `${row[0]} 至 ${row[1]}`).join('；') || '空列表';
    return (value as string[]).map(item => OPTION_LABELS[item] ?? item).join('；') || '空列表';
  }
  return OPTION_LABELS[value] ?? value;
}
function safeId(value: string) { return value.length <= 12 ? value : `${value.slice(0, 6)}…${value.slice(-4)}`; }
function locationText(source: Source) {
  const labels: Record<string, string> = { page: '页', paragraph: '段', image: '张图片', row: '行', column: '列', table: '表', sheet: '工作表', cell: '单元格' };
  const parts = Object.entries(source.location).filter(([key]) => key !== '_grounding')
    .map(([key, value]) => typeof value === 'number' && ['page', 'paragraph', 'image', 'row', 'table'].includes(key)
      ? `第 ${String(value)} ${labels[key]}` : `${labels[key] ?? key} ${String(value)}`);
  return parts.join(' · ') || '材料内位置待核对';
}
function isValidValue(spec: ValueSpec, value: FactValue): boolean {
  if (value === null) return Boolean(spec.nullable);
  if (spec.input_type === 'date') {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const [year, month, day] = value.split('-').map(Number); const parsed = new Date(Date.UTC(year, month - 1, day));
    return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
  }
  if (spec.input_type === 'boolean') return typeof value === 'boolean';
  if (spec.input_type === 'enum') return typeof value === 'string' && Boolean(spec.options?.includes(value));
  if (spec.input_type === 'text') return typeof value === 'string' && Boolean(value.trim()) && value.length <= (spec.max_length ?? 200);
  if (spec.input_type === 'list') return Array.isArray(value) && value.length <= (spec.max_items ?? 50)
    && value.every(item => typeof item === 'string' && Boolean(item.trim()) && item.length <= (spec.max_length ?? 200)
      && (!spec.options?.length || spec.options.includes(item)));
  if (spec.input_type === 'periods') return Array.isArray(value) && value.every(row => Array.isArray(row) && row.length === 2
    && isValidValue({ editable: true, input_type: 'date' }, row[0]) && isValidValue({ editable: true, input_type: 'date' }, row[1]) && row[0] <= row[1])
    && value.length <= (spec.max_items ?? 50);
  return false;
}

function rawListValue(fact: Fact, value: FactValue) {
  return fact.value_spec.input_type === 'list' && !fact.value_spec.options?.length && Array.isArray(value)
    ? (value as string[]).join('\n') : '';
}

function ValueControl({ fact, value, rawList, disabled, onChange }: { fact: Fact; value: FactValue; rawList?: string;
  disabled: boolean; onChange: (value: FactValue, rawList?: string) => void }) {
  const label = `${factLabel(fact.fact_type)}当前值`; const spec = fact.value_spec;
  if (spec.input_type === 'boolean') return <select aria-label={label} value={value === null ? 'unknown' : value ? 'yes' : 'no'} disabled={disabled}
    onChange={event => onChange(event.target.value === 'unknown' ? null : event.target.value === 'yes')}>
    <option value="unknown">未知</option><option value="yes">是</option><option value="no">否</option></select>;
  if (spec.input_type === 'enum') return <select aria-label={label} value={value === null ? '' : String(value)} disabled={disabled}
    onChange={event => onChange(event.target.value || null)}><option value="">未知</option>
    {(spec.options ?? []).map(option => <option key={option} value={option}>{OPTION_LABELS[option] ?? option}</option>)}</select>;
  if (spec.input_type === 'list') {
    const items = Array.isArray(value) ? value as string[] : [];
    return <div className="list-editor"><select aria-label={`${label}状态`} value={value === null ? 'unknown' : 'known'} disabled={disabled}
      onChange={event => onChange(event.target.value === 'unknown' ? null : [], spec.options?.length ? undefined : '')}>
      <option value="unknown">未知</option><option value="known">已核对列表</option></select>
      {value !== null && (spec.options?.length ? <fieldset aria-label={label}>{spec.options.map(option => <label key={option}>
        <input type="checkbox" checked={items.includes(option)} disabled={disabled} onChange={event => onChange(event.target.checked
          ? [...items, option] : items.filter(item => item !== option))} />{OPTION_LABELS[option] ?? option}</label>)}</fieldset>
        : <textarea aria-label={label} disabled={disabled} value={rawList ?? items.join('\n')} placeholder="每行一项；可保留明确核对后的空列表"
          maxLength={(spec.max_items ?? 50) * (spec.max_length ?? 200)} onChange={event => onChange(event.target.value
            ? event.target.value.split('\n').map(item => item.trim()).filter(Boolean) : [], event.target.value)} />)}</div>;
  }
  if (spec.input_type === 'periods') {
    const rows = Array.isArray(value) ? value as [string, string][] : [];
    return <div className="period-editor" aria-label={label}><select aria-label={`${label}状态`} value={value === null ? 'unknown' : 'known'} disabled={disabled}
      onChange={event => onChange(event.target.value === 'unknown' ? null : [])}><option value="unknown">未知</option><option value="known">已核对期间</option></select>
      {value !== null ? <>{rows.map((row, index) => <div key={index} className="period-row">
      <input type="date" aria-label={`第 ${index + 1} 段开始日期`} value={row[0]} disabled={disabled} onChange={event => {
        const next = rows.map(item => [...item] as [string, string]); next[index][0] = event.target.value; onChange(next);
      }} /><span>至</span><input type="date" aria-label={`第 ${index + 1} 段结束日期`} value={row[1]} disabled={disabled} onChange={event => {
        const next = rows.map(item => [...item] as [string, string]); next[index][1] = event.target.value; onChange(next);
      }} /><button type="button" disabled={disabled} onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}>删除此段</button>
    </div>)}<button type="button" disabled={disabled || rows.length >= (spec.max_items ?? 50)} onClick={() => onChange([...rows, ['', '']])}>添加期间</button></> : null}</div>;
  }
  return <input aria-label={label} type={spec.input_type === 'date' ? 'date' : 'text'} disabled={disabled}
    value={value === null ? '' : String(value)} maxLength={spec.max_length ?? 200} onChange={event => onChange(event.target.value || null)} />;
}

function FactCard({ fact, base, api, drafts, requests, readOnly, dependencyBusy, confirmationContext, refresh,
  onDependencyBusy, onInputsCommitted }: { fact: Fact; base: string; api: DesktopRequest;
  drafts: FactRevisionDrafts; requests: FactRevisionRequests; readOnly: boolean; confirmationContext: string; refresh: () => Promise<FactsPayload>;
  dependencyBusy: boolean; onDependencyBusy: (key: string, busy: boolean) => void;
  onInputsCommitted: (revision: AssessmentRevision) => void | Promise<void> }) {
  const key = `${base}/${fact.record_id}/${fact.id}`;
  const initial = drafts.get(key);
  const [value, setValue] = useState<FactValue>(initial ? initial.value : fact.effective_value);
  const [rawList, setRawList] = useState(initial?.rawList ?? rawListValue(fact, initial ? initial.value : fact.effective_value));
  const [reason, setReason] = useState(initial?.reason ?? '');
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState(''); const [error, setError] = useState('');
  const [explicitConflict, setConflict] = useState(Boolean(initial && initial.reconsidered === false));
  const authored = drafts.get(key);
  const basisChanged = Boolean(authored && JSON.stringify(authored.basis) !== JSON.stringify(factBasis(fact)));
  const conflict = explicitConflict || basisChanged;
  const [serverValue, setServerValue] = useState<FactValue>(fact.effective_value);
  const [history, setHistory] = useState<Page<Revision> | null>(null);
  const [expanded, setExpanded] = useState(Boolean(initial || requests.has(key)));
  const pending = requests.get(key); const effectiveReadOnly = readOnly || fact.read_only || !fact.value_spec.editable;
  useEffect(() => {
    if (!drafts.has(key) && !requests.has(key) && !conflict) {
      setValue(fact.effective_value); setRawList(rawListValue(fact, fact.effective_value)); setServerValue(fact.effective_value);
    }
  }, [conflict, drafts, fact.effective_value, fact.revision_valid, fact.version, key, requests]);
  function store(nextValue = value, nextReason = reason, reconsidered = !conflict, nextRawList = rawList, adopt = false) {
    drafts.set(key, { value: nextValue, reason: nextReason, reconsidered,
      basis: adopt ? factBasis(fact) : drafts.get(key)?.basis ?? (drafts.has(key) ? undefined : factBasis(fact)),
      ...(fact.value_spec.input_type === 'list' && !fact.value_spec.options?.length ? { rawList: nextRawList } : {}) });
  }
  async function loadHistory(page = 1) {
    setBusy(true); setError('');
    try { setHistory(await readJson<Page<Revision>>(api(`${base}/effective-facts/${fact.id}/revisions?page=${page}&page_size=20`))); }
    catch (failure) { setError(describeOperationError(safeErrorCode(failure))); } finally { setBusy(false); }
  }
  async function reconcile() {
    const target = requests.get(key); if (!target || busy) return;
    onDependencyBusy(key, true); setBusy(true); setError('');
    try {
      const params = new URLSearchParams({ record_id: fact.record_id, request_id: target.id, page: '1', page_size: '50' });
      const payload = await readJson<FactsPayload>(api(`${base}/effective-facts?${params}`));
      if (!payload.request_revision) {
        setMessage('原请求仍可能在处理中，暂未查到记录；请稍后再次核对。'); return;
      }
      requests.delete(key);
      if (payload.request_revision?.id === target.id) {
        drafts.delete(key); setValue(payload.request_revision.value); setRawList(rawListValue(fact, payload.request_revision.value));
        setReason(''); setConflict(false); const refreshed = await refresh(); await onInputsCommitted(refreshed.assessment_revision);
        setMessage('事实修订已核实保存。');
      } else {
        const current = payload.items.find(item => item.id === fact.id);
        if (current) setServerValue(current.effective_value);
        setConflict(true); store(value, reason, false);
        setMessage('未查到该请求，已读取服务器当前事实；请重新核对后再明确保存。');
      }
    } catch (failure) { setError(describeOperationError(safeErrorCode(failure))); }
    finally { onDependencyBusy(key, requests.has(key)); setBusy(false); }
  }
  async function save(kind: 'correct' | 'confirm') {
    if (busy || dependencyBusy || pending || effectiveReadOnly || !reason.trim() || conflict || (kind === 'confirm' && fact.effective_value === null)) return;
    const id = crypto.randomUUID();
    const body: Record<string, unknown> = { id, expected_version: fact.version, kind, reason: reason.trim(),
      expected_owner_signature: fact.owner_signature, expected_source_signature: fact.source_signature,
      expected_context_signature: fact.context_signature };
    if (kind === 'correct') body.value = value; else body.value = fact.effective_value;
    const target = { id, body, inFlight: true }; requests.set(key, target); onDependencyBusy(key, true); setBusy(true); setMessage(''); setError('');
    try {
      const saved = await readJson<Fact>(api(`${base}/effective-facts/${fact.id}/revisions`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
      requests.delete(key); drafts.delete(key); setValue(saved.effective_value); setRawList(rawListValue(fact, saved.effective_value));
      setReason(''); setConflict(false); const refreshed = await refresh(); await onInputsCommitted(refreshed.assessment_revision);
      setMessage(`已保存事实版本 ${saved.version}；结果待重新评估。`);
    } catch (failure) {
      const code = safeErrorCode(failure); setError(describeOperationError(code));
      if (!['DESKTOP_REQUEST_TIMEOUT', 'DESKTOP_CONNECTION_FAILED', 'DESKTOP_REQUEST_CANCELLED'].includes(code)) requests.delete(key);
      if (['FACT_VERSION_CONFLICT', 'FACT_OWNERSHIP_INVALID', 'FACT_SOURCE_INVALID'].includes(code)) {
        const payload = await refresh().catch(() => null); const current = payload?.items.find(item => item.id === fact.id);
        setServerValue(current?.effective_value ?? fact.effective_value); setConflict(true); store(value, reason, false);
      }
    } finally { if (!requests.has(key)) target.inFlight = false; onDependencyBusy(key, requests.has(key)); setBusy(false); }
  }
  const changed = JSON.stringify(value) !== JSON.stringify(fact.effective_value);
  const invalid = !isValidValue(fact.value_spec, value);
  return <article role="region" className={`fact-card${fact.selected_support ? '' : ' fact-retained'}`} aria-label={factLabel(fact.fact_type)}>
    <header><div><h4>{factLabel(fact.fact_type)}</h4><p>{fact.filename || `材料 ${safeId(fact.file_id)}`} · 版本 {fact.version}</p></div>
      <span className="status-pill">{fact.selected_support ? '当前参与材料' : '保留历史 / 未纳入当前合同'}</span></header>
    <div className="fact-preview"><span>原始：{valueText(fact.original_value)}</span><span>有效：{valueText(fact.effective_value)}</span>
      <span>{fact.sources.some(source => source.provenance === 'locally_located') ? '有本地定位来源' : '原文未定位'}</span></div>
    <button type="button" className="text-action fact-toggle" aria-expanded={expanded} onClick={() => setExpanded(open => !open)}>
      {expanded ? '收起核对详情' : `展开核对${factLabel(fact.fact_type)}`}</button>
    {expanded ? <div className="fact-detail">
    <dl className="fact-values"><div><dt>原始识别</dt><dd>原始识别值：{valueText(fact.original_value)}</dd></div>
      <div><dt>当前有效</dt><dd>{valueText(fact.effective_value)}</dd></div>
      <div><dt>人工确认</dt><dd>{fact.human_confirmed && fact.revision_valid ? '人工已确认（并未改写原始来源）' : '尚未有效确认'}</dd></div></dl>
    {fact.basis_pending ? <p className="inline-warning">当前合同或试用期上下文仍待确认；本事实不会被静默套用到新合同。</p> : null}
    {fact.confirmation_context === 'current_contract_period' ? <p>该确认绑定于{confirmationContext}；合同选择或期间变化后必须重新核对。</p> : null}
    {!fact.source_valid ? <p role="alert">来源完整性校验失败，本事实只读且不能用于本地重新评估。</p> : null}
    <div className="fact-sources">{fact.sources.length ? fact.sources.map(source => <div key={source.id}>
      <p>{source.provenance === 'locally_located' ? `本地已定位 · ${locationText(source)}` : '原文未定位，需查看原材料；人工确认不等于解析器已定位。'}</p>
      {source.excerpt ? <blockquote>{source.excerpt}</blockquote> : null}
    </div>) : <p>暂无可展示的来源定位；本页不会因此创建无来源事实。</p>}</div>
    {fact.value_spec.input_type === 'periods' ? <section className="retained-periods" aria-label="保留的历次试用期">
      <strong>已保留的历次期间</strong>{Array.isArray(fact.effective_value) && fact.effective_value.length
        ? (fact.effective_value as [string, string][]).map((row, index) => <p key={index}>{row[0]} 至 {row[1]}</p>) : <p>尚无可列示期间</p>}
    </section> : null}
    {!effectiveReadOnly ? <form className="fact-form" onSubmit={event => { event.preventDefault(); void save('correct'); }}>
      <label>{factLabel(fact.fact_type)}当前值<ValueControl fact={fact} value={value} rawList={rawList} disabled={busy || dependencyBusy || Boolean(pending)} onChange={(next, nextRawList) => {
        setValue(next); if (nextRawList !== undefined) setRawList(nextRawList); setMessage(''); store(next, reason, !conflict, nextRawList ?? rawList);
      }} /></label>
      <label>更正或确认理由<textarea aria-label="更正或确认理由" maxLength={500} value={reason} disabled={busy || dependencyBusy || Boolean(pending)}
        onChange={event => { setReason(event.target.value); store(value, event.target.value, !conflict); }} /></label>
      {invalid ? <p className="inline-warning">当前值不是有效的日期、选项或期间；未知值请使用“未知”或留空。</p> : null}
      <div className="fact-actions"><button type="submit" disabled={busy || dependencyBusy || Boolean(pending) || conflict || !reason.trim() || invalid}>保存更正</button>
        <button type="button" disabled={busy || dependencyBusy || Boolean(pending) || conflict || !reason.trim() || changed || fact.effective_value === null}
          onClick={() => void save('confirm')}>确认当前识别值</button>
        <button type="button" disabled={busy || Boolean(pending) || !drafts.has(key)} onClick={() => {
          drafts.delete(key); setValue(fact.effective_value); setRawList(rawListValue(fact, fact.effective_value));
          setReason(''); setConflict(false); setMessage('已放弃未保存修改。');
        }}>放弃未保存修改</button></div>
    </form> : <p>此事实只读，不提供变更操作。</p>}
    {conflict ? <section className="conflict-panel" aria-label="事实冲突核对"><strong>保存前必须重新核对</strong>
      <p>服务器当前值：{valueText(basisChanged ? fact.effective_value : serverValue)}</p><p>你的草稿值：{valueText(value)}</p>
      <button type="button" disabled={busy || dependencyBusy || Boolean(pending)} onClick={() => { setConflict(false); store(value, reason, true, rawList, true); setMessage('已标记重新核对；再次保存将使用刚读取的版本。'); }}>我已重新核对，允许再次保存</button>
    </section> : null}
    {pending ? <section><p>提交结果尚未核实；草稿与请求编号分别保留，不会自动重复提交。</p>
      <button type="button" disabled={busy} onClick={() => void reconcile()}>核对事实提交结果</button></section> : null}
    <button type="button" className="text-action" disabled={busy} onClick={() => history ? setHistory(null) : void loadHistory()}>
      {history ? '收起修订历史' : '查看修订历史'}</button>
    {history ? <section aria-label="事实修订历史">{history.items.length ? history.items.map(item => <p key={item.id}>
      版本 {item.version} · {item.kind === 'confirm' ? '确认' : '更正'} · {valueText(item.value)} · {item.reason} · {item.created_at.replace('T', ' ')}</p>)
      : <p>尚无人工修订。</p>}{history.pages > 1 ? <div className="pager">
        <button type="button" disabled={busy || history.page <= 1} onClick={() => void loadHistory(history.page - 1)}>上一页修订</button>
        <span>第 {history.page} / {history.pages} 页</span>
        <button type="button" disabled={busy || history.page >= history.pages} onClick={() => void loadHistory(history.page + 1)}>下一页修订</button>
      </div> : null}</section> : null}
    {message ? <p role="status">{message}</p> : null}{error ? <p role="alert">{error}</p> : null}
    </div> : null}
  </article>;
}

export function EmployeeFactWorkbench({ companyId, analysisId, recordId, api, factDrafts, factRequests,
  decisionDrafts, decisionRequests, reevaluationRequests, readOnly = false, onInputsCommitted = () => undefined, onReevaluated }: {
  companyId: string; analysisId: string; recordId: string; api: DesktopRequest; factDrafts: FactRevisionDrafts;
  factRequests: FactRevisionRequests; decisionDrafts: AssessmentDecisionDrafts; decisionRequests: AssessmentDecisionRequests;
  reevaluationRequests: ReevaluationRequests; readOnly?: boolean; onInputsCommitted?: (revision: AssessmentRevision) => void | Promise<void>;
  onReevaluated: (revision: AssessmentRevision) => void | Promise<void>;
}) {
  const base = `/api/company-workspaces/${companyId}/analyses/${analysisId}`;
  const factScope = `${base}/${recordId}/`;
  const [facts, setFacts] = useState<FactsPayload | null>(null); const [decisions, setDecisions] = useState<DecisionsPayload | null>(null);
  const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const checkKey = `${base}/check_date`; const contractKey = `${base}/${recordId}/current_contract`; const resultKey = `${base}/reevaluate`;
  const [checkDate, setCheckDate] = useState(decisionDrafts.get(checkKey)?.value ?? '');
  const [checkReason, setCheckReason] = useState(decisionDrafts.get(checkKey)?.reason ?? '');
  const [contract, setContract] = useState(decisionDrafts.get(contractKey)?.value ?? '');
  const [contractReason, setContractReason] = useState(decisionDrafts.get(contractKey)?.reason ?? '');
  const [decisionConflict, setDecisionConflict] = useState<'' | 'check_date' | 'current_contract'>('');
  const [currentDecisionServer, setCurrentDecisionServer] = useState('');
  const [factPage, setFactPage] = useState(1); const [decisionPage, setDecisionPage] = useState(1);
  const [factMutationKeys, setFactMutationKeys] = useState<Set<string>>(() => new Set(
    [...factRequests.keys()].filter(key => key.startsWith(factScope))));
  const [basisRefreshRequired, setBasisRefreshRequired] = useState(false);
  const assessment = facts?.assessment_revision ?? decisions?.assessment_revision;
  const factBasisBusy = factMutationKeys.size > 0 || basisRefreshRequired;

  async function fetchFacts(page = factPage) {
    const params = new URLSearchParams({ record_id: recordId, page: String(page), page_size: '50' });
    const payload = await readJson<FactsPayload>(api(`${base}/effective-facts?${params}`)); setFacts(payload); setFactPage(payload.page); return payload;
  }
  async function fetchDecisions(page = decisionPage) {
    const params = new URLSearchParams({ record_id: recordId, page: String(page), page_size: '20' });
    const payload = await readJson<DecisionsPayload>(api(`${base}/assessment-decisions?${params}`));
    checkAuthoredContractBasis(payload); setDecisions(payload); setDecisionPage(payload.page); return payload;
  }
  async function loadPage(kind: 'facts' | 'decisions', page: number) {
    if (busy) return; setBusy(true); setError('');
    try { if (kind === 'facts') await fetchFacts(page); else await fetchDecisions(page); }
    catch (failure) { setError(describeOperationError(safeErrorCode(failure))); }
    finally { setBusy(false); }
  }
  function setFactDependencyBusy(key: string, nextBusy: boolean) {
    setFactMutationKeys(current => { const next = new Set(current); if (nextBusy) next.add(key); else next.delete(key); return next; });
  }
  async function refreshAll() { const [nextFacts] = await Promise.all([fetchFacts(), fetchDecisions()]); return nextFacts; }
  function checkAuthoredContractBasis(payload: DecisionsPayload) {
    const authored = decisionDrafts.get(contractKey);
    if (authored && !decisionRequests.has(contractKey) && authored.basisSignature !== payload.contract_dependency_signature) {
      const serverValue = payload.current_contract?.value ?? '尚未选择';
      // A fresh projection is not consent to rebase the authored draft, including after remount.
      decisionDrafts.set(contractKey, { ...authored, reconsidered: false, serverValue });
      if (decisionDrafts.get(checkKey)?.reconsidered !== false) {
        setDecisionConflict('current_contract'); setCurrentDecisionServer(serverValue);
      }
      setMessage('事实依据已更新；已保留当前合同草稿，请对照新的事实依据重新核对。');
    }
  }
  async function refreshAfterFactCommit() {
    try {
      const nextFacts = await refreshAll();
      setBasisRefreshRequired(false);
      return nextFacts;
    } catch (failure) {
      setBasisRefreshRequired(true); setMessage('事实已保存，但决定依据尚未重新读取；完成核对前不会允许提交新决定。');
      throw failure;
    }
  }
  async function recoverFactBasis() {
    if (busy || factMutationKeys.size) return; setBusy(true); setError('');
    try { await refreshAfterFactCommit(); setMessage('事实与决定依据已重新读取。'); }
    catch (failure) { setError(describeOperationError(safeErrorCode(failure))); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    let alive = true; const controller = new AbortController();
    const savedCheck = decisionDrafts.get(checkKey); const savedContract = decisionDrafts.get(contractKey);
    setCheckDate(savedCheck?.value ?? ''); setCheckReason(savedCheck?.reason ?? '');
    setContract(savedContract?.value ?? ''); setContractReason(savedContract?.reason ?? '');
    const savedConflict = savedCheck?.reconsidered === false ? 'check_date' : savedContract?.reconsidered === false ? 'current_contract' : '';
    setDecisionConflict(savedConflict); setCurrentDecisionServer(savedConflict === 'check_date'
      ? savedCheck?.serverValue ?? '' : savedConflict === 'current_contract' ? savedContract?.serverValue ?? '' : '');
    setFactPage(1); setDecisionPage(1);
    setFactMutationKeys(new Set([...factRequests.keys()].filter(key => key.startsWith(factScope))));
    const factParams = new URLSearchParams({ record_id: recordId, page: '1', page_size: '50' });
    const decisionParams = new URLSearchParams({ record_id: recordId, page: '1', page_size: '20' });
    Promise.all([readJson<FactsPayload>(api(`${base}/effective-facts?${factParams}`, { signal: controller.signal })),
      readJson<DecisionsPayload>(api(`${base}/assessment-decisions?${decisionParams}`, { signal: controller.signal }))])
      .then(([factPayload, decisionPayload]) => { if (!alive) return; setFacts(factPayload); setDecisions(decisionPayload); setError('');
        checkAuthoredContractBasis(decisionPayload);
        if (!decisionDrafts.has(checkKey)) setCheckDate(decisionPayload.check_date);
        if (!decisionDrafts.has(contractKey)) setContract(decisionPayload.current_contract_valid ? decisionPayload.current_contract?.value ?? '' : '');
      }).catch(failure => { if (alive) setError(describeOperationError(safeErrorCode(failure))); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; controller.abort(); };
  }, [api, base, recordId]);
  const files = useMemo(() => {
    const names = new Map<string, string>();
    for (const fact of facts?.items ?? []) if (!names.has(fact.file_id)) names.set(fact.file_id, fact.filename || `材料 ${safeId(fact.file_id)}`);
    return names;
  }, [facts]);
  const confirmationContext = useMemo(() => {
    const contractDecision = decisions?.current_contract;
    const contractName = contractDecision && decisions?.current_contract_valid
      ? files.get(contractDecision.value) ?? `材料 ${safeId(contractDecision.value)}` : '待确认的当前合同';
    const currentValue = (type: string) => {
      const values = (facts?.items ?? []).filter(fact => fact.fact_type === type && fact.selected_support && !fact.basis_pending)
        .map(fact => fact.effective_value).filter((value): value is string => typeof value === 'string');
      return new Set(values).size === 1 ? values[0] : '';
    };
    const start = currentValue('employment.probation.start_date'); const end = currentValue('employment.probation.end_date');
    return `${contractName} · 当前试用期${start && end ? ` ${start} 至 ${end}` : '区间待确认'}`;
  }, [decisions, facts, files]);
  function setDecisionDraft(key: string, value: string, reason: string,
    reconsidered = decisionDrafts.get(key)?.reconsidered ?? true,
    serverValue = decisionDrafts.get(key)?.serverValue, basisSignature?: string) {
    decisionDrafts.set(key, { value, reason, reconsidered, ...(serverValue === undefined ? {} : { serverValue }),
      ...(key === contractKey ? { basisSignature: basisSignature ?? decisionDrafts.get(key)?.basisSignature ?? decisions?.contract_dependency_signature } : {}) });
  }
  async function saveDecision(kind: 'check_date' | 'current_contract') {
    if (!decisions || busy || factBasisBusy || decisionConflict) return;
    const key = kind === 'check_date' ? checkKey : contractKey; const value = kind === 'check_date' ? checkDate : contract;
    const reason = kind === 'check_date' ? checkReason : contractReason; if (!value || !reason.trim() || decisionRequests.has(key)) return;
    const id = crypto.randomUUID(); const body: Record<string, unknown> = { id,
      expected_version: kind === 'check_date' ? decisions.check_date_version : decisions.current_contract_version,
      kind, value, reason: reason.trim() };
    if (kind === 'current_contract') { body.record_id = recordId; body.expected_dependency_signature = decisions.contract_dependency_signature; }
    const target = { id, body, inFlight: true }; decisionRequests.set(key, target); setBusy(true); setError(''); setMessage('');
    try {
      const saved = await readJson<Decision>(api(`${base}/assessment-decisions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
      decisionRequests.delete(key); decisionDrafts.delete(key); const refreshed = await refreshAll();
      await onInputsCommitted(refreshed.assessment_revision);
      if (kind === 'check_date') { setCheckDate(saved.value); setCheckReason(''); } else { setContract(saved.value); setContractReason(''); }
      setMessage(`${kind === 'check_date' ? '核查日期' : '当前合同'}已保存；结果待重新评估。`);
    } catch (failure) {
      const code = safeErrorCode(failure); setError(describeOperationError(code));
      if (!['DESKTOP_REQUEST_TIMEOUT', 'DESKTOP_CONNECTION_FAILED', 'DESKTOP_REQUEST_CANCELLED'].includes(code)) decisionRequests.delete(key);
      if (['ASSESSMENT_VERSION_CONFLICT', 'ASSESSMENT_CONTRACT_INVALID', 'FACT_OWNERSHIP_INVALID'].includes(code)) {
        const latest = await fetchDecisions().catch(() => null); setDecisionConflict(kind);
        const serverValue = kind === 'check_date' ? latest?.check_date ?? decisions.check_date
          : latest?.current_contract?.value ?? '尚未选择';
        setCurrentDecisionServer(serverValue); setDecisionDraft(key, value, reason, false, serverValue,
          latest?.contract_dependency_signature ?? decisions.contract_dependency_signature);
      }
    } finally { if (!decisionRequests.has(key)) target.inFlight = false; setBusy(false); }
  }
  async function reconcileDecision(kind: 'check_date' | 'current_contract') {
    const key = kind === 'check_date' ? checkKey : contractKey; const target = decisionRequests.get(key); if (!target || busy) return;
    setBusy(true); setError('');
    try {
      const params = new URLSearchParams({ record_id: recordId, request_id: target.id, page: '1', page_size: '20' });
      const payload = await readJson<DecisionsPayload>(api(`${base}/assessment-decisions?${params}`)); setDecisions(payload);
      if (!payload.request_decision) {
        setMessage('原决定请求仍可能在处理中，请稍后再次核对。'); return;
      }
      decisionRequests.delete(key);
      if (payload.request_decision?.id === target.id) {
        decisionDrafts.delete(key);
        if (kind === 'check_date') { setCheckDate(payload.request_decision.value); setCheckReason(''); }
        else { setContract(payload.request_decision.value); setContractReason(''); }
        const refreshed = await refreshAll(); await onInputsCommitted(refreshed.assessment_revision);
        setMessage('评估决定已核实保存。');
      } else { const serverValue = kind === 'check_date' ? payload.check_date : payload.current_contract?.value ?? '尚未选择';
        setDecisionConflict(kind); setCurrentDecisionServer(serverValue);
        setDecisionDraft(key, kind === 'check_date' ? checkDate : contract, kind === 'check_date' ? checkReason : contractReason,
          false, serverValue, payload.contract_dependency_signature);
        setMessage('该请求编号对应其他决定；请对照服务器当前决定重新核对。'); }
    } catch (failure) { setError(describeOperationError(safeErrorCode(failure))); } finally { setBusy(false); }
  }
  async function reevaluate() {
    if (!assessment || busy || factBasisBusy || readOnly || assessment.read_only || reevaluationRequests.has(resultKey)) return;
    const target = { id: crypto.randomUUID(), expectedInputRevision: assessment.input_revision, inFlight: true };
    reevaluationRequests.set(resultKey, target); setBusy(true); setError(''); setMessage('');
    try {
      const updated = await readJson<AssessmentRevision>(api(`${base}/reevaluate`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: target.id, expected_input_revision: target.expectedInputRevision }) }));
      reevaluationRequests.delete(resultKey); await refreshAll(); await onReevaluated(updated);
      setMessage(`本地重新评估已完成；结果版本 ${updated.result_revision ?? '尚无'}。`);
    } catch (failure) { const code = safeErrorCode(failure); setError(describeOperationError(code));
      if (!['DESKTOP_REQUEST_TIMEOUT', 'DESKTOP_CONNECTION_FAILED', 'DESKTOP_REQUEST_CANCELLED'].includes(code)) reevaluationRequests.delete(resultKey);
    } finally { if (!reevaluationRequests.has(resultKey)) target.inFlight = false; setBusy(false); }
  }
  async function reconcileResult() {
    const target = reevaluationRequests.get(resultKey); if (!target || busy) return;
    setBusy(true); setError('');
    try {
      const payload = await readJson<ResultsPayload>(api(`${base}/assessment-results?request_id=${encodeURIComponent(target.id)}&page=1&page_size=20`));
      if (!payload.request_result) {
        setMessage('原重新评估仍可能在处理中，请稍后再次核对。'); return;
      }
      reevaluationRequests.delete(resultKey);
      if (payload.request_result?.id === target.id) { setFacts(old => old ? { ...old, assessment_revision: payload.assessment_revision } : old);
        await refreshAll(); await onReevaluated(payload.assessment_revision); setMessage(`本地重新评估已核实完成；结果版本 ${payload.request_result.result_revision}。`); }
      else setMessage('未查到该重新评估请求；已读取当前结果，若仍需评估请再次明确操作。');
    } catch (failure) { setError(describeOperationError(safeErrorCode(failure))); } finally { setBusy(false); }
  }
  if (loading) return <section className="fact-workbench"><p>正在读取该员工已识别事实…</p></section>;
  if (!facts || !decisions) return <section className="fact-workbench"><h3>已识别事实与当前评估</h3>
    <p role="alert">事实读取失败：{error || '操作未完成'}。原始材料与已保存修订未被改写。</p>
    <button type="button" onClick={() => { setLoading(true); void refreshAll().then(() => setError('')).catch(failure => setError(describeOperationError(safeErrorCode(failure)))).finally(() => setLoading(false)); }}>重读事实</button></section>;
  const effectiveReadOnly = readOnly || facts.read_only || decisions.read_only;
  const currentAssessment = facts.assessment_revision;
  const pendingCheck = decisionRequests.get(checkKey); const pendingContract = decisionRequests.get(contractKey); const pendingResult = reevaluationRequests.get(resultKey);
  return <section className="fact-workbench" aria-labelledby="fact-workbench-title">
    <div className="section-heading"><div><p className="eyebrow">人工事实核对</p><h3 id="fact-workbench-title">已识别事实与当前评估</h3>
      <p className="muted">只修订已有识别事实；原始值与来源始终保留。资料不足不等于无风险。</p></div></div>
    <AssessmentRevisionStatus revision={assessment} />
    {effectiveReadOnly ? <p>历史事实与修订记录只读；当前页面不会提供保存或重新评估操作。</p> : null}
    {basisRefreshRequired ? <section className="conflict-panel" role="alert"><p>事实已经保存，但当前决定仍可能基于旧事实依据。</p>
      <button type="button" disabled={busy || factMutationKeys.size > 0} onClick={() => void recoverFactBasis()}>重新读取事实与决定依据</button></section> : null}
    <section className="assessment-controls" aria-label="当前评估决定">
      <h4>当前评估依据</h4>
      <p>{decisions.assessment_revision.check_date_explicit ? '已设置核查日期' : '核查日期来自历史创建日期回退；这不表示你主动选择过该日期。'}</p>
      {!effectiveReadOnly ? <div className="decision-grid"><label>核查日期<input type="date" aria-label="核查日期" value={checkDate} disabled={busy || factBasisBusy || Boolean(pendingCheck)} onChange={event => {
        setCheckDate(event.target.value); setDecisionDraft(checkKey, event.target.value, checkReason);
      }} /></label><label>核查日期变更理由<textarea aria-label="核查日期变更理由" maxLength={500} value={checkReason} disabled={busy || factBasisBusy || Boolean(pendingCheck)} onChange={event => {
        setCheckReason(event.target.value); setDecisionDraft(checkKey, checkDate, event.target.value);
      }} /></label><button type="button" disabled={busy || factBasisBusy || Boolean(pendingCheck) || decisionConflict === 'check_date' || !checkDate || !checkReason.trim()} onClick={() => void saveDecision('check_date')}>保存核查日期</button>
      <label>当前合同材料<select aria-label="当前合同材料" value={contract} disabled={busy || factBasisBusy || Boolean(pendingContract)} onChange={event => {
        setContract(event.target.value); setDecisionDraft(contractKey, event.target.value, contractReason);
      }}><option value="">请明确选择当前合同</option>{decisions.contract_file_ids.map(fileId => <option key={fileId} value={fileId}>{files.get(fileId) ?? `材料 ${safeId(fileId)}`}</option>)}</select></label>
      <label>当前合同选择理由<textarea aria-label="当前合同选择理由" maxLength={500} value={contractReason} disabled={busy || factBasisBusy || Boolean(pendingContract)} onChange={event => {
        setContractReason(event.target.value); setDecisionDraft(contractKey, contract, event.target.value);
      }} /></label><button type="button" disabled={busy || factBasisBusy || Boolean(pendingContract) || decisionConflict === 'current_contract' || !contract || !contractReason.trim()} onClick={() => void saveDecision('current_contract')}>保存当前合同选择</button></div> : null}
      {decisions.current_contract ? <p>{decisions.current_contract_valid ? '当前参与材料' : '已保存选择因材料或事实变化而失效'}：{files.get(decisions.current_contract.value) ?? `材料 ${safeId(decisions.current_contract.value)}`}
        {decisions.current_contract.reason ? ` · ${decisions.current_contract.reason}` : ''}</p> : <p>尚未明确选择当前合同；系统不会按最新上传顺序自动猜测。</p>}
      {decisionConflict ? <section className="conflict-panel"><p>服务器当前决定：{files.get(currentDecisionServer) ?? currentDecisionServer}</p>
        <p>你的草稿决定：{decisionConflict === 'check_date' ? checkDate : files.get(contract) ?? contract}</p>
        {decisionConflict === 'current_contract' ? <p>当前事实依据版本：{safeId(decisions.contract_dependency_signature)}</p> : null}
        <button type="button" onClick={() => { const key = decisionConflict === 'check_date' ? checkKey : contractKey;
          const value = decisionConflict === 'check_date' ? checkDate : contract; const reason = decisionConflict === 'check_date' ? checkReason : contractReason;
          setDecisionDraft(key, value, reason, true, undefined, decisions.contract_dependency_signature);
          const otherKind = decisionConflict === 'check_date' ? 'current_contract' : 'check_date';
          const otherDraft = decisionDrafts.get(otherKind === 'check_date' ? checkKey : contractKey);
          setDecisionConflict(otherDraft?.reconsidered === false ? otherKind : '');
          setCurrentDecisionServer(otherDraft?.serverValue ?? ''); }}>我已重新核对评估决定</button></section> : null}
      {pendingCheck ? <button type="button" disabled={busy} onClick={() => void reconcileDecision('check_date')}>核对核查日期提交结果</button> : null}
      {pendingContract ? <button type="button" disabled={busy} onClick={() => void reconcileDecision('current_contract')}>核对当前合同提交结果</button> : null}
      <section className="decision-history" aria-label="评估决定历史"><h5>已保存的决定历史</h5>
        {decisions.items.length ? decisions.items.map(item => <p key={item.id}>{item.kind === 'check_date' ? '核查日期' : '当前合同'} · 版本 {item.version} · {
          item.kind === 'current_contract' ? files.get(item.value) ?? `材料 ${safeId(item.value)}` : item.value} · {item.reason} · {item.created_at.replace('T', ' ')}</p>)
          : <p>尚无可列示的已保存决定。</p>}
        {decisions.pages > 1 ? <div className="pager"><button type="button" disabled={busy || decisionPage <= 1} onClick={() => void loadPage('decisions', decisionPage - 1)}>上一页决定</button>
          <span>第 {decisionPage} / {decisions.pages} 页</span><button type="button" disabled={busy || decisionPage >= decisions.pages} onClick={() => void loadPage('decisions', decisionPage + 1)}>下一页决定</button></div> : null}
      </section>
    </section>
    <div className="fact-list">{facts.items.length ? facts.items.map(fact => <FactCard key={`${base}/${recordId}/${fact.id}`} fact={fact} base={base} api={api}
      drafts={factDrafts} requests={factRequests} readOnly={effectiveReadOnly}
      dependencyBusy={factBasisBusy || busy || Boolean(pendingContract)} onDependencyBusy={setFactDependencyBusy}
      confirmationContext={confirmationContext} refresh={refreshAfterFactCommit} onInputsCommitted={onInputsCommitted} />)
      : <p>尚无可人工核对的已识别事实；不能在此创建无来源的新事实。</p>}</div>
    {facts.pages > 1 ? <div className="pager" aria-label="事实分页"><button type="button" disabled={busy || factPage <= 1} onClick={() => void loadPage('facts', factPage - 1)}>上一页事实</button>
      <span>第 {factPage} / {facts.pages} 页 · 共 {facts.total} 项</span><button type="button" disabled={busy || factPage >= facts.pages} onClick={() => void loadPage('facts', factPage + 1)}>下一页事实</button></div> : null}
    {!effectiveReadOnly ? <section className="reevaluation-panel" aria-label="本地重新评估"><h4>刷新确定性结果</h4>
      <p>仅使用本机已保存事实运行规则，不调用模型、不消耗模型额度。保存事实或决定后，旧结果会保持“待重新评估”。</p>
      {currentAssessment.availability === 'none' ? <p className="inline-warning">当前没有可用证据；不会将缺失事实重新评估成全零风险。</p> : null}
      <button type="button" disabled={busy || factBasisBusy || Boolean(pendingResult) || currentAssessment.availability === 'none'} onClick={() => void reevaluate()}>仅用已保存事实重新评估</button>
      {pendingResult ? <><p>重新评估结果尚未核实，不会自动重复提交。</p><button type="button" disabled={busy} onClick={() => void reconcileResult()}>核对重新评估结果</button></> : null}
    </section> : null}
    {message ? <p role="status">{message}</p> : null}{error ? <p role="alert">{error}</p> : null}
  </section>;
}
