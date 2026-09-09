import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import type { AssessmentRevision } from '../src/lib/api';
import {
  EmployeeFactWorkbench,
  type AssessmentDecisionDrafts,
  type AssessmentDecisionRequests,
  type FactRevisionDrafts,
  type FactRevisionRequests,
  type ReevaluationRequests,
} from '../src/features/employees/EmployeeFactWorkbench';
import { companyServer, json, syntheticConfiguration } from './company-fixture';

const revision = (fresh = false): AssessmentRevision => ({
  input_revision: fresh ? 'b'.repeat(64) : 'a'.repeat(64), result_revision: fresh ? 'result-new' : 'result-old',
  evaluated_input_revision: fresh ? 'b'.repeat(64) : '0'.repeat(64), fresh,
  check_date: '2026-09-01', check_date_explicit: false, evaluated_at: '2026-09-01T08:00:00', read_only: false,
  report_review_revision: 'c'.repeat(64), availability: 'available' as const, completeness: 'partial' as const,
});

const sources = [{ id: 'source-new', file_id: 'contract-new', locator_type: 'paragraph', location: { paragraph: 3 },
  excerpt: '完全虚构劳动合同期限内容', provenance: 'locally_located' }];
const baseFact = { analysis_id: 'current', employee_id: 'snapshot-one', record_id: 'record-one',
  filename: 'synthetic-renewal-contract.docx', verification_status: 'verified', version: 0, human_confirmed: false,
  revision_valid: false, latest_revision: null as Record<string, unknown> | null, sources, owner_signature: '1'.repeat(64), source_signature: '2'.repeat(64),
  context_signature: '', selected_support: true, basis_pending: false, source_valid: true,
  confirmation_context: 'fact_owner_and_material', read_only: false };

