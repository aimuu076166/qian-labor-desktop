import type { CurrentWorkspace, EmployeeFilters } from './useCompanyWorkspace';
import type { ReactNode } from 'react';
import { AssessmentRevisionStatus } from '../employees/AssessmentRevisionStatus';

export function CompanyWorkbench({ payload, filters, onFilters, onSelectEmployee, onImport, busy, onMatching, onOverview, priorities }:
  { payload: CurrentWorkspace; filters: EmployeeFilters; onFilters: (patch: Partial<EmployeeFilters>) => void;
    onSelectEmployee: (id: string) => void; onImport: () => void; busy: boolean; onMatching: () => void; onOverview: () => void; priorities?: ReactNode }) {
  const analysis = payload.current_analysis;
  return <section className="employee-ledger" aria-label="企业工作台">
    <div className="section-heading"><div><h2>{payload.company.display_name}</h2>
      <p>已建档员工 {payload.enrolled_employee_count} 人</p>
      <p className="muted">仅统计本机已建档员工，不代表企业全体人员。资料不足不等于无风险。</p>
      <p>{!analysis ? '尚未导入企业材料' : analysis.stale ? '当前材料待分析或处理中，风险结果待更新' : '当前材料分析完成'}</p>
      <AssessmentRevisionStatus revision={analysis?.assessment_revision} />
    </div><button type="button" className="primary-action" onClick={onImport} disabled={busy}>选择企业材料</button></div>
    {payload.pending_identity_count > 0 ? <p>有 {payload.pending_identity_count} 项材料待确认归属 <button type="button" disabled={busy} onClick={onMatching}>确认员工匹配</button>
      {busy ? <span role="status">任务正在处理，完成后才能确认员工匹配。</span> : null}</p> : null}
    {priorities}
    <div className="employee-filters">
      <label>搜索员工<input aria-label="搜索员工" value={filters.search} onChange={e => onFilters({ search: e.target.value, page: 1 })} /></label>
      <label>风险等级<select aria-label="风险等级" value={filters.severity} onChange={e => onFilters({ severity: e.target.value, page: 1 })}>
        <option value="">全部</option><option value="high">高风险</option><option value="medium">中风险</option></select></label>
      <label>评估状态<select aria-label="评估状态" value={filters.assessment_state} onChange={e => onFilters({ assessment_state: e.target.value, page: 1 })}>
        <option value="">全部</option><option value="pending_evidence">待补材料</option><option value="pending_analysis">待分析</option><option value="evaluated">已评估</option></select></label>
    </div>
    <div className="table-scroll"><table className="employee-table"><thead><tr><th>员工</th><th>部门 / 岗位</th><th>用工状态</th><th>档案状态</th><th>当前评估</th><th /></tr></thead>
      <tbody>{payload.employees.map(item => <tr key={item.id}>
        <td>{item.masked_name}<small>{item.employee_number ?? '无工号'}</small></td><td>{item.department ?? '部门待确认'}<small>{item.job_title ?? '岗位待确认'}</small></td>
        <td>{item.employment_status === 'active' ? '在职' : item.employment_status === 'terminated' ? '离职' : '待确认'}</td>
        <td>{item.lifecycle_status === 'archived' ? '已归档' : '使用中'}</td>
        <td>{item.assessment ? <>高 {item.assessment.risk_counts.high} · 中 {item.assessment.risk_counts.medium}<small>资料不足 {item.assessment.insufficient_data_count} · 待复核 {item.assessment.requires_human_review_count}</small></>
          : item.assessment_state === 'pending_evidence' ? '待补材料' : '待分析'}</td>
        <td><button type="button" className="text-action" aria-label={`查看${item.masked_name}详情`} onClick={() => onSelectEmployee(item.id)}>查看详情</button></td>
      </tr>)}</tbody></table></div>
    {!payload.employees.length ? <p>当前没有符合条件的员工记录。可调整筛选，或导入材料并确认员工归属。</p> : null}
    <div className="dashboard-actions"><span>共 {payload.total} 条 · 第 {payload.page} / {Math.max(1, payload.pages)} 页</span>
      <button type="button" disabled={payload.page <= 1} onClick={() => onFilters({ page: payload.page - 1 })}>上一页</button>
      <button type="button" disabled={payload.page >= payload.pages} onClick={() => onFilters({ page: payload.page + 1 })}>下一页</button></div>
    {analysis && !analysis.stale ? <button type="button" className="secondary-action" onClick={onOverview}>查看当前风险概览</button> : null}
  </section>;
}
