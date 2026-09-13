export type FindingSource = {
  id: string;
  file_id: string;
  file_name: string;
  locator_type: string;
  location: Record<string, unknown>;
  excerpt: string;
  provenance?: string;
};

export type FindingDetailData = {
  id: string;
  analysis_id: string;
  rule_id: string;
  title: string;
  severity: string;
  assessment_status: string;
  requires_human_review: boolean;
  summary: string;
  sources: FindingSource[];
  is_current?: boolean;
  retired_at?: string | null;
  review_status?: ReviewStatus;
  version?: number;
  missing_fact_types?: string[];
  recommended_actions?: string[];
  reviews?: { status: ReviewStatus; note: string; created_at: string }[];
};

type FindingDetailProps = {
  finding: FindingDetailData;
  onBack: () => void;
  onReview?: (input: FindingReviewInput) => Promise<void>;
  onReload?: () => Promise<void>;
  backLabel?: string;
};

const SEVERITY_LABELS: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
  info: '提示',
};

const STATUS_LABELS: Record<string, string> = {
  management_reminder: '管理提醒',
  confirmed_anomaly: '确定性异常',
  suspected_risk: '疑似风险',
  insufficient_data: '资料不足',
  requires_human_review: '需要人工复核',
};

function locatorText(location: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof location.sheet === 'string' && location.sheet) parts.push(`工作表：${location.sheet}`);
  if (typeof location.page === 'number') parts.push(`第 ${location.page} 页`);
  if (typeof location.row === 'number') parts.push(`第 ${location.row} 行`);
  if (typeof location.cell === 'string' && location.cell) parts.push(`单元格 ${location.cell}`);
  if (typeof location.paragraph === 'number') parts.push(`第 ${location.paragraph} 段`);
  if (typeof location.table === 'number') parts.push(`表格 ${location.table}`);
  if (typeof location.column === 'string') parts.push(`列 ${location.column}`);
  return parts.length > 0 ? parts.join(' · ') : '文档级来源';
}

export function FindingDetail({ finding, onBack, onReview, onReload, backLabel = '返回风险概览' }: FindingDetailProps) {
  const [status, setStatus] = useState<ReviewStatus>(finding.review_status ?? 'open');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);
  async function save() {
    if (!onReview || finding.is_current === false || finding.version === undefined || !note.trim() || busy || stale) return;
    setBusy(true); setError(null); setSaved(false);
    try {
      await onReview({ expected_version: finding.version, status, note: note.trim() });
      setNote(''); setSaved(true);
    } catch (reason) {
      const code = safeErrorCode(reason);
      setStale(true);
      setError(code === 'DESKTOP_REVIEW_STALE' ? '依据已更新，请先刷新详情，核对后再提交。你的说明已保留。' : `复核结果尚未核实：${code}。请先刷新详情，输入已保留，不会重复提交。`);
    } finally { setBusy(false); }
  }
  async function reload() {
    if (!onReload || busy) return;
    setBusy(true);
    try { await onReload(); setStale(false); setError(null); setSaved(false); }
    catch (reason) { setError(`刷新失败：${safeErrorCode(reason)}。你的说明已保留。`); }
    finally { setBusy(false); }
  }
  return (
    <section className="finding-detail" aria-labelledby="finding-title">
      <button type="button" className="text-action" onClick={onBack} disabled={busy}>
        ← {backLabel}
      </button>
      <div className="section-heading">
        <div>
          <p className="eyebrow">风险详情</p>
          <h2 id="finding-title">{finding.title}</h2>
        </div>
        <span className="status-pill">{SEVERITY_LABELS[finding.severity] ?? finding.severity}</span>
      </div>

      <dl className="finding-meta">
        <div>
          <dt>判断状态</dt>
          <dd>{STATUS_LABELS[finding.assessment_status] ?? finding.assessment_status}</dd>
        </div>
        <div>
          <dt>规则 ID</dt>
          <dd>{finding.rule_id}</dd>
        </div>
        <div>
          <dt>人工复核</dt>
          <dd>{finding.requires_human_review ? '需要人工复核' : '无需强制人工复核'}</dd>
        </div>
      </dl>

      <p className="finding-summary">{finding.summary}</p>
      {finding.is_current === false ? <p role="status">本事项已退出当前结果，仅保留历史复核；不代表风险已解决。</p> : null}

      <div className="source-list">
        <h3>材料依据</h3>
        <p>定位仅证明文字所在位置，不代表事实解释或法律判断正确。</p>
        {finding.sources.length === 0 ? <p>没有可追溯的直接材料依据，不能据此认定不存在风险；请补充或核对材料。</p> : null}
        {finding.sources.map((source) => (
          <article key={source.id} className="source-card">
            <strong>{source.file_name}</strong>
            <p>{provenanceLabel(source.provenance)}</p>
            <p>{locatorText(source.location)}</p>
            {source.excerpt ? <blockquote>{source.excerpt}</blockquote> : null}
          </article>
        ))}
      </div>
      {finding.recommended_actions?.length ? <section><h3>建议处理</h3>
        {finding.recommended_actions.map((action, index) => <p key={index}>{action}</p>)}
      </section> : null}
      <section aria-label="人工复核记录">
        <h3>复核记录</h3>
        <p>当前处理状态：{REVIEW_LABELS[finding.review_status ?? 'open']}。人工处理决定不会改写原始判断和材料。</p>
        {finding.reviews?.map((review, index) => <article key={index} className="source-card">
          <strong>{REVIEW_LABELS[review.status]}</strong><p>{review.note}</p><time>{review.created_at.replace('T', ' ')}</time>
        </article>)}
      </section>
      {onReview && finding.is_current !== false && finding.version !== undefined ? <section aria-label="处理本项风险" className="review-form">
        <h3>处理本项风险</h3>
        <label>复核决定<select value={status} disabled={busy} onChange={(event) => { setStatus(event.target.value as ReviewStatus); setSaved(false); }}>
          {Object.entries(REVIEW_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label>复核说明<textarea value={note} maxLength={500} disabled={busy}
          onChange={(event) => { setNote(event.target.value); setSaved(false); }} /></label>
        <p>请说明核对了哪些材料及处理理由。避免填写身份证号、电话号码等个人信息。</p>
        {error ? <p role="alert">{error}</p> : null}
        {saved ? <p role="status">复核已保存。</p> : null}
        {stale && onReload ? <button type="button" disabled={busy} onClick={reload}>刷新详情</button> : null}
        <button type="button" className="primary-action" disabled={busy || stale || !note.trim()} onClick={save}>{busy ? '正在保存…' : '保存复核'}</button>
      </section> : null}
    </section>
  );
}
import { useState } from 'react';
import { safeErrorCode } from '../../lib/api';

export type ReviewStatus = 'open' | 'reviewed' | 'dismissed' | 'not_applicable' | 'needs_material';
export type FindingReviewInput = { expected_version: number; status: ReviewStatus; note: string };
export const REVIEW_LABELS: Record<ReviewStatus, string> = {
  open: '待复核', reviewed: '已复核', dismissed: '误报排除', not_applicable: '不适用', needs_material: '待补材料',
};

export function provenanceLabel(state?: string): string {
  return state === 'locally_located' ? '本地已定位'
    : state === 'unlocated_needs_review' ? '未定位，需核对原材料' : '旧版来源未经本地核验';
}