function facts() {
  return [
    { ...baseFact, id: 'fact-start', file_id: 'contract-new', fact_type: 'employment.contract.start_date',
      original_value: '2025-01-03', effective_value: '2025-01-03', value_spec: { editable: true, input_type: 'date', options: [], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-assessment', file_id: 'assessment-file', filename: 'synthetic-assessment.docx',
      fact_type: 'employment.probation.assessment_exists', original_value: null, effective_value: null,
      verification_status: 'needs_human_confirmation', sources: [{ ...sources[0], id: 'source-assessment', file_id: 'assessment-file',
        excerpt: '', location: {}, provenance: 'unlocated_needs_review' }],
      value_spec: { editable: true, input_type: 'boolean', options: [], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-probation-start', file_id: 'contract-new', fact_type: 'employment.probation.start_date',
      original_value: '2025-01-03', effective_value: '2025-01-03',
      value_spec: { editable: true, input_type: 'date', options: [], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-probation-end', file_id: 'contract-new', fact_type: 'employment.probation.end_date',
      original_value: '2025-03-03', effective_value: '2025-03-03',
      value_spec: { editable: true, input_type: 'date', options: [], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-contract-exists', file_id: 'assessment-file', filename: 'synthetic-assessment.docx',
      fact_type: 'employment.contract.exists', original_value: true, effective_value: true,
      verification_status: 'needs_human_confirmation', sources: [{ ...sources[0], id: 'source-contract-exists', file_id: 'assessment-file',
        excerpt: '', location: {}, provenance: 'unlocated_needs_review' }],
      value_spec: { editable: true, input_type: 'boolean', options: [], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-periods', file_id: 'contract-old', filename: 'synthetic-old-contract.docx',
      fact_type: 'employment.probation.periods', original_value: [['2023-01-01', '2023-03-01'], ['2025-01-03', '2025-03-03']],
      effective_value: [['2023-01-01', '2023-03-01'], ['2025-01-03', '2025-03-03']], selected_support: false,
      value_spec: { editable: true, input_type: 'periods', options: [], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-type', file_id: 'contract-new', fact_type: 'employment.contract.type',
      original_value: 'fixed', effective_value: 'fixed',
      value_spec: { editable: true, input_type: 'enum', options: ['fixed', 'indefinite'], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-settlement', file_id: 'contract-old', filename: 'synthetic-old-contract.docx',
      fact_type: 'employment.termination.settlement_materials', original_value: null, effective_value: null,
      value_spec: { editable: true, input_type: 'list', options: ['final_pay', 'separation_certificate', 'work_handover'], max_items: 50, max_length: 200, nullable: true } },
    { ...baseFact, id: 'fact-entities', file_id: 'contract-new', fact_type: 'employment.entities',
      original_value: ['合成用工主体'], effective_value: ['合成用工主体'],
      value_spec: { editable: true, input_type: 'list', options: [], max_items: 50, max_length: 200, nullable: true } },
  ];
}

function effectiveServer(options: { unknownFact?: boolean; conflictFact?: boolean; conflictDecision?: boolean; unknownDecision?: boolean;
  unknownResult?: boolean; noEvidence?: boolean; initiallyFresh?: boolean; paginationFailures?: boolean; dependencyChanges?: boolean } = {}) {
  const rows = facts(); let currentRevision = revision(false); let requestRevision: Record<string, unknown> | null = null;
  const histories = new Map<string, Record<string, unknown>[]>();
  let currentContract: Record<string, unknown> | null = null; let checkDate = '2026-09-01'; let checkDateVersion = 0;
  let contractDependencySignature = '3'.repeat(64);
  let requestDecision: Record<string, unknown> | null = null; let requestResult: Record<string, unknown> | null = null;
  let factLookups = 0; let decisionLookups = 0; let resultLookups = 0;
  if (options.initiallyFresh) currentRevision = revision(true);
  if (options.noEvidence) currentRevision = { ...currentRevision, availability: 'none', completeness: 'pending' };
  const posts: string[] = [];
  const api = vi.fn(async (path: string, init?: RequestInit): Promise<Response> => {
    const method = init?.method ?? 'GET';
    if (path.includes('/effective-facts/') && path.includes('/revisions?') && method === 'GET') {
      const factId = path.split('/effective-facts/')[1].split('/')[0]; const items = histories.get(factId) ?? [];
      return json({ items, total: items.length, page: 1, page_size: 20, pages: items.length ? 1 : 0 });
    }
    if (path.includes('/effective-facts/') && path.endsWith('/revisions') && method === 'POST') {
      posts.push(path); const body = JSON.parse(String(init?.body));
      if (options.conflictFact) return json({ detail: { code: 'FACT_VERSION_CONFLICT' } }, 409);
      const index = rows.findIndex(row => row.id === path.split('/').at(-2));
      const value = body.kind === 'confirm' ? rows[index].effective_value : body.value;
      requestRevision = { id: body.id, fact_id: rows[index].id, version: rows[index].version + 1, kind: body.kind,
        value, reason: body.reason, employee_id: 'snapshot-one', record_id: 'record-one', actor: 'local-user',
        created_at: '2026-09-08T09:00:00', context_signature: rows[index].context_signature };
      histories.set(rows[index].id, [requestRevision]);
      rows[index] = { ...rows[index], effective_value: value, version: rows[index].version + 1,
        human_confirmed: true, revision_valid: true, latest_revision: requestRevision };
      currentRevision = { ...currentRevision, input_revision: 'b'.repeat(64), fresh: false };
      if (options.dependencyChanges) contractDependencySignature = '5'.repeat(64);
      if (options.unknownFact) throw new Error('DESKTOP_REQUEST_TIMEOUT');
      return json(rows[index]);
    }
    if (path.includes('/effective-facts')) {
      const requested = path.includes('request_id='); const page = Number(new URL(path, 'http://local').searchParams.get('page') ?? 1);
      if (options.paginationFailures && page === 2) return json({ detail: { code: 'DESKTOP_CONNECTION_FAILED' } }, 503);
      const delayed = requested && options.unknownFact && factLookups++ < 2;
      return json({ items: rows, total: rows.length, page, page_size: 50, pages: options.paginationFailures ? 2 : 1,
        read_only: false, request_revision: requested && !delayed ? requestRevision : null, assessment_revision: currentRevision });
    }
    if (path.endsWith('/assessment-decisions') && method === 'POST') {
      posts.push(path); const body = JSON.parse(String(init?.body));
      if (options.conflictDecision) { checkDate = '2026-09-02'; checkDateVersion += 1;
        return json({ detail: { code: 'ASSESSMENT_VERSION_CONFLICT' } }, 409); }
      if (body.kind === 'current_contract' && body.expected_dependency_signature !== contractDependencySignature) {
        return json({ detail: { code: 'ASSESSMENT_VERSION_CONFLICT' } }, 409);
      }
      if (body.kind === 'check_date') { checkDate = body.value; checkDateVersion += 1; }
      else {
        currentContract = { ...body, version: 1, employee_id: 'snapshot-one', created_at: '2026-09-08T09:01:00', actor: 'local-user', dependency_signature: '3'.repeat(64) };
        const assessment = rows.find(row => row.id === 'fact-assessment');
        if (assessment) Object.assign(assessment, { effective_value: null, human_confirmed: false, revision_valid: false,
          context_signature: '4'.repeat(64), confirmation_context: 'current_contract_period' });
      }
      currentRevision = { ...currentRevision, input_revision: 'b'.repeat(64), fresh: false, check_date: checkDate, check_date_explicit: true };
      requestDecision = body.kind === 'check_date' ? { ...body, version: checkDateVersion, record_id: null, employee_id: null,
        actor: 'local-user', created_at: '2026-09-08T09:01:00', dependency_signature: '' } : currentContract;
      if (options.unknownDecision) throw new Error('DESKTOP_REQUEST_TIMEOUT');
      return json(requestDecision);
    }
    if (path.includes('/assessment-decisions')) {
      const requested = path.includes('request_id='); const page = Number(new URL(path, 'http://local').searchParams.get('page') ?? 1);
      if (options.paginationFailures && page === 2) return json({ detail: { code: 'DESKTOP_CONNECTION_FAILED' } }, 503);
      const delayed = requested && options.unknownDecision && decisionLookups++ < 2;
      return json({ items: [], total: 0, page, page_size: 20, pages: options.paginationFailures ? 2 : 0,
      request_decision: requested && !delayed ? requestDecision : null, read_only: false, check_date: checkDate, check_date_version: checkDateVersion,
      current_contract: currentContract, current_contract_version: currentContract ? 1 : 0,
      current_contract_valid: Boolean(currentContract), contract_dependency_signature: contractDependencySignature,
      contract_file_ids: ['contract-new', 'contract-old'], assessment_revision: currentRevision });
    }
    if (path.endsWith('/reevaluate') && method === 'POST') {
      posts.push(path); const body = JSON.parse(String(init?.body)); currentRevision = revision(true);
      requestResult = { id: body.id, result_revision: currentRevision.result_revision, analysis_id: 'current',
        input_revision: currentRevision.input_revision, check_date: currentRevision.check_date, evaluated_at: currentRevision.evaluated_at };
      if (options.unknownResult) throw new Error('DESKTOP_REQUEST_TIMEOUT');
      return json(currentRevision);
    }
    if (path.includes('/assessment-results')) { const requested = path.includes('request_id=');
      const delayed = requested && options.unknownResult && resultLookups++ < 2;
      return json({ items: [], total: 0, page: 1, page_size: 20, pages: 0,
        request_result: requested && !delayed ? requestResult : null, assessment_revision: currentRevision }); }
    return json({}, 404);
  });
  return { api, posts, currentRevision: () => currentRevision, contractDependencySignature: () => contractDependencySignature };
}

async function reconcile(server: ReturnType<typeof effectiveServer>, name: string) {
  const before = server.api.mock.calls.filter(([path]) => String(path).includes('request_id=')).length;
  fireEvent.click(await screen.findByRole('button', { name }));
  await waitFor(() => expect(server.api.mock.calls.filter(([path]) => String(path).includes('request_id=')).length).toBe(before + 1));
}

it.each(['version', 'owner_signature', 'source_signature', 'context_signature', 'unchanged'])('retains authored fact basis across actual App Settings: %s', async changedField => {
  const effective = effectiveServer(); let changed = false;
  mountActualApp(effective, async (path, init) => {
    const response = await effective.api(path, init);
    if (!init?.method && path.includes('/effective-facts?') && changed && changedField !== 'unchanged') {
      const data = await response.json();
      data.items = data.items.map((row: { id: string }) => row.id === 'fact-start'
        ? { ...row, [changedField]: changedField === 'version' ? 1 : '6'.repeat(64), effective_value: '2027-03-01' } : row);
      return json(data);
    }
    return response;
  });
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  const region = await screen.findByRole('region', { name: '合同开始日期' });
  fireEvent.click(within(region).getByRole('button', { name: /展开/ }));
  fireEvent.change(within(region).getByLabelText('合同开始日期当前值'), { target: { value: '2026-04-01' } });
  fireEvent.change(within(region).getByLabelText('更正或确认理由'), { target: { value: '原始证据下的合成草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '设置' })); changed = true;
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  const restored = await screen.findByRole('region', { name: '合同开始日期' });
  expect(within(restored).getByLabelText('合同开始日期当前值')).toHaveValue('2026-04-01');
  expect(within(restored).getByLabelText('更正或确认理由')).toHaveValue('原始证据下的合成草稿');
  if (changedField !== 'unchanged') {
    expect(within(restored).getByRole('button', { name: '保存更正' })).toBeDisabled();
    fireEvent.click(within(restored).getByRole('button', { name: '我已重新核对，允许再次保存' }));
  }
  expect(within(restored).getByRole('button', { name: '保存更正' })).toBeEnabled();
  expect(effective.posts).toHaveLength(0);
});

function mountActualApp(effective = effectiveServer(), overrideEffective?: (path: string, init?: RequestInit) => Promise<Response>) {
  const server = companyServer({ override: async (path, init) => {
    if (path.endsWith('/employees/snapshot-one')) return json({ employee: { id: 'snapshot-one', masked_name: '合成员**',
      employee_number: 'SYN-1', department: '合成部', job_title: '合成岗位', employment_status: 'active', match_status: 'confirmed' },
      findings: [], assessment_revision: effective.currentRevision() });
    return path.includes('/effective-facts') || path.includes('/assessment-') || path.endsWith('/reevaluate')
      ? (overrideEffective ? overrideEffective(path, init) : effective.api(path, init)) : undefined;
  } });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <App backendLoader={async () => ({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' })}
      apiFactory={() => server.request} configurationLoader={async () => syntheticConfiguration} />
  </QueryClientProvider>);
  return { effective, server };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>(done => { resolve = done; }), resolve };
}

function mountTwoEmployeeApp(effective: ReturnType<typeof effectiveServer>,
  overrideEffective?: (path: string, init?: RequestInit) => Promise<Response>,
  overrideSnapshotA?: (read: number) => Promise<Response> | undefined) {
  const employeeA = { id: 'record-one', company_id: 'company-one', masked_name: '合成员**', employee_number: 'SYN-1',
    department: '合成部', job_title: '合成岗位', lifecycle_status: 'active', version: 0, created_at: '2026-09-01T00:00:00' };
  const employeeB = { id: 'record-two', company_id: 'company-one', masked_name: '合成员乙**', employee_number: 'SYN-2',
    department: '合成乙部', job_title: '合成乙岗', lifecycle_status: 'active', version: 0, created_at: '2026-09-01T00:00:00' };
  const revisionB = { ...revision(true), input_revision: 'd'.repeat(64), evaluated_input_revision: 'd'.repeat(64), result_revision: 'result-b' };
  let snapshotAReads = 0;
  const server = companyServer({ override: async (path, init) => {
    if (path.startsWith('/api/company-workspaces/company-one/current?')) {
      const bindingA = { snapshot_id: 'snapshot-one', employee_record_id: employeeA.id, company_id: 'company-one', analysis_id: 'current', created_at: employeeA.created_at };
      return json({ company: { id: 'company-one', display_name: '完全虚构企业', version: 0, created_at: employeeA.created_at },
        current_analysis: { analysis_id: 'current', company_id: 'company-one', role: 'current', assessment_profile: 'labor_materials_v1',
          status: 'completed', current_stage: 'completed', progress: 100, analysis_version: 0, company_version: 0, stale: false, pending_identity_count: 0 },
        enrolled_employee_count: 2, pending_identity_count: 0, total: 2, page: 1, page_size: 25, pages: 1,
        employees: [{ ...employeeA, current_binding: bindingA, snapshot_employee_id: 'snapshot-one', employment_status: 'active',
          assessment_state: 'evaluated', assessment: { risk_counts: { high: 1, medium: 0 }, insufficient_data_count: 1,
            requires_human_review_count: 1, material_coverage: 0.6 } }, {
        ...employeeB, current_binding: { snapshot_id: 'snapshot-two', employee_record_id: employeeB.id, company_id: 'company-one',
          analysis_id: 'current', created_at: employeeB.created_at }, snapshot_employee_id: 'snapshot-two', employment_status: 'active',
        assessment_state: 'evaluated', assessment: { risk_counts: { high: 0, medium: 0 }, insufficient_data_count: 0,
          requires_human_review_count: 0, material_coverage: 1 } }] });
    }
    if (path === '/api/company-workspaces/company-one/employees/record-two') return json({ ...employeeB,
      bindings: [{ snapshot_id: 'snapshot-two', employee_record_id: employeeB.id, company_id: 'company-one', analysis_id: 'current', created_at: employeeB.created_at }],
      current_binding: { snapshot_id: 'snapshot-two', employee_record_id: employeeB.id, company_id: 'company-one', analysis_id: 'current', created_at: employeeB.created_at },
      historical_bindings: [] });
    if (path.endsWith('/employees/snapshot-one')) { snapshotAReads += 1; const overridden = overrideSnapshotA?.(snapshotAReads);
      if (overridden) return overridden; return json({ employee: { ...employeeA, id: 'snapshot-one',
        employment_status: 'active', match_status: 'confirmed' }, findings: [], assessment_revision: effective.currentRevision() }); }
    if (path.endsWith('/employees/snapshot-two')) return json({ employee: { ...employeeB, id: 'snapshot-two', employment_status: 'active',
      match_status: 'confirmed' }, findings: [], assessment_revision: revisionB });
    return path.includes('/effective-facts') || path.includes('/assessment-') || path.endsWith('/reevaluate')
      ? (overrideEffective ? overrideEffective(path, init) : effective.api(path, init)) : undefined;
  } });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <App backendLoader={async () => ({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' })}
      apiFactory={() => server.request} configurationLoader={async () => syntheticConfiguration} />
  </QueryClientProvider>);
  return { server, revisionB, snapshotAReads: () => snapshotAReads };
}

async function openEmployee(name: string) {
  fireEvent.click(screen.getByRole('button', { name: `查看${name}详情` }));
  await screen.findByRole('heading', { name });
}

function mountWorkbench(server = effectiveServer(), scope = { companyId: 'company-one', analysisId: 'current', recordId: 'record-one' },
  stores = { factDrafts: new Map() as FactRevisionDrafts, factRequests: new Map() as FactRevisionRequests,
    decisionDrafts: new Map() as AssessmentDecisionDrafts, decisionRequests: new Map() as AssessmentDecisionRequests,
    reevaluationRequests: new Map() as ReevaluationRequests }) {
  const view = render(<EmployeeFactWorkbench {...scope} api={server.api} {...stores} onReevaluated={vi.fn()} />);
  return { server, stores, view };
}

function expandFact(name: string) {
  const region = screen.getByRole('region', { name });
  const toggle = within(region).queryByRole('button', { name: `展开核对${name}` });
  if (toggle) fireEvent.click(toggle);
  return region;
}

it('uses concrete date/boolean controls, preserves provenance and history, and reevaluates locally without provider calls', async () => {
  const { server } = mountWorkbench();
  expect(await screen.findByRole('heading', { name: '已识别事实与当前评估' })).toBeInTheDocument();
  const start = expandFact('合同开始日期');
  const assessment = expandFact('试用期考核记录');
  expandFact('历次试用期区间');
  expect(screen.getByText('原始识别值：2025-01-03')).toBeInTheDocument();
  expect(screen.getAllByText(/本地已定位.*第 3 段/).length).toBeGreaterThan(0);
  expect(screen.getByText(/原文未定位.*人工确认不等于解析器已定位/)).toBeInTheDocument();
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-01-01' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '对照续签合同首页日期' } });
  fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  expect(await within(start).findByText(/已保存事实版本 1.*结果待重新评估/)).toBeInTheDocument();
  expect(within(assessment).getByLabelText('试用期考核记录当前值')).toHaveValue('unknown');
  fireEvent.change(within(assessment).getByLabelText('试用期考核记录当前值'), { target: { value: 'yes' } });
  fireEvent.change(within(assessment).getByLabelText('更正或确认理由'), { target: { value: '已查看合成考核材料' } });
  fireEvent.click(within(assessment).getByRole('button', { name: '保存更正' }));
  expect(await within(assessment).findByText(/人工已确认.*并未改写原始来源/)).toBeInTheDocument();
  const contractExists = expandFact('书面劳动合同');
  fireEvent.change(within(contractExists).getByLabelText('更正或确认理由'), { target: { value: '已对照合成合同原文' } });
  fireEvent.click(within(contractExists).getByRole('button', { name: '确认当前识别值' }));
  expect(await within(contractExists).findByText(/已保存事实版本 1/)).toBeInTheDocument();
  expect(within(contractExists).getByText(/原文未定位.*人工确认不等于解析器已定位/)).toBeInTheDocument();
  expect(screen.getAllByText('2023-01-01 至 2023-03-01').length).toBeGreaterThan(0);
  expect(screen.getAllByText('2025-01-03 至 2025-03-03').length).toBeGreaterThan(0);
  fireEvent.click(within(start).getByRole('button', { name: '查看修订历史' }));
  expect(await within(start).findByText(/对照续签合同首页日期/)).toBeInTheDocument();
  expect(screen.getAllByText(/核查日期来自历史创建日期回退/).length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText('当前合同材料'), { target: { value: 'contract-new' } });
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: '续签合同为当前履行版本' } });
  fireEvent.click(screen.getByRole('button', { name: '保存当前合同选择' }));
  expect(await screen.findByText(/当前参与材料：synthetic-renewal-contract.docx/)).toBeInTheDocument();
  await waitFor(() => expect(within(assessment).getByLabelText('试用期考核记录当前值')).toHaveValue('unknown'));
  expect(within(assessment).getByText(/synthetic-renewal-contract\.docx.*2025-01-03 至 2025-03-03/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '仅用已保存事实重新评估' }));
  expect(await screen.findByText(/本地重新评估已完成.*结果版本 result-new/)).toBeInTheDocument();
  expect(screen.getByText(/部分可用.*仍有材料未完整读取/)).toBeInTheDocument();
  expect(server.posts.filter(path => path.endsWith('/reevaluate'))).toHaveLength(1);
  expect(server.api.mock.calls.every(([path]) => !path.includes('provider') && !path.endsWith('/process'))).toBe(true);
});

it('keeps an ordinary draft separate from an unknown UUID across unmount and reconciles before any retry', async () => {
  const server = effectiveServer({ unknownFact: true }); const mounted = mountWorkbench(server);
  await screen.findByRole('region', { name: '合同开始日期' });
  const start = expandFact('合同开始日期');
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-02-01' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '合成草稿说明' } });
  fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  expect(await within(start).findByRole('button', { name: '核对事实提交结果' })).toBeInTheDocument();
  mounted.view.unmount();
  mountWorkbench(server, undefined, mounted.stores);
  const restored = await screen.findByRole('region', { name: '合同开始日期' });
  expect(within(restored).getByLabelText('合同开始日期当前值')).toHaveValue('2025-02-01');
  expect(within(restored).getByLabelText('更正或确认理由')).toHaveValue('合成草稿说明');
  await reconcile(server, '核对事实提交结果');
  expect(within(restored).getByRole('button', { name: '核对事实提交结果' })).toBeInTheDocument();
  expect(within(restored).getByText(/仍可能在处理中/)).toBeInTheDocument();
  await reconcile(server, '核对事实提交结果');
  expect(within(restored).getByRole('button', { name: '核对事实提交结果' })).toBeInTheDocument();
  await reconcile(server, '核对事实提交结果');
  expect(await within(restored).findByText('事实修订已核实保存。')).toBeInTheDocument();
  expect(server.posts.filter(path => path.includes('/effective-facts/'))).toHaveLength(1);
});

it('isolates drafts by company/analysis/record/fact and requires reconsideration after a CAS conflict', async () => {
  const server = effectiveServer({ conflictFact: true }); const mounted = mountWorkbench(server);
  await screen.findByRole('region', { name: '合同开始日期' });
  const start = expandFact('合同开始日期');
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-04-01' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '待重新核对的草稿' } });
  fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  expect(await within(start).findByText(/服务器当前值：2025-01-03/)).toBeInTheDocument();
  expect(within(start).getByText(/你的草稿值：2025-04-01/)).toBeInTheDocument();
  expect(within(start).getByRole('button', { name: '保存更正' })).toBeDisabled();
  fireEvent.click(within(start).getByRole('button', { name: '我已重新核对，允许再次保存' }));
  expect(within(start).getByRole('button', { name: '保存更正' })).toBeEnabled();
  mounted.view.rerender(<EmployeeFactWorkbench companyId="company-two" analysisId="current" recordId="record-one"
    api={server.api} {...mounted.stores} onReevaluated={vi.fn()} />);
  await screen.findByRole('region', { name: '合同开始日期' });
  const other = expandFact('合同开始日期');
  expect(within(other).getByLabelText('合同开始日期当前值')).toHaveValue('2025-01-03');
  mounted.view.rerender(<EmployeeFactWorkbench companyId="company-one" analysisId="current-two" recordId="record-one"
    api={server.api} {...mounted.stores} onReevaluated={vi.fn()} />);
  await screen.findByRole('region', { name: '合同开始日期' });
  expect(within(expandFact('合同开始日期')).getByLabelText('合同开始日期当前值')).toHaveValue('2025-01-03');
});

it('preserves null, false, empty and raw multiline drafts through Settings in the actual App', async () => {
  const { effective } = mountActualApp();
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('region', { name: '合同开始日期' });
  const start = expandFact('合同开始日期');
  const assessment = expandFact('试用期考核记录');
  const settlement = expandFact('离职交接材料');
  const entities = expandFact('材料中的用工主体');
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '设置往返仍保留' } });
  fireEvent.change(within(assessment).getByLabelText('试用期考核记录当前值'), { target: { value: 'no' } });
  fireEvent.change(within(settlement).getByLabelText('离职交接材料当前值状态'), { target: { value: 'known' } });
  const entityEditor = within(entities).getByRole('textbox', { name: '材料中的用工主体当前值' });
  fireEvent.change(entityEditor, { target: { value: '第一主体\n' } });
  expect(entityEditor).toHaveValue('第一主体\n');
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  const restored = await screen.findByRole('region', { name: '合同开始日期' });
  const restoredAssessment = expandFact('试用期考核记录'); const restoredSettlement = expandFact('离职交接材料');
  const restoredEntities = expandFact('材料中的用工主体');
  expect(within(restored).getByLabelText('合同开始日期当前值')).toHaveValue('');
  expect(within(restored).getByLabelText('更正或确认理由')).toHaveValue('设置往返仍保留');
  expect(within(restoredAssessment).getByLabelText('试用期考核记录当前值')).toHaveValue('no');
  expect(within(restoredSettlement).getByLabelText('离职交接材料当前值状态')).toHaveValue('known');
  const restoredEditor = within(restoredEntities).getByRole('textbox', { name: '材料中的用工主体当前值' });
  expect(restoredEditor).toHaveValue('第一主体\n');
  fireEvent.change(restoredEditor, { target: { value: '第一主体\n第二主体' } });
  expect(restoredEditor).toHaveValue('第一主体\n第二主体');
  expect(effective.posts).toHaveLength(0);
});

it('keeps a decision conflict blocked through Settings until explicit reconsideration', async () => {
  const { effective } = mountActualApp(effectiveServer({ conflictDecision: true }));
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  fireEvent.change(screen.getByLabelText('核查日期'), { target: { value: '2026-09-08' } });
  fireEvent.change(screen.getByLabelText('核查日期变更理由'), { target: { value: '必须保留的决定冲突' } });
  fireEvent.click(screen.getByRole('button', { name: '保存核查日期' }));
  expect(await screen.findByText('服务器当前决定：2026-09-02')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  expect(await screen.findByText('服务器当前决定：2026-09-02')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存核查日期' })).toBeDisabled();
  expect(screen.getByLabelText('核查日期')).toHaveValue('2026-09-08');
  expect(screen.getByLabelText('核查日期变更理由')).toHaveValue('必须保留的决定冲突');
  fireEvent.click(screen.getByRole('button', { name: '我已重新核对评估决定' }));
  expect(screen.getByRole('button', { name: '保存核查日期' })).toBeEnabled();
  expect(effective.posts.filter(path => path.endsWith('/assessment-decisions'))).toHaveLength(1);
});

it('refreshes a changed fact basis before the first current-contract choice and blocks the old basis while loading', async () => {
  const effective = effectiveServer({ dependencyChanges: true }); const refreshStarted = deferred<void>(); const releaseRefresh = deferred<void>();
  let holdDecisionRefresh = false;
  mountActualApp(effective, async (path, init) => {
    if (path.endsWith('/effective-facts/fact-contract-exists/revisions') && init?.method === 'POST') {
      const response = await effective.api(path, init); holdDecisionRefresh = true; return response;
    }
    if (holdDecisionRefresh && path.includes('/assessment-decisions?')) {
      holdDecisionRefresh = false; refreshStarted.resolve(); await releaseRefresh.promise;
    }
    return effective.api(path, init);
  });
  await screen.findByText('已建档员工 1 人'); fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  const contractFact = expandFact('书面劳动合同');
  fireEvent.change(within(contractFact).getByLabelText('更正或确认理由'), { target: { value: '确认合同后刷新决定依据' } });
  fireEvent.click(within(contractFact).getByRole('button', { name: '确认当前识别值' }));
  await refreshStarted.promise;
  try {
    expect(screen.getByLabelText('当前合同材料')).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存当前合同选择' })).toBeDisabled();
  } finally { releaseRefresh.resolve(); }
  expect(await within(contractFact).findByText(/已保存事实版本 1/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('当前合同材料'), { target: { value: 'contract-new' } });
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: '首次选择使用新事实依据' } });
  fireEvent.click(screen.getByRole('button', { name: '保存当前合同选择' }));
  expect(await screen.findByText(/当前合同已保存；结果待重新评估/)).toBeInTheDocument();
  const decisionCall = effective.api.mock.calls.find(([path, init]) => path.endsWith('/assessment-decisions') && init?.method === 'POST');
  expect(JSON.parse(String(decisionCall?.[1]?.body)).expected_dependency_signature).toBe('5'.repeat(64));
  expect(screen.queryByText(/服务器当前决定/)).not.toBeInTheDocument();
});

it('preserves a current-contract draft across a changed fact basis and requires explicit reconsideration', async () => {
  const effective = effectiveServer({ dependencyChanges: true }); mountActualApp(effective);
  await screen.findByText('已建档员工 1 人'); fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  fireEvent.change(screen.getByLabelText('当前合同材料'), { target: { value: 'contract-new' } });
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: '事实变化前写好的选择理由' } });
  const contractFact = expandFact('书面劳动合同');
  fireEvent.change(within(contractFact).getByLabelText('更正或确认理由'), { target: { value: '触发合同依赖变化' } });
  fireEvent.click(within(contractFact).getByRole('button', { name: '确认当前识别值' }));
  expect(await screen.findByText(/事实依据已更新.*重新核对/)).toBeInTheDocument();
  expect(screen.getByLabelText('当前合同材料')).toHaveValue('contract-new');
  expect(screen.getByLabelText('当前合同选择理由')).toHaveValue('事实变化前写好的选择理由');
  expect(screen.getByText(/当前事实依据版本：555555…5555/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存当前合同选择' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '我已重新核对评估决定' }));
  fireEvent.click(screen.getByRole('button', { name: '保存当前合同选择' }));
  expect(await screen.findByText(/当前合同已保存；结果待重新评估/)).toBeInTheDocument();
  expect(effective.posts.filter(path => path.endsWith('/assessment-decisions'))).toHaveLength(1);
});

it('refreshes the current-contract basis after a reconciled late fact commit', async () => {
  const effective = effectiveServer({ dependencyChanges: true, unknownFact: true }); mountActualApp(effective);
  await screen.findByText('已建档员工 1 人'); fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  const contractFact = expandFact('书面劳动合同');
  fireEvent.change(within(contractFact).getByLabelText('更正或确认理由'), { target: { value: '迟到提交也刷新依据' } });
  fireEvent.click(within(contractFact).getByRole('button', { name: '确认当前识别值' }));
  await screen.findByRole('button', { name: '核对事实提交结果' });
  await reconcile(effective, '核对事实提交结果'); await reconcile(effective, '核对事实提交结果'); await reconcile(effective, '核对事实提交结果');
  expect(await within(contractFact).findByText('事实修订已核实保存。')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('当前合同材料'), { target: { value: 'contract-new' } });
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: '迟到提交后的首次选择' } });
  fireEvent.click(screen.getByRole('button', { name: '保存当前合同选择' }));
  expect(await screen.findByText(/当前合同已保存；结果待重新评估/)).toBeInTheDocument();
  const decisionCall = effective.api.mock.calls.find(([path, init]) => path.endsWith('/assessment-decisions') && init?.method === 'POST');
  expect(JSON.parse(String(decisionCall?.[1]?.body)).expected_dependency_signature).toBe(effective.contractDependencySignature());
});

it('keeps decisions blocked after a failed dependent-basis GET and recovers explicitly', async () => {
  const effective = effectiveServer({ dependencyChanges: true }); let failNextDecisionRefresh = false;
  mountActualApp(effective, async (path, init) => {
    if (path.endsWith('/effective-facts/fact-contract-exists/revisions') && init?.method === 'POST') {
      const response = await effective.api(path, init); failNextDecisionRefresh = true; return response;
    }
    if (failNextDecisionRefresh && path.includes('/assessment-decisions?')) {
      failNextDecisionRefresh = false; return json({ detail: { code: 'DESKTOP_CONNECTION_FAILED' } }, 503);
    }
    return effective.api(path, init);
  });
  await screen.findByText('已建档员工 1 人'); fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  const contractFact = expandFact('书面劳动合同');
  fireEvent.change(within(contractFact).getByLabelText('更正或确认理由'), { target: { value: '模拟依赖读取失败' } });
  fireEvent.click(within(contractFact).getByRole('button', { name: '确认当前识别值' }));
  expect(await screen.findByRole('button', { name: '重新读取事实与决定依据' })).toBeInTheDocument();
  expect(screen.getByLabelText('当前合同材料')).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '重新读取事实与决定依据' }));
  await waitFor(() => expect(screen.getByLabelText('当前合同材料')).toBeEnabled());
  fireEvent.change(screen.getByLabelText('当前合同材料'), { target: { value: 'contract-new' } });
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: '恢复读取后首次选择' } });
  fireEvent.click(screen.getByRole('button', { name: '保存当前合同选择' }));
  expect(await screen.findByText(/当前合同已保存；结果待重新评估/)).toBeInTheDocument();
});

