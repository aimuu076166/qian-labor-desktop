import { useRef, useState } from 'react';
import { printAnalysisReport } from '../../lib/desktop';
import type { AssessmentRevision, AssessmentScope } from '../../lib/api';
import { REVIEW_LABELS, type ReviewStatus } from '../findings/FindingDetail';
import { AssessmentScopeNotice } from '../scope/AssessmentScopeNotice';
import { AssessmentRevisionStatus } from '../employees/AssessmentRevisionStatus';
import { factLabel, OPTION_LABELS } from '../employees/EmployeeFactWorkbench';
import { describeOperationError } from '../../lib/errorMessages';

function factValue(value: unknown): string {
  if (value === null || value === undefined) return '未知，待确认';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map(item => Array.isArray(item) ? item.map(factValue).join(' 至 ') : factValue(item)).join('；') || '未列示';
  if (typeof value === 'object') return Object.entries(value).map(([name, item]) => `${factLabel(name)}：${factValue(item)}`).join('；') || '未列示';
  return OPTION_LABELS[String(value)] ?? String(value);
}
const reportLabels: Record<string, string> = {
  pending: '待处理', checking: '正在核对', addressed: '已采取措施', dismissed: '暂不采纳',
  uploaded: '待分析', parsing: '正在解析', extracting: '正在提取', processed: '已处理', partial: '部分完成', failed: '处理失败', interrupted: '已中断，待恢复',
  completed: '已执行条款观察（意见未核验）', not_executed: '未执行条款观察', unreadable: '无法读取条款内容', not_applicable: '未识别为适用内容，仍需核对',
  contract: '劳动合同', roster: '员工名册', social_insurance: '社保材料', termination: '解除材料', unknown: '待分类',
};

export type ReportPayload = {
  analysis_id: string;
  company_name: string;
  generated_at: string;
  status: string;
  is_demo: boolean;
  limitations?: string[];
  dataset_label?: string;
  company?: { display_name: string };
  canonical_employees?: Array<{ snapshot_id: string; masked_name: string; employee_number: string; department: string; job_title: string; lifecycle_status: string }>;
  facts?: Array<{ id: string; employee_id?: string | null; fact_type: string; filename: string; original_value: unknown; effective_value: unknown; human_confirmed: boolean;
    revision_valid: boolean; basis_pending: boolean; source_valid: boolean; latest_revision?: { reason: string; created_at: string } | null;
    sources: Array<{ id: string; location: Record<string, unknown>; excerpt?: string; provenance?: string }> }>;
  advisories?: Array<{ id: string; filename: string; issue: string; checks: string[]; next_action: string; unverified_references: string[];
    source: { location: Record<string, unknown>; excerpt?: string; provenance?: string }; handling?: { decision: string; reason: string } | null }>;
  advisory_runs?: Array<{ id: string; execution_status: string }>;
  materials?: Array<{ id: string; filename: string; kind: string; status: string; error_code: string | null }>;
  assessment_scope?: AssessmentScope;
  assessment_revision?: AssessmentRevision;
  summary: {
    employee_count: number;
    high_count: number;
    medium_count: number;
    low_count: number;
    insufficient_data_count: number;
    coverage_rate: number;
    affected_employee_count: number;
    requires_human_review_count: number;
    deadline_30_count: number;
    classification_pending: boolean;
  };
  material_coverage: {
    overall: number;
    scope_pending?: boolean;
    items: Array<{ label?: string; covered?: number; applicable?: number; rate?: number }>;
  };
  employees: Array<{ id: string; masked_name: string; employee_number: string | null }>;
  findings: Array<{
    id: string;
    rule_id: string;
    title: string;
    severity_label: string;
    status_label: string;
    requires_human_review: boolean;
    employee_name: string;
    summary?: string;
    review_status?: ReviewStatus;
    recommended_actions?: string[];
    reviews?: { status: ReviewStatus; note: string; created_at: string }[];
    sources: Array<{
      file_name: string;
      locator_type: string;
      location: Record<string, unknown>;
      provenance?: string;
      excerpt?: string;
      human_confirmed?: boolean;
    }>;
  }>;
};

function sourceLocation(location: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof location.sheet === 'string') parts.push(location.sheet);
  if (typeof location.paragraph === 'number') parts.push(`第 ${location.paragraph} 段`);
  if (typeof location.image === 'number') parts.push(`第 ${location.image} 张图片`);
  if (typeof location.row === 'number') parts.push(`第 ${location.row} 行`);
  if (typeof location.cell === 'string') parts.push(location.cell);
  if (typeof location.page === 'number') parts.push(`第 ${location.page} 页`);
  if (typeof location.table === 'number') parts.push(`表格 ${location.table}`);
  if (typeof location.column === 'string') parts.push(`列 ${location.column}`);
  return parts.join(' · ') || '材料内位置';
}

