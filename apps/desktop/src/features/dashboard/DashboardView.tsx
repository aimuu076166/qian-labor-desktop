export type DashboardSummary = {
  analysis_id: string;
  status: string;
  employee_count: number;
  finding_count: number;
  high_count: number;
  medium_count: number;
  insufficient_data_count: number;
};

export type DashboardFinding = {
  id: string;
  rule_id: string;
  title: string;
  severity: string;
  assessment_status: string;
  requires_human_review: boolean;
};

type DashboardViewProps = {
  summary: DashboardSummary;
  findings: DashboardFinding[];
  onSelectFinding: (findingId: string) => void;
  onDeleteAnalysis?: () => void;
};

const ASSESSMENT_LABELS: Record<string, string> = {
  management_reminder: '管理提醒',
  confirmed_anomaly: '确定性异常',
  suspected_risk: '疑似风险',
  insufficient_data: '资料不足 · 请补充材料',
  requires_human_review: '需要人工复核',
};

export function DashboardView({
  summary,
  findings,
  onSelectFinding,
  onDeleteAnalysis,
}: DashboardViewProps) {
  return (
    <section className="dashboard-view" aria-labelledby="dashboard-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">风险体检结果</p>
          <h2 id="dashboard-title">企业用工风险概览</h2>
        </div>
        <div className="heading-actions">
          <span className="status-pill">分析完成</span>
          {onDeleteAnalysis ? (
            <button type="button" className="text-action danger-action" onClick={onDeleteAnalysis}>
              删除本次分析
            </button>
          ) : null}
        </div>
      </div>

      <div className="summary-grid">
        <article className="metric-card metric-high">
          <span>高风险</span>
          <strong>{summary.high_count}</strong>
        </article>
        <article className="metric-card">
          <span>中风险</span>
          <strong>{summary.medium_count}</strong>
        </article>
        <article className="metric-card metric-insufficient">
          <span>资料不足</span>
          <strong>{summary.insufficient_data_count}</strong>
        </article>
        <article className="metric-card">
          <span>员工数</span>
          <strong>{summary.employee_count}</strong>
        </article>
        <article className="metric-card">
          <span>发现数</span>
          <strong>{summary.finding_count}</strong>
        </article>
      </div>

      <div className="finding-list" aria-label="风险与资料事项">
        {findings.map((finding) => (
          <button
            key={finding.id}
            type="button"
            className="finding-row"
            onClick={() => onSelectFinding(finding.id)}
          >
            <span className="finding-copy">
              <strong>{finding.title}</strong>
              <small>{finding.rule_id}</small>
            </span>
            <span className="finding-badges">
              <span>{ASSESSMENT_LABELS[finding.assessment_status] ?? finding.assessment_status}</span>
              {finding.requires_human_review ? <b>需要人工复核</b> : null}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
