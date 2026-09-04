export type ReportPayload = {
  analysis_id: string;
  company_name: string;
  generated_at: string;
  status: string;
  is_demo: boolean;
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
    sources: Array<{
      file_name: string;
      locator_type: string;
      location: Record<string, unknown>;
    }>;
  }>;
};

function sourceLocation(location: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof location.sheet === 'string') parts.push(location.sheet);
  if (typeof location.paragraph === 'number') parts.push(`第 ${location.paragraph} 段`);
  if (typeof location.row === 'number') parts.push(`第 ${location.row} 行`);
  if (typeof location.cell === 'string') parts.push(location.cell);
  if (typeof location.page === 'number') parts.push(`第 ${location.page} 页`);
  return parts.join(' · ') || '材料内位置';
}

export function ReportView({
  payload,
  onBack,
  onPrint = () => window.print(),
}: {
  payload: ReportPayload;
  onBack: () => void;
  onPrint?: () => void;
}) {
  return (
    <article className="report-view" aria-labelledby="report-title">
      <div className="report-toolbar print-hidden">
        <button type="button" className="secondary-action" onClick={onBack}>返回风险概览</button>
        <button type="button" className="primary-action" onClick={onPrint}>打印或保存 PDF</button>
      </div>
      <header className="report-header">
        <p className="eyebrow">QIAN LABOR DESKTOP</p>
        <h2 id="report-title">企业用工风险体检报告</h2>
        <p><strong>{payload.company_name}</strong></p>
        <p className="muted">生成时间：{new Date(payload.generated_at).toLocaleString('zh-CN')} · {payload.is_demo ? '演示模式' : '真实模型分析'}</p>
      </header>
      <section className="report-metrics" aria-label="报告摘要">
        <div><span>员工</span><strong>{payload.summary.employee_count}</strong></div>
        <div><span>高风险</span><strong>{payload.summary.high_count}</strong></div>
        <div><span>中风险</span><strong>{payload.summary.medium_count}</strong></div>
        <div><span>资料不足</span><strong>{payload.summary.insufficient_data_count}</strong></div>
        <div><span>材料覆盖</span><strong>{Math.round(payload.summary.coverage_rate * 100)}%</strong></div>
        <div><span>人工复核</span><strong>{payload.summary.requires_human_review_count}</strong></div>
      </section>
      <section className="report-section">
        <h3>风险与资料事项</h3>
        {payload.findings.length ? payload.findings.map((finding, index) => (
          <article className="report-finding" key={finding.id}>
            <h4>{index + 1}. {finding.title}</h4>
            <p>{finding.rule_id} · {finding.severity_label} · {finding.status_label} · {finding.employee_name}</p>
            {finding.requires_human_review ? <p className="review-note">需要人工复核</p> : null}
            {finding.sources.length ? (
              <ul>
                {finding.sources.map((source, sourceIndex) => (
                  <li key={`${finding.id}-${sourceIndex}`}>{source.file_name} · {sourceLocation(source.location)}</li>
                ))}
              </ul>
            ) : <p className="muted">本事项暂无可展示来源定位。</p>}
          </article>
        )) : <p>本次没有可列示事项。</p>}
      </section>
      <footer className="report-footer">本报告为企业用工风险体检辅助结果；“资料不足”不等于无风险，需结合原始材料人工复核。</footer>
    </article>
  );
}
