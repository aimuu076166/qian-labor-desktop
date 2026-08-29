export type FindingSource = {
  id: string;
  file_id: string;
  file_name: string;
  locator_type: string;
  location: Record<string, unknown>;
  excerpt: string;
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
};

type FindingDetailProps = {
  finding: FindingDetailData;
  onBack: () => void;
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
  return parts.length > 0 ? parts.join(' · ') : '文档级来源';
}

export function FindingDetail({ finding, onBack }: FindingDetailProps) {
  return (
    <section className="finding-detail" aria-labelledby="finding-title">
      <button type="button" className="text-action" onClick={onBack}>
        ← 返回风险概览
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

      <div className="source-list">
        <h3>材料依据</h3>
        {finding.sources.map((source) => (
          <article key={source.id} className="source-card">
            <strong>{source.file_name}</strong>
            <p>{locatorText(source.location)}</p>
            {source.excerpt ? <blockquote>{source.excerpt}</blockquote> : null}
          </article>
        ))}
      </div>
    </section>
  );
}
