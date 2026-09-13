import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { vi } from 'vitest';
import { App } from '../src/App';
import type { FindingDetailData } from '../src/features/findings/FindingDetail';
import type { EmployeeFinding } from '../src/features/employees/EmployeeLedger';
import type { CurrentAnalysis, CurrentWorkspace } from '../src/features/workspace/useCompanyWorkspace';
import type { TaskRun } from '../src/features/processing/useAnalysisTask';

export const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
export const taskMetadata = (analysis_id: string, company_id: string | null, business_status: string, run: TaskRun | null = null, read_only = false) => ({
  analysis_id, company_id, business_status, run, read_only, history: run ? [run] : [], limit: 20, offset: 0,
  resume_preview: { reusable_file_ids: [], extraction_file_ids: [] }, usage_notice: 'admitted_requests_may_consume_quota_unknown_is_not_zero' });
export const taskRun = (analysis_id: string, company_id: string | null, state: TaskRun['state'] = 'completed'): TaskRun => ({
  id: `run-${analysis_id}`, analysis_id, company_id, parent_run_id: null, state, version: 2, in_flight: state === 'running',
  created_at: '2026-09-09T00:00:00', completed_at: state === 'completed' ? '2026-09-09T00:01:00' : null });
export const taskReceipt = (analysis_id: string, init: RequestInit, state: TaskRun['state'] = 'completed') => {
  const body = JSON.parse(String(init.body)); const run = taskRun(analysis_id, body.company_id, state);
  return { analysis_id, company_id: body.company_id, outcome: 'accepted', run, request: {
    id: body.request_id, analysis_id, company_id: body.company_id, run_id: run.id, operation: 'start',
    expected_run_id: body.expected_run_id, expected_version: body.expected_version, outcome: 'accepted', error_code: null, created_at: run.created_at } };
};
export const syntheticConfiguration = { provider: 'zhipu', configured: true, validated: true,
  textModel: 'glm-5.3-flash', visionModel: 'glm-5.3-flash', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' };
export const syntheticFinding: FindingDetailData & EmployeeFinding = {
  id: 'finding-one', analysis_id: 'current', rule_id: 'R01', title: '合成合同事项待核查', severity: 'high',
  assessment_status: 'suspected_risk', requires_human_review: true, summary: '请核对合成合同', version: 2,
  review_status: 'open', reviews: [], category: 'contract', severity_label: '高风险', status_label: '疑似风险',
  review_status_label: '待复核', employee_id: 'snapshot-one', employee_name: '合成员**', department: '合成部', due_date: null,
  sources: [{ id: 'source', file_id: 'file', file_name: 'synthetic-contract.docx', locator_type: 'paragraph',
    location: { paragraph: 2 }, excerpt: '完全虚构来源摘录' }],
};
export function companyServer(options: { analysisId?: string | null; status?: string; exists?: boolean;
  override?: (path: string, init?: RequestInit) => Promise<Response | undefined> } = {}) {
  const company = { id: 'company-one', display_name: '完全虚构企业', version: 0, created_at: '2026-09-01T00:00:00' };
  let companies = options.exists === false ? [] : [company];
  let preference = { last_company_id: companies.length ? company.id : null as string | null, version: 0 };
  let analysisId = options.analysisId === undefined ? 'current' : options.analysisId;
  let status = options.status ?? 'completed';
  let fileCount = analysisId ? 1 : 0;
  let finding = { ...syntheticFinding };
  const runs = new Map<string, TaskRun>();
  const receipts = new Map<string, ReturnType<typeof taskReceipt>>();
  const record = { id: 'record-one', company_id: company.id, masked_name: '合成员**', employee_number: 'SYN-1',
    department: '合成部', job_title: '合成岗位', lifecycle_status: 'active', version: 0, created_at: company.created_at };
  const binding = () => ({ snapshot_id: 'snapshot-one', employee_record_id: record.id, company_id: company.id,
    analysis_id: analysisId!, created_at: company.created_at });
  const metadata = (): CurrentAnalysis | null => analysisId ? { analysis_id: analysisId, company_id: company.id,
    role: 'current', assessment_profile: 'labor_materials_v1', status, current_stage: status, progress: 100,
    analysis_version: 0, company_version: company.version, stale: status !== 'completed', pending_identity_count: status === 'matching_review' ? 1 : 0 } : null;
  const projection = (params = new URLSearchParams()): CurrentWorkspace => ({ company, current_analysis: metadata(),
    enrolled_employee_count: 1, pending_identity_count: status === 'matching_review' ? 1 : 0,
    employees: [{ ...record, current_binding: analysisId ? binding() : null, snapshot_employee_id: analysisId ? 'snapshot-one' : null,
      employment_status: 'active', assessment_state: !analysisId ? 'pending_evidence' : status === 'completed' ? 'evaluated' : 'pending_analysis',
      assessment: status === 'completed' && analysisId ? { risk_counts: { high: 1, medium: 0 }, insufficient_data_count: 1,
        requires_human_review_count: 1, material_coverage: 0.6 } : null }],
    page: Number(params.get('page') ?? 1), page_size: 25, total: 26, pages: 2 });
  const request = vi.fn(async (path: string, init?: RequestInit): Promise<Response> => {
    const override = await options.override?.(path, init); if (override) return override;
    const method = init?.method ?? 'GET';
    if (path.includes('/task?')) {
      const id = path.split('/')[3]; const owner = new URLSearchParams(path.split('?')[1]).get('company_id');
      const business = id === analysisId ? status : 'uploading';
      return json(taskMetadata(id, owner, business, runs.get(id) ?? (['queued', 'parsing', 'extracting', 'evaluating'].includes(business) ? taskRun(id, owner, 'running') : null), owner !== null && id !== analysisId));
    }
    if (path.includes('/task/requests/')) {
      const id = path.split('/')[3]; const requestId = path.split('/')[6].split('?')[0];
      return json(receipts.get(requestId) ?? { analysis_id: id, company_id: new URLSearchParams(path.split('?')[1]).get('company_id'), outcome: 'unknown', request: null, run: null });
    }
    if (path.endsWith('/task/start')) {
      const id = path.split('/')[3]; const receipt = taskReceipt(id, init!);
      runs.set(id, receipt.run); receipts.set(receipt.request.id, receipt); if (id === analysisId) status = 'completed';
      return json(receipt, 202);
    }
    if (path.includes('/contract-advisories')) return json({ runs: [], observations: [], total: 0, run_total: 0,
      page: 1, pages: 0, page_size: 20, read_only: false, request_handling: null });
    if (path === '/api/company-workspaces') {
      if (method === 'POST') { const body = JSON.parse(String(init?.body)); Object.assign(company, { id: body.id, display_name: body.display_name }); companies = [company]; }
      return json(method === 'POST' ? company : companies, method === 'POST' ? 201 : 200);
    }
    if (path === '/api/workspace-preference') {
      if (method === 'PUT') preference = { last_company_id: JSON.parse(String(init?.body)).last_company_id, version: preference.version + 1 };
      return json(preference);
    }
    if (path === `/api/company-workspaces/${company.id}`) return json(company);
    if (path.startsWith(`/api/company-workspaces/${company.id}/current?`)) return json(projection(new URLSearchParams(path.split('?')[1])));
    if (path === `/api/company-workspaces/${company.id}/current-analysis`) {
      if (method === 'POST') { analysisId = JSON.parse(String(init?.body)).id; status = 'uploading'; company.version += 1; }
      return json(metadata(), method === 'POST' ? 201 : 200);
    }
    if (path === `/api/company-workspaces/${company.id}/employees/${record.id}`) return json({ ...record,
      bindings: analysisId ? [binding()] : [], current_binding: analysisId ? binding() : null, historical_bindings: [] });
    if (path.endsWith('/import-paths')) { fileCount = 1; status = 'uploading'; return json({ analysis_id: analysisId,
      files: [{ id: 'file', original_filename: 'synthetic-contract.docx' }],
      results: JSON.parse(String(init?.body)).paths.map((_: string, index: number) => ({ index, filename: 'synthetic-contract.docx',
        file_id: 'file', status: 'imported', error_code: null })) }); }
    if (path.endsWith('/process')) { status = 'queued'; return json({ status: 'queued' }, 202); }
    if (path.endsWith('/processing')) { if (status === 'queued') status = 'completed'; return json({ analysis_id: analysisId, status,
      progress: 100, current_stage: status, files: status === 'failed' ? [{ error_code: 'AI_TIMEOUT' }] : [] }); }
    if (path.endsWith('/workspace')) return json({ analysis: { id: path.split('/')[3], name: '合成材料档案', company_display_name: company.display_name, status: path.includes('/older/') ? 'uploading' : status },
      files: fileCount ? [{ id: 'file', filename: 'synthetic-contract.docx', status: 'uploaded', progress: 0, detected_kind: 'unknown',
        classified_kind: 'unknown', error_code: null, size_bytes: 100, fact_count: 1 }] : [] });
    if (path.endsWith('/dashboard')) return json({ summary: { analysis_id: analysisId, status, employee_count: 1, finding_count: 1,
      high_count: 1, medium_count: 0, insufficient_data_count: 1 }, findings: [finding] });
    if (path.endsWith('/employees/snapshot-one')) return json({ employee: { ...record, id: 'snapshot-one', employment_status: 'active', match_status: 'confirmed' }, findings: [finding] });
    if (path.endsWith('/matching-candidates')) return json({ analysis_id: analysisId, current_company_id: company.id, employee_record_options: [record],
      candidates: status === 'matching_review' ? [{ id: 'candidate', file_id: 'file', material_name: '合成待匹配材料', employee_id: null,
        employee_name: '待确认', employee_number: null, extracted_fields: {}, fact_ids: ['fact'], score: 0, reasons: [], status: 'pending', employee_options: [] }] : [] });
    if (path.endsWith('/matching-decisions')) { status = 'completed'; return json({ analysis_id: analysisId, analysis_status: status, employee_record_id: record.id }); }
    if (path.endsWith('/reviews')) { const body = JSON.parse(String(init?.body)); finding = { ...finding, review_status: body.status,
      version: finding.version! + 1, reviews: [{ status: body.status, note: body.note, created_at: '2026-09-08T01:00:00' }] }; return json(finding); }
    if (path === '/api/findings/finding-one') return json({ ...finding, analysis_id: analysisId });
    if (path.endsWith('/report')) return json({ analysis_id: analysisId, company_name: company.display_name, generated_at: company.created_at,
      status, is_demo: false, summary: { employee_count: 1, high_count: 1, medium_count: 0, low_count: 0,
        insufficient_data_count: 1, coverage_rate: 0.6, affected_employee_count: 1, requires_human_review_count: 1,
        deadline_30_count: 0, classification_pending: false }, material_coverage: { overall: 0.6, items: [] }, employees: [record], findings: [finding] });
    if (path.startsWith(`/api/company-workspaces/${company.id}/analyses?`)) return json({ page: 1, pages: 1, page_size: 25, total: 1, items: [{ id: 'older', name: '合成历史体检',
      company_id: company.id, relation: 'historical', can_adopt: false, adoption_blocker_code: 'WORKSPACE_ANALYSIS_ALREADY_BOUND',
      company_display_name: company.display_name, status: 'partial', file_count: 1, employee_count: 1, created_at: company.created_at }] });
    return json({}, 404);
  });
  return { request, company, record, projection, metadata, setStatus: (next: string) => { status = next; },
    setAnalysisId: (id: string) => { analysisId = id; }, getAnalysisId: () => analysisId };
}
export function renderCompany(server: ReturnType<typeof companyServer>, options: Partial<React.ComponentProps<typeof App>> = {}, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<QueryClientProvider client={client}><App
    backendLoader={async () => ({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' })}
    apiFactory={() => server.request} configurationLoader={async () => syntheticConfiguration} {...options} />
  </QueryClientProvider>);
}
