import type { DashboardFinding } from '../dashboard/DashboardView';

export type EmployeeLedgerItem = {
  id: string;
  employee_number: string | null;
  masked_name: string;
  department: string;
  job_title: string | null;
  employment_status: string;
  match_status: string;
  risk_counts: { high: number; medium: number };
  insufficient_data_count: number;
  requires_human_review_count: number;
  material_coverage: number;
};

export type EmployeeLedgerPayload = {
  items: EmployeeLedgerItem[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  department_options: string[];
};

export type EmployeeFinding = DashboardFinding & {
  summary: string;
  category: string;
  severity_label: string;
  status_label: string;
  review_status: string;
  review_status_label: string;
  employee_id: string | null;
  employee_name: string;
  department: string | null;
  due_date: string | null;
};

export type EmployeeDetailPayload = {
  employee: Omit<
    EmployeeLedgerItem,
    | 'risk_counts'
    | 'insufficient_data_count'
    | 'requires_human_review_count'
    | 'material_coverage'
  >;
  findings: EmployeeFinding[];
};

const EMPLOYMENT_STATUS_LABELS: Record<string, string> = {
  active: '在职',
  terminated: '离职',
  unknown: '待确认',
};

export function EmployeeLedger({
  payload,
  onSelectEmployee,
  onBack,
}: {
  payload: EmployeeLedgerPayload;
  onSelectEmployee: (employeeId: string) => void;
  onBack: () => void;
}) {
  return (
    <section className="employee-ledger" aria-labelledby="employee-ledger-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">员工风险台账</p>
          <h2 id="employee-ledger-title">员工台账</h2>
          <p className="muted">共 {payload.total} 名员工，仅显示脱敏姓名。</p>
        </div>
        <button type="button" className="secondary-action" onClick={onBack}>返回风险概览</button>
      </div>
      {payload.items.length ? (
        <div className="table-scroll">
          <table className="employee-table">
            <thead>
              <tr>
                <th>员工</th><th>部门 / 岗位</th><th>状态</th><th>风险</th><th>资料事项</th><th>材料覆盖</th><th />
              </tr>
            </thead>
            <tbody>
              {payload.items.map((item) => (
                <tr key={item.id}>
                  <td><strong>{item.masked_name}</strong><small>{item.employee_number ?? '无工号'}</small></td>
                  <td>{item.department}<small>{item.job_title ?? '岗位待确认'}</small></td>
                  <td>{EMPLOYMENT_STATUS_LABELS[item.employment_status] ?? item.employment_status}</td>
                  <td>高 {item.risk_counts.high} · 中 {item.risk_counts.medium}</td>
                  <td>不足 {item.insufficient_data_count} · 复核 {item.requires_human_review_count}</td>
                  <td>{Math.round(item.material_coverage * 100)}%</td>
                  <td>
                    <button
                      type="button"
                      className="text-action"
                      aria-label={`查看${item.masked_name}详情`}
                      onClick={() => onSelectEmployee(item.id)}
                    >查看详情</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty-state">当前筛选下没有员工记录。</p>}
    </section>
  );
}

export function EmployeeDetail({
  payload,
  onSelectFinding,
  onBack,
}: {
  payload: EmployeeDetailPayload;
  onSelectFinding: (findingId: string) => void;
  onBack: () => void;
}) {
  return (
    <section className="employee-detail" aria-labelledby="employee-detail-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">员工风险详情</p>
          <h2 id="employee-detail-title">{payload.employee.masked_name}</h2>
          <p className="muted">{payload.employee.employee_number ?? '无工号'} · {payload.employee.department} · {payload.employee.job_title ?? '岗位待确认'}</p>
        </div>
        <button type="button" className="secondary-action" onClick={onBack}>返回员工台账</button>
      </div>
      <div className="finding-list" aria-label="员工风险与资料事项">
        {payload.findings.length ? payload.findings.map((finding) => (
          <button type="button" className="finding-row" key={finding.id} onClick={() => onSelectFinding(finding.id)}>
            <span className="finding-copy"><strong>{finding.title}</strong><small>{finding.rule_id} · {finding.summary}</small></span>
            <span className="finding-badges">
              <span>{finding.status_label}</span>
              {finding.requires_human_review ? <b>需要人工复核</b> : null}
            </span>
          </button>
        )) : <p className="empty-state">该员工暂无可列示事项。</p>}
      </div>
    </section>
  );
}