export function ReportView({
  payload,
  onBack,
  saved,
}: {
  payload: ReportPayload;
  onBack: () => void;
  saved?: { version: number; id: string; created_at: string; content_sha256: string; input_revision: string; result_revision: string | null; review_revision: string; context_signature: string };
}) {
  const printInFlight = useRef(false);
  const [printing, setPrinting] = useState(false);
  const [printError, setPrintError] = useState(false);

  async function handlePrint() {
    if (printInFlight.current) return;
    printInFlight.current = true;
    setPrinting(true);
    setPrintError(false);
    try {
      await printAnalysisReport();
    } catch {
      setPrintError(true);
    } finally {
      printInFlight.current = false;
      setPrinting(false);
    }
  }

  return (
    <article className="report-view" aria-labelledby="report-title">
      <div className="report-toolbar print-hidden">
        <button type="button" className="secondary-action" onClick={onBack}>返回风险概览</button>
        <button type="button" className="primary-action" onClick={handlePrint} disabled={printing}>
          {printing ? '正在打开打印窗口…' : '打印或保存 PDF'}
        </button>
      </div>
      {printError ? <p role="alert" className="error-message print-hidden">无法打开系统打印窗口，请重试。</p> : null}
      <header className="report-header">
        <p className="eyebrow">QIAN LABOR DESKTOP</p>
        <h2 id="report-title">企业用工风险体检报告</h2>
        <p className="review-note">{saved ? '已保存冻结版本' : '报告草稿'}</p>
        {saved ? <><p>已保存版本 {saved.version} · {saved.id}</p><p>本页为已保存的冻结版本，后续资料或处理修改不会改写本页。</p>
          <p className="report-signature">内容校验 SHA-256：{saved.content_sha256}</p>
          <p className="report-signature">输入：{saved.input_revision} · 结果：{saved.result_revision ?? '尚无结果'} · 复核：{saved.review_revision}</p></> :
          <p>本页为当前结果，尚未形成锁定版本；补充材料或修改复核决定后，内容可能变化。</p>}
        <p><strong>{payload.company_name}</strong></p>
        {payload.company ? <p>归属企业（保存时）：{payload.company.display_name}</p> : null}
        <p className="muted">生成时间：{new Date(payload.generated_at).toLocaleString('zh-CN')} · {payload.dataset_label ?? (payload.is_demo ? '演示模式' : '本地资料（不代表已调用真实模型）')}</p>
      </header>
      <AssessmentScopeNotice scope={payload.assessment_scope} />
      <AssessmentRevisionStatus revision={payload.assessment_revision} />
      {payload.assessment_revision ? <p>核查日期：{payload.assessment_revision.check_date} · {payload.assessment_revision.check_date_explicit ? '人工明确选择' : '原体检创建日期（未人工确认）'}</p> : null}
      {payload.limitations?.length ? <section className="report-section"><h3>适用范围与限制</h3>{payload.limitations.map((text, i) => <p key={i}>{text}</p>)}</section> : null}
      <section className="report-metrics" aria-label="报告摘要">
        <div><span>员工</span><strong>{payload.summary.employee_count}</strong></div>
        <div><span>高风险</span><strong>{payload.summary.high_count}</strong></div>
        <div><span>中风险</span><strong>{payload.summary.medium_count}</strong></div>
        <div><span>资料不足</span><strong>{payload.summary.insufficient_data_count}</strong></div>
        <div><span>材料覆盖</span><strong>{payload.material_coverage.scope_pending ? '待确认' : `${Math.round(payload.summary.coverage_rate * 100)}%`}</strong></div>
        <div><span>人工复核</span><strong>{payload.summary.requires_human_review_count}</strong></div>
      </section>
      <section className="report-section">
        <h3>风险与资料事项</h3>
        {payload.findings.length ? payload.findings.map((finding, index) => (
          <article className="report-finding" key={finding.id}>
            <h4>{index + 1}. {finding.title}</h4>
            <p>{finding.rule_id} · {finding.severity_label} · {finding.status_label} · {finding.employee_name}</p>
            {finding.summary ? <p>{finding.summary}</p> : null}
            <p>处理状态：{REVIEW_LABELS[finding.review_status ?? 'open']}</p>
            {finding.recommended_actions?.map((action, actionIndex) => <p key={actionIndex}>{action}</p>)}
            {finding.reviews?.map((review, reviewIndex) => <p key={reviewIndex}>
              {REVIEW_LABELS[review.status]}：{review.note}（{review.created_at.replace('T', ' ')}）
            </p>)}
            {finding.requires_human_review ? <p className="review-note">需要人工复核</p> : null}
            {finding.sources.length ? (
              <ul>
                {finding.sources.map((source, sourceIndex) => (
                  <li key={`${finding.id}-${sourceIndex}`}>{source.file_name} · {sourceLocation(source.location)} · {provenanceLabel(source.provenance)}
                    {source.human_confirmed ? ' · 人工确认事实' : ''}{source.excerpt ? <blockquote>{source.excerpt}</blockquote> : null}</li>
                ))}
              </ul>
            ) : <p className="muted">本事项暂无可展示来源定位。</p>}
          </article>
        )) : <p>本次没有可列示事项。</p>}
      </section>
      <section className="report-section"><h3>本次体检员工名单</h3>
        {payload.employees.length ? payload.employees.map(person => <p key={person.id}>{person.masked_name} · {person.employee_number || '未列示工号'}</p>) : <p>尚无可列示员工。</p>}</section>
      {payload.canonical_employees?.length ? <section className="report-section"><h3>员工档案归属（保存时）</h3>
        {payload.canonical_employees.map(person => <p key={person.snapshot_id}>{person.masked_name} · {person.employee_number} · {person.department} · {person.job_title} · {person.lifecycle_status === 'archived' ? '已归档' : '在册'}</p>)}</section> : null}
      {payload.materials ? <section className="report-section"><h3>材料状态（保存时）</h3>{payload.materials.map(file => <p key={file.id}>{file.filename} · {reportLabels[file.kind] ?? file.kind} · {reportLabels[file.status] ?? file.status}{file.error_code ? ` · ${describeOperationError(file.error_code)}` : ''}</p>)}</section> : null}
      {payload.facts ? <section className="report-section"><h3>原始事实、有效事实与人工确认</h3>
        {payload.facts.map(fact => <article className="report-finding" key={fact.id}><h4>{factLabel(fact.fact_type)} · {fact.filename}</h4>
          <p>员工：{(() => {
            const captured = payload.canonical_employees?.find(person => person.snapshot_id === fact.employee_id)
              ?? payload.employees.find(person => person.id === fact.employee_id);
            return captured ? `${captured.masked_name} · ${captured.employee_number || '工号未记录'}` : '员工归属未匹配或待确认';
          })()}</p>
          <p>原始值：{factValue(fact.original_value)}</p><p>有效值：{factValue(fact.effective_value)}</p>
          <p>{fact.human_confirmed ? '人工已确认' : '尚待人工确认'}{!fact.source_valid || fact.basis_pending ? ' · 来源或合同依据待核对' : ''}</p>
          {fact.latest_revision ? <p>确认／修订说明：{fact.latest_revision.reason} · {fact.latest_revision.created_at}</p> : null}
          {fact.sources.map(source => <div key={source.id}><p>{sourceLocation(source.location)} · {provenanceLabel(source.provenance)}</p>
            {source.excerpt ? <blockquote>{source.excerpt}</blockquote> : null}</div>)}</article>)}</section> : null}
      {payload.advisories ? <section className="report-section"><h3>合同条款观察（未经核验，不计入确定性风险统计）</h3>
        {payload.advisory_runs?.map(run => <p key={run.id}>已保存执行状态：{reportLabels[run.execution_status] ?? '执行状态待核对'}</p>)}
        {payload.advisories.length ? payload.advisories.map(item => <article className="report-finding" key={item.id}><h4>{item.issue}</h4>
          <p>{item.filename} · {sourceLocation(item.source.location)} · {provenanceLabel(item.source.provenance)}</p>
          {item.source.excerpt ? <blockquote>{item.source.excerpt}</blockquote> : null}
          {item.checks.map((check, i) => <p key={i}>{check}</p>)}<p>{item.next_action}</p>
          {item.unverified_references.map((ref, i) => <p key={i}>未经核验引用：{ref}</p>)}
          {item.handling ? <p>人工处理：{reportLabels[item.handling.decision] ?? '待核对'} · {item.handling.reason}</p> : <p>尚待人工核对</p>}</article>) :
          <p>无可列示的已保存条款观察；不表示合同已经模型审核或不存在风险。</p>}</section> : null}
      <footer className="report-footer">本报告为企业用工风险体检辅助结果；“资料不足”不等于无风险，需结合原始材料人工复核。</footer>
    </article>
  );
}
import { provenanceLabel } from '../findings/FindingDetail';