it.each([false, true])('keeps an old-basis draft blocked through Settings after failed refresh (return GET fails: %s)', async failReturn => {
  const effective = effectiveServer({ dependencyChanges: true });
  const refreshStarted = deferred<void>(); const releaseRefresh = deferred<void>();
  let committed = false; let firstRefresh = true; let failReads = true;
  mountActualApp(effective, async (path, init) => {
    if (path.endsWith('/effective-facts/fact-contract-exists/revisions') && init?.method === 'POST') {
      const response = await effective.api(path, init); committed = true; return response;
    }
    if (committed && path.includes('/assessment-decisions?') && failReads) {
      if (firstRefresh) { firstRefresh = false; refreshStarted.resolve(); await releaseRefresh.promise; }
      return json({ detail: { code: 'DESKTOP_CONNECTION_FAILED' } }, 503);
    }
    return effective.api(path, init);
  });
  await screen.findByText('已建档员工 1 人'); await openEmployee('合成员**');
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  fireEvent.change(screen.getByLabelText('当前合同材料'), { target: { value: 'contract-new' } });
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: 'S1 下已写好的选择理由' } });
  const fact = expandFact('书面劳动合同');
  fireEvent.change(within(fact).getByLabelText('更正或确认理由'), { target: { value: '确认合同形成 S2' } });
  fireEvent.click(within(fact).getByRole('button', { name: '确认当前识别值' }));
  await refreshStarted.promise;
  expect(screen.getByRole('button', { name: '保存当前合同选择' })).toBeDisabled();
  await act(async () => { releaseRefresh.resolve(); });
  await screen.findByRole('button', { name: '重新读取事实与决定依据' });
  expect(effective.posts).toHaveLength(1);
  expect(screen.queryByRole('button', { name: '核对事实提交结果' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  failReads = failReturn;
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  if (failReturn) {
    await screen.findByText(/事实读取失败/);
    expect(screen.queryByRole('button', { name: '保存当前合同选择' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重读事实' }));
    await screen.findByText(/事实读取失败/);
    expect(screen.queryByRole('button', { name: '保存当前合同选择' })).not.toBeInTheDocument();
    expect(effective.posts).toHaveLength(1);
    failReads = false;
    fireEvent.click(screen.getByRole('button', { name: '重读事实' }));
  }
  await screen.findByLabelText('当前合同材料');
  expect(screen.getByLabelText('当前合同材料')).toHaveValue('contract-new');
  expect(screen.getByLabelText('当前合同选择理由')).toHaveValue('S1 下已写好的选择理由');
  expect(screen.getByRole('button', { name: '保存当前合同选择' })).toBeDisabled();
  expect(screen.getByText(/当前事实依据版本：555555…5555/)).toBeInTheDocument();
  expect(effective.posts).toHaveLength(1);
  // Editing the reason is not an acknowledgement, including after another remount.
  fireEvent.change(screen.getByLabelText('当前合同选择理由'), { target: { value: '保留 S1 理由并补充说明' } });
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  await screen.findByLabelText('当前合同材料');
  expect(screen.getByLabelText('当前合同选择理由')).toHaveValue('保留 S1 理由并补充说明');
  expect(screen.getByRole('button', { name: '保存当前合同选择' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '我已重新核对评估决定' }));
  expect(screen.getByRole('button', { name: '保存当前合同选择' })).toBeEnabled();
  expect(effective.posts).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: '保存当前合同选择' }));
  await screen.findByText(/当前合同已保存；结果待重新评估/);
  const decisionCalls = effective.api.mock.calls.filter(([path, init]) => path.endsWith('/assessment-decisions') && init?.method === 'POST');
  expect(decisionCalls).toHaveLength(1);
  expect(JSON.parse(String(decisionCalls[0][1]?.body))).toMatchObject({ value: 'contract-new',
    reason: '保留 S1 理由并补充说明', expected_dependency_signature: '5'.repeat(64) });
});

it('keeps unresolved employee A locks and UUID on A while B stays editable through late A completion', async () => {
  const effective = effectiveServer(); const postStarted = deferred<void>(); const releasePost = deferred<void>();
  let submittedId = ''; let postCount = 0;
  const mounted = mountTwoEmployeeApp(effective, async (path, init) => {
    if (path.endsWith('/effective-facts/fact-start/revisions') && init?.method === 'POST') {
      submittedId = JSON.parse(String(init.body)).id; postCount += 1; postStarted.resolve(); await releasePost.promise;
    }
    const response = await effective.api(path, init);
    if (new URL(path, 'http://local').searchParams.get('record_id') !== 'record-two') return response;
    const payload = await response.json();
    if (path.includes('/effective-facts?')) payload.items = facts().map(row => ({ ...row, id: `b-${row.id}`,
      record_id: 'record-two', employee_id: 'snapshot-two', filename: 'synthetic-b-contract.docx' }));
    return json({ ...payload, assessment_revision: { ...revision(true), input_revision: 'd'.repeat(64),
      evaluated_input_revision: 'd'.repeat(64), result_revision: 'result-b' } });
  });
  async function switchEmployee(name: string) {
    fireEvent.click(screen.getByRole('button', { name: '员工' }));
    await screen.findByRole('button', { name: `查看${name}详情` }); await openEmployee(name);
    await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  }
  await screen.findByText('已建档员工 2 人'); await openEmployee('合成员**');
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  const start = expandFact('合同开始日期');
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-02-01' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '甲的未决更正' } });
  fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  await postStarted.promise;
  try {
    await switchEmployee('合成员乙**');
    expect(within(expandFact('合同开始日期')).getByLabelText('合同开始日期当前值')).toBeEnabled();
    expect(screen.getByLabelText('当前合同材料')).toBeEnabled();
    expect(screen.getByLabelText('核查日期')).toBeEnabled();
    expect(screen.getByRole('button', { name: '仅用已保存事实重新评估' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '核对事实提交结果' })).not.toBeInTheDocument();
    await switchEmployee('合成员**');
    const restored = expandFact('合同开始日期');
    expect(within(restored).getByLabelText('合同开始日期当前值')).toHaveValue('2025-02-01');
    expect(within(restored).getByLabelText('更正或确认理由')).toHaveValue('甲的未决更正');
    expect(within(restored).getByLabelText('合同开始日期当前值')).toBeDisabled();
    expect(screen.getByLabelText('当前合同材料')).toBeDisabled();
    expect(screen.getByRole('button', { name: '仅用已保存事实重新评估' })).toBeDisabled();
    for (let attempt = 0; attempt < 2; attempt += 1) {
      fireEvent.click(screen.getByRole('button', { name: '核对事实提交结果' }));
      await within(restored).findByText(/仍可能在处理中/);
      await waitFor(() => expect(screen.getByRole('button', { name: '核对事实提交结果' })).toBeEnabled());
    }
    const lookups = effective.api.mock.calls.filter(([path]) => path.includes('request_id='));
    expect(lookups).toHaveLength(2);
    expect(lookups.every(([path]) => new URL(path, 'http://local').searchParams.get('request_id') === submittedId)).toBe(true);
    await switchEmployee('合成员乙**');
    const bStart = expandFact('合同开始日期');
    fireEvent.change(within(bStart).getByLabelText('合同开始日期当前值'), { target: { value: '2025-03-01' } });
    fireEvent.change(within(bStart).getByLabelText('更正或确认理由'), { target: { value: '乙的独立草稿' } });
    const aReads = mounted.snapshotAReads();
    await act(async () => { releasePost.resolve(); });
    expect(mounted.snapshotAReads()).toBe(aReads);
    expect(within(bStart).getByLabelText('合同开始日期当前值')).toHaveValue('2025-03-01');
    expect(within(bStart).getByLabelText('更正或确认理由')).toHaveValue('乙的独立草稿');
    expect(within(bStart).getByRole('button', { name: '保存更正' })).toBeEnabled();
    expect(screen.getByLabelText('当前合同材料')).toBeEnabled();
    expect(screen.getByRole('button', { name: '仅用已保存事实重新评估' })).toBeEnabled();
    expect(screen.getAllByText(/结果版本：result-b/)).toHaveLength(2);
    await switchEmployee('合成员**');
    expect(screen.queryByRole('button', { name: '核对事实提交结果' })).not.toBeInTheDocument();
    expect(within(expandFact('合同开始日期')).getByLabelText('合同开始日期当前值')).toHaveValue('2025-02-01');
    expect(screen.getByLabelText('当前合同材料')).toBeEnabled();
    expect(postCount).toBe(1);
    expect(effective.posts).toHaveLength(1);
  } finally { await act(async () => { releasePost.resolve(); }); }
});

it('keeps a history-only workbench mutation-free', async () => {
  const server = effectiveServer();
  render(<EmployeeFactWorkbench companyId="company-one" analysisId="history" recordId="record-one" api={server.api}
    factDrafts={new Map()} factRequests={new Map()} decisionDrafts={new Map()} decisionRequests={new Map()}
    reevaluationRequests={new Map()} readOnly onReevaluated={vi.fn()} />);
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  expect(screen.queryByRole('button', { name: '保存更正' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '仅用已保存事实重新评估' })).not.toBeInTheDocument();
  expect(screen.getByText(/历史事实与修订记录只读/)).toBeInTheDocument();
  expect(server.posts).toHaveLength(0);
});

it('reconciles unknown decision and reevaluation UUIDs without replaying either POST', async () => {
  const server = effectiveServer({ unknownDecision: true, unknownResult: true });
  mountWorkbench(server);
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  fireEvent.change(screen.getByLabelText('核查日期'), { target: { value: '2026-09-08' } });
  fireEvent.change(screen.getByLabelText('核查日期变更理由'), { target: { value: '合成日期决定' } });
  fireEvent.click(screen.getByRole('button', { name: '保存核查日期' }));
  await reconcile(server, '核对核查日期提交结果');
  expect(screen.getByRole('button', { name: '核对核查日期提交结果' })).toBeInTheDocument();
  await reconcile(server, '核对核查日期提交结果');
  expect(screen.getByRole('button', { name: '核对核查日期提交结果' })).toBeInTheDocument();
  await reconcile(server, '核对核查日期提交结果');
  expect(await screen.findByText('评估决定已核实保存。')).toBeInTheDocument();
  expect(screen.getByLabelText('核查日期变更理由')).toHaveValue('');
  fireEvent.click(screen.getByRole('button', { name: '仅用已保存事实重新评估' }));
  await reconcile(server, '核对重新评估结果');
  expect(screen.getByRole('button', { name: '核对重新评估结果' })).toBeInTheDocument();
  await reconcile(server, '核对重新评估结果');
  expect(screen.getByRole('button', { name: '核对重新评估结果' })).toBeInTheDocument();
  await reconcile(server, '核对重新评估结果');
  expect(await screen.findByText(/本地重新评估已核实完成.*result-new/)).toBeInTheDocument();
  expect(server.posts.filter(path => path.endsWith('/assessment-decisions'))).toHaveLength(1);
  expect(server.posts.filter(path => path.endsWith('/reevaluate'))).toHaveLength(1);
});

it('makes both actual employee status panels stale after committed inputs and fresh only after reevaluation', async () => {
  const { effective } = mountActualApp(effectiveServer({ unknownFact: true, initiallyFresh: true }));
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  expect(screen.getAllByText(/当前结果与已保存事实一致/)).toHaveLength(2);
  const start = expandFact('合同开始日期');
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-02-01' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '合成事实提交' } });
  fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  await screen.findByRole('button', { name: '核对事实提交结果' });
  await reconcile(effective, '核对事实提交结果'); await reconcile(effective, '核对事实提交结果'); await reconcile(effective, '核对事实提交结果');
  await waitFor(() => { const panels = screen.getAllByLabelText('当前结果版本'); expect(panels).toHaveLength(2);
    expect(panels.every(item => item.classList.contains('is-stale'))).toBe(true); });
  fireEvent.click(screen.getByRole('button', { name: '仅用已保存事实重新评估' }));
  await waitFor(() => { const panels = screen.getAllByLabelText('当前结果版本'); expect(panels).toHaveLength(2);
    expect(panels.every(item => item.classList.contains('is-fresh'))).toBe(true); });
  fireEvent.change(screen.getByLabelText('核查日期'), { target: { value: '2026-09-08' } });
  fireEvent.change(screen.getByLabelText('核查日期变更理由'), { target: { value: '合成决定提交' } });
  fireEvent.click(screen.getByRole('button', { name: '保存核查日期' }));
  await waitFor(() => { const panels = screen.getAllByLabelText('当前结果版本'); expect(panels).toHaveLength(2);
    expect(panels.every(item => item.classList.contains('is-stale'))).toBe(true); });
  expect(effective.posts.filter(path => path.includes('/effective-facts/'))).toHaveLength(1);
});

it.each(['fact', 'reevaluation'] as const)('does not apply delayed employee A %s completion after employee B opens', async kind => {
  const effective = effectiveServer({ initiallyFresh: true }); const postStarted = deferred<void>(); const releasePost = deferred<void>();
  const mounted = mountTwoEmployeeApp(effective, async (path, init) => {
    const targeted = kind === 'fact' ? path.endsWith('/effective-facts/fact-start/revisions') : path.endsWith('/reevaluate');
    if (targeted && init?.method === 'POST') { const response = await effective.api(path, init); postStarted.resolve(); await releasePost.promise; return response; }
    return effective.api(path, init);
  });
  await screen.findByText('已建档员工 2 人'); await openEmployee('合成员**');
  if (kind === 'fact') {
    const start = expandFact('合同开始日期');
    fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-02-01' } });
    fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '延迟的甲员工事实' } });
    fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  } else fireEvent.click(screen.getByRole('button', { name: '仅用已保存事实重新评估' }));
  await postStarted.promise;
  fireEvent.click(screen.getByRole('button', { name: '员工' }));
  await screen.findByRole('button', { name: '查看合成员乙**详情' }); await openEmployee('合成员乙**');
  const readsBeforeRelease = effective.api.mock.calls.length; releasePost.resolve();
  await waitFor(() => expect(effective.api.mock.calls.length).toBeGreaterThan(readsBeforeRelease));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(mounted.snapshotAReads()).toBe(1);
  const detail = screen.getByRole('heading', { name: '合成员乙**' }).closest('.employee-detail') as HTMLElement;
  expect(within(detail).getByText(/结果版本：result-b/)).toBeInTheDocument();
});

