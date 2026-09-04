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

export type DashboardOverview = {
  company_name: string;
  summary: {
    coverage_rate: number;
    affected_employee_count: number;
    requires_human_review_count: number;
    deadline_30_count: number;
    classification_pending: boolean;
  };
  categories: Array<{ code: string; label: string; count: number }>;
  material_coverage: {
    overall: number;
    classification_pending: boolean;
    items: Array<{
      code: string;
      label: string;
      covered: number;
      applicable: number;
      rate: number;
      not_applicable: boolean;
      classification_pending: boolean;
    }>;
  };
};

type DashboardViewProps = {
  summary?: DashboardSummary;
  findings?: DashboardFinding[];
  overview?: DashboardOverview;
  onSelectFinding?: (findingId: string) => void;
  onOpenEmployees?: () => void;
  onOpenReport?: () => void;
  onDeleteAnalysis?: () => void;
  onSelectMaterials?: () => void;
  selectingMaterials?: boolean;
};

const ASSESSMENT_LABELS: Record<string, string> = {
  management_reminder: '管理提醒',
  confirmed_anomaly: '确定性异常',
  suspected_risk: '疑似风险',
  insufficient_data: '资料不足 · 请补充材料',
  requires_human_review: '需要人工复核',
};

const ANALYSIS_STATUS_LABELS: Record<string, string> = {
  completed: '分析完成',
  partial: '部分完成',
};

export function DashboardView({
  summary,
  findings = [],
  overview,
  onSelectFinding,
  onOpenEmployees,
  onOpenReport,
  onDeleteAnalysis,
  onSelectMaterials,
  selectingMaterials = false,
}: DashboardViewProps) {
  return (
    <section className="dashboard-view" aria-labelledby="dashboard-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">风险体检结果</p>
          <h2 id="dashboard-title">企业用工风险概览</h2>
        </div>
        <div className="heading-actions">
          {onSelectMaterials ? (
            <button
              type="button"
              className="primary-action"
              disabled={selectingMaterials}
              onClick={onSelectMaterials}
            >
              {selectingMaterials ? '正在选择…' : '选择企业材料'}
            </button>
          ) : null}
          {summary ? (
            <span className="status-pill">
              {ANALYSIS_STATUS_LABELS[summary.status] ?? '结果已生成'}
            </span>
          ) : null}
          {onDeleteAnalysis ? (
            <button type="button" className="text-action danger-action" onClick={onDeleteAnalysis}>
              删除本次分析
            </button>
          ) : null}
        </div>
      </div>

      {!summary ? (
        <div className="dashboard-empty-state">
          <h3>尚未导入企业材料</h3>
          <p className="muted">选择 Word、Excel、PDF、图片或扫描件后开始本机风险体检。</p>
        </div>
      ) : (
        <>
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
            {overview ? (
              <>
                <article className="metric-card">
                  <span>材料覆盖率</span>
                  <strong>{Math.round(overview.summary.coverage_rate * 100)}%</strong>
                </article>
                <article className="metric-card">
                  <span>受影响员工</span>
                  <strong>{overview.summary.affected_employee_count}</strong>
                </article>
                <article className="metric-card">
                  <span>需人工复核</span>
                  <strong>{overview.summary.requires_human_review_count}</strong>
                </article>
              </>
            ) : null}
          </div>

          {overview ? (
            <div className="dashboard-breakdown">
              <section aria-labelledby="coverage-title">
                <h3 id="coverage-title">材料覆盖情况</h3>
                {overview.material_coverage.classification_pending ? (
                  <p className="inline-warning">仍有材料类型待确认，覆盖率仅供人工复核。</p>
                ) : null}
                <div className="coverage-list">
                  {overview.material_coverage.items.map((item) => (
                    <div className="coverage-row" key={item.code}>
                      <span>{item.label}</span>
                      <strong>
                        {item.not_applicable
                          ? '不适用'
                          : `${item.covered}/${item.applicable}（${Math.round(item.rate * 100)}%）`}
                      </strong>
                    </div>
                  ))}
                </div>
              </section>
              <section aria-labelledby="category-title">
                <h3 id="category-title">风险领域</h3>
                <div className="category-list">
                  {overview.categories.length ? (
                    overview.categories.map((item) => (
                      <span key={item.code}>{item.label} {item.count}</span>
                    ))
                  ) : (
                    <p className="muted">本次没有可列示的风险领域。</p>
                  )}
                </div>
              </section>
            </div>
          ) : null}

          {onOpenEmployees || onOpenReport ? (
            <div className="dashboard-actions">
              {onOpenEmployees ? (
                <button type="button" className="secondary-action" onClick={onOpenEmployees}>
                  查看员工台账
                </button>
              ) : null}
              {onOpenReport ? (
                <button type="button" className="primary-action" onClick={onOpenReport}>
                  生成体检报告
                </button>
              ) : null}
            </div>
          ) : null}

          {onSelectFinding ? (
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
                    <span>
                      {ASSESSMENT_LABELS[finding.assessment_status]
                        ?? finding.assessment_status}
                    </span>
                    {finding.requires_human_review ? <b>需要人工复核</b> : null}
                  </span>
                </button>
              ))}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