it('does not let a delayed employee A detail refresh replace employee B', async () => {
  const effective = effectiveServer({ initiallyFresh: true }); const delayedDetail = deferred<Response>();
  const mounted = mountTwoEmployeeApp(effective, undefined, read => read === 2 ? delayedDetail.promise : undefined);
  await screen.findByText('已建档员工 2 人'); await openEmployee('合成员**');
  const start = expandFact('合同开始日期');
  fireEvent.change(within(start).getByLabelText('合同开始日期当前值'), { target: { value: '2025-02-01' } });
  fireEvent.change(within(start).getByLabelText('更正或确认理由'), { target: { value: '延迟读取的甲员工事实' } });
  fireEvent.click(within(start).getByRole('button', { name: '保存更正' }));
  await waitFor(() => expect(mounted.snapshotAReads()).toBe(2));
  fireEvent.click(screen.getByRole('button', { name: '员工' }));
  await screen.findByRole('button', { name: '查看合成员乙**详情' }); await openEmployee('合成员乙**');
  await act(async () => { delayedDetail.resolve(json({ employee: { ...mounted.server.record, id: 'snapshot-one', masked_name: '合成员**',
    employment_status: 'active', match_status: 'confirmed' }, findings: [], assessment_revision: effective.currentRevision() }));
    await Promise.resolve(); await Promise.resolve(); });
  const detail = screen.getByRole('heading', { name: '合成员乙**' }).closest('.employee-detail') as HTMLElement;
  expect(within(detail).getByText(/结果版本：result-b/)).toBeInTheDocument();
});

it('reports failed fact and decision pagination inline', async () => {
  mountWorkbench(effectiveServer({ paginationFailures: true }));
  await screen.findByRole('heading', { name: '已识别事实与当前评估' });
  fireEvent.click(screen.getByRole('button', { name: '下一页事实' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/暂时无法连接本机分析服务/);
  fireEvent.click(screen.getByRole('button', { name: '下一页决定' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/暂时无法连接本机分析服务/));
});

it('keeps false distinct from unknown and blocks an inverted structured period before POST', async () => {
  const { server } = mountWorkbench();
  await screen.findByRole('region', { name: '试用期考核记录' });
  const assessment = expandFact('试用期考核记录');
  const booleanControl = within(assessment).getByLabelText('试用期考核记录当前值');
  expect(booleanControl).toHaveValue('unknown');
  fireEvent.change(booleanControl, { target: { value: 'no' } });
  expect(booleanControl).toHaveValue('no');
  fireEvent.change(booleanControl, { target: { value: 'unknown' } });
  expect(booleanControl).toHaveValue('unknown');
  expect(within(assessment).getByRole('button', { name: '确认当前识别值' })).toBeDisabled();
  const periods = expandFact('历次试用期区间');
  fireEvent.change(within(periods).getByLabelText('第 1 段开始日期'), { target: { value: '2023-04-01' } });
  fireEvent.change(within(periods).getByLabelText('更正或确认理由'), { target: { value: '合成期间核对' } });
  expect(within(periods).getByText(/不是有效的日期、选项或期间/)).toBeInTheDocument();
  expect(within(periods).getByRole('button', { name: '保存更正' })).toBeDisabled();
  const settlement = expandFact('离职交接材料');
  const listStatus = within(settlement).getByLabelText('离职交接材料当前值状态');
  expect(listStatus).toHaveValue('unknown');
  fireEvent.change(listStatus, { target: { value: 'known' } });
  expect(listStatus).toHaveValue('known');
  expect(within(settlement).getByLabelText('工作交接')).not.toBeChecked();
  fireEvent.change(listStatus, { target: { value: 'unknown' } });
  expect(listStatus).toHaveValue('unknown');
  const entities = expandFact('材料中的用工主体');
  fireEvent.change(within(entities).getByRole('textbox', { name: '材料中的用工主体当前值' }), { target: { value: '甲'.repeat(201) } });
  fireEvent.change(within(entities).getByLabelText('更正或确认理由'), { target: { value: '合成主体核对' } });
  expect(within(entities).getByText(/不是有效的日期、选项或期间/)).toBeInTheDocument();
  expect(within(entities).getByRole('button', { name: '保存更正' })).toBeDisabled();
  expect(server.posts).toHaveLength(0);
});

it('does not offer a safe-zero reevaluation when no facts are available', async () => {
  mountWorkbench(effectiveServer({ noEvidence: true }));
  const action = await screen.findByRole('button', { name: '仅用已保存事实重新评估' });
  expect(action).toBeDisabled();
  expect(screen.getByText(/不会将缺失事实重新评估成全零风险/)).toBeInTheDocument();
});
