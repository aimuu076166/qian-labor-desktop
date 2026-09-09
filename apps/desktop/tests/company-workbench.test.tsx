import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { selectEmploymentFiles } from '../src/lib/desktop';
import { taskMetadata, taskRun } from './company-fixture';
import canonicalCreation from './fixtures/company-create-canonical.json';

vi.mock('../src/lib/desktop', () => ({ selectEmploymentFiles: vi.fn(),
  getProviderConfigurationStatus: vi.fn(), configureZhipuProvider: vi.fn(), markZhipuProviderValidated: vi.fn() }));
const company = { id: 'company-a', display_name: '合成甲企业', version: 0, created_at: '2026-09-01' };
const second = { ...company, id: 'company-b', display_name: '合成乙企业' };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const employees = Array.from({ length: 10 }, (_, index) => ({ id: `record-${index}`, company_id: company.id,
  masked_name: `合成员${index}**`, employee_number: `SYN-${index}`, department: '合成部门', job_title: null,
  lifecycle_status: 'active', version: 0, created_at: '2026-09-01', current_binding: null,
  snapshot_employee_id: null, employment_status: null, assessment_state: 'pending_evidence', assessment: null }));
const projection = { company, enrolled_employee_count: 10, current_analysis: null, employees,
  pending_identity_count: 0, total: 10, page: 1, page_size: 25, pages: 1 };
function mount(request: (path: string, init?: RequestInit) => Promise<Response>) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <App backendLoader={async () => ({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' })}
      apiFactory={() => request} configurationLoader={async () => ({ provider: 'zhipu', configured: false,
        validated: false, textModel: '', visionModel: '', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' })} />
  </QueryClientProvider>);
}
function fixture(extra?: (path: string, init?: RequestInit) => Promise<Response | undefined>) {
  return vi.fn(async (path: string, init?: RequestInit): Promise<Response> => {
    const result = await extra?.(path, init); if (result) return result;
    if (path === '/api/company-workspaces') return json([company, second]);
    if (path === '/api/workspace-preference') return json({ last_company_id: company.id, version: 0 });
    if (path.startsWith(`/api/company-workspaces/${company.id}/current?`)) return json(projection);
    if (path.startsWith(`/api/company-workspaces/${second.id}/current?`)) return json({ ...projection, company: second, employees: [], total: 0 });
    return json({}, 404);
  });
}
describe('production company workbench', () => {
  it.each(['accepted', 'timeout'] as const)('resolves actual schema-masked creation through canonical point GET: %s', async outcome => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(canonicalCreation.request.id as `${string}-${string}-${string}-${string}-${string}`);
    let sent = false, visible = false;
    const request = fixture(async (path, init) => {
      if (path === '/api/company-workspaces' && init?.method === 'POST') {
        sent = true;
        if (outcome === 'timeout') throw new Error('DESKTOP_REQUEST_TIMEOUT');
        visible = true; return json(canonicalCreation.post, 201);
      }
      if (path === '/api/company-workspaces') return json(visible ? canonicalCreation.listing : []);
      if (path === `/api/company-workspaces/${canonicalCreation.request.id}`) return visible ? json(canonicalCreation.point) : json({}, 404);
      if (path === '/api/workspace-preference') return json({ last_company_id: null, version: 0 });
      if (path.includes(`/${canonicalCreation.request.id}/current?`)) return json({ ...projection, company: canonicalCreation.point, employees: [], total: 0 });
    });
    const view = mount(request);
    fireEvent.change(await screen.findByLabelText('企业名称'), { target: { value: canonicalCreation.request.name_parts.join('') } });
    fireEvent.click(screen.getByRole('button', { name: '建立本地档案' }));
    await waitFor(() => expect(sent).toBe(true));
    if (outcome === 'timeout') {
      await waitFor(() => expect(screen.getByRole('button', { name: '核对企业档案' })).toBeEnabled());
      fireEvent.click(screen.getByRole('button', { name: '核对企业档案' }));
      await waitFor(() => expect(screen.getByRole('button', { name: '核对企业档案' })).toBeEnabled());
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
      view.unmount(); mount(request);
      await screen.findByRole('button', { name: '核对企业档案' }); visible = true;
      fireEvent.click(screen.getByRole('button', { name: '核对企业档案' }));
    }
    await waitFor(() => expect(localStorage.getItem('qian-company-creation-v1')).toBeNull());
    expect(screen.getByLabelText('当前企业')).toHaveValue(canonicalCreation.request.id);
    expect(screen.getAllByRole('option', { name: canonicalCreation.point.display_name })).toHaveLength(1);
    const posts = request.mock.calls.filter(([, init]) => init?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0][1]!.body))).toEqual({ id: canonicalCreation.request.id, display_name: canonicalCreation.request.name_parts.join('') });
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: '' } });
    fireEvent.change(await screen.findByLabelText('企业名称'), { target: { value: '下一家完全虚构企业' } });
    await waitFor(() => expect(screen.getByRole('button', { name: '建立本地档案' })).toBeEnabled());
  });
  it.each(['missing', 'failed', 'name-mismatch', 'foreign-point', 'foreign-candidate'] as const)('keeps creation unknown when authoritative point identity cannot confirm candidate: %s', async failure => {
    let original: { id: string; display_name: string } | null = null;
    let repaired = false;
    const request = fixture(async (path, init) => {
      if (path === '/api/company-workspaces' && init?.method === 'POST') {
        original = JSON.parse(String(init.body));
        return json({ ...company, ...original, ...(failure === 'foreign-candidate' ? { id: 'foreign-uuid' } : {}) }, 201);
      }
      if (path === '/api/company-workspaces') return json(repaired && original ? [{ ...company, ...original }] : []);
      if (original && path === `/api/company-workspaces/${original.id}`) {
        if (repaired) return json({ ...company, ...original });
        if (failure === 'failed') throw new Error('DESKTOP_CONNECTION_FAILED');
        if (failure === 'missing') return json({}, 404);
        return json({ ...company, ...original, ...(failure === 'name-mismatch' ? { display_name: '另一个规范名称' } : {}),
          ...(failure === 'foreign-point' ? { id: 'foreign-uuid' } : {}) });
      }
      if (path === '/api/workspace-preference') return json({ last_company_id: null, version: 0 });
    }); mount(request);
    fireEvent.change(await screen.findByLabelText('企业名称'), { target: { value: '原始合成名称' } });
    fireEvent.click(screen.getByRole('button', { name: '建立本地档案' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '核对企业档案' })).toBeEnabled());
    expect(JSON.parse(localStorage.getItem('qian-company-creation-v1')!)).toEqual(original);
    expect(screen.getByRole('button', { name: '建立本地档案' })).toBeDisabled();
    expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    repaired = true; fireEvent.click(screen.getByRole('button', { name: '核对企业档案' }));
    await waitFor(() => expect(localStorage.getItem('qian-company-creation-v1')).toBeNull());
    expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });
  it('keeps a damaged creation journal visible and blocks writes after setup GET succeeds', async () => {
    localStorage.setItem('qian-company-creation-v1', '{damaged');
    const request = fixture(); mount(request);
    expect(await screen.findByText(/无法安全保存或恢复企业建档请求/)).toBeInTheDocument();
    expect(localStorage.getItem('qian-company-creation-v1')).toBe('{damaged');
    expect(request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  });
  it('preserves the exact unresolved creation through absent reads, Settings and restart, retrying only its original body', async () => {
    let original: { id: string; display_name: string } | null = null; let visible = false;
    const submitted: string[] = [];
    const request = fixture(async (path, init) => {
      if (path === '/api/company-workspaces' && init?.method === 'POST') {
        submitted.push(String(init.body)); original ??= JSON.parse(String(init.body));
        throw new Error('DESKTOP_REQUEST_TIMEOUT');
      }
      if (path === '/api/company-workspaces') return json(visible && original ? [{ ...company, ...original }] : []);
      if (path === '/api/workspace-preference') return json({ last_company_id: null, version: 0 });
      if (original && path === `/api/company-workspaces/${original.id}`) return visible ? json({ ...company, ...original }) : json({}, 404);
      if (original && path.startsWith(`/api/company-workspaces/${original.id}/current?`)) return json({ ...projection, company: { ...company, ...original } });
    });
    const view = mount(request); fireEvent.change(await screen.findByLabelText('企业名称'), { target: { value: '原始合成企业' } });
    fireEvent.click(screen.getByRole('button', { name: '建立本地档案' }));
    for (let i = 0; i < 2; i++) {
      fireEvent.click(await screen.findByRole('button', { name: '核对企业档案' }));
      await waitFor(() => expect(screen.getByRole('button', { name: '核对企业档案' })).toBeEnabled());
      expect(screen.getByRole('button', { name: '建立本地档案' })).toBeDisabled();
    }
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    view.unmount(); mount(request);
    const retry = await screen.findByRole('button', { name: '重试原企业建档请求' });
    fireEvent.change(screen.getByLabelText('企业名称'), { target: { value: '后来编辑名称不得替换原请求' } });
    fireEvent.click(retry); await waitFor(() => expect(submitted).toHaveLength(2));
    expect(submitted[1]).toBe(submitted[0]);
    visible = true;
    fireEvent.click(await screen.findByRole('button', { name: '核对企业档案' }));
    await waitFor(() => expect(screen.getByLabelText('当前企业')).toHaveValue(original!.id));
    expect(submitted).toHaveLength(2);
  });
  it('unlocks only a definitive first create validation rejection', async () => {
    const request = fixture(async (path, init) => path === '/api/company-workspaces' && init?.method === 'POST'
      ? json({ detail: [{ type: 'string_too_long' }] }, 422) : undefined);
    mount(request); await screen.findByText('已建档员工 10 人');
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: '' } });
    fireEvent.change(await screen.findByLabelText('企业名称'), { target: { value: '合成被拒绝建档' } });
    await waitFor(() => expect(screen.getByRole('button', { name: '建立本地档案' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '建立本地档案' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '建立本地档案' })).toBeEnabled());
    expect(screen.queryByRole('button', { name: '重试原企业建档请求' })).not.toBeInTheDocument();
  });
  it('does not replace an explicit new-company choice with a late initial restoration retry', async () => {
    let listReads = 0;
    let finish!: (response: Response) => void;
    let created: typeof company | null = null;
    const request = fixture(async (path, init) => {
      if (path === '/api/company-workspaces' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)); created = { ...company, id: body.id, display_name: body.display_name };
        return json(created, 201);
      }
      if (path === '/api/company-workspaces') {
        listReads += 1;
        if (listReads === 1) throw new Error('DESKTOP_CONNECTION_FAILED');
        return new Promise<Response>(resolve => { finish = resolve; });
      }
      if (path === '/api/workspace-preference' && init?.method === 'PUT') return json({ last_company_id: created!.id, version: 1 });
      if (created && path === `/api/company-workspaces/${created.id}`) return json(created);
      if (created && path.startsWith(`/api/company-workspaces/${created.id}/current?`)) return json({ ...projection,
        company: created, enrolled_employee_count: 0, employees: [], total: 0 });
    }); mount(request);
    fireEvent.click(await screen.findByRole('button', { name: '核对企业档案' }));
    await waitFor(() => expect(finish).toBeDefined());
    fireEvent.change(screen.getByLabelText('企业名称'), { target: { value: '明确新建的合成企业' } });
    fireEvent.click(screen.getByRole('button', { name: '建立本地档案' }));
    await screen.findByText('已建档员工 0 人');
    await act(async () => { finish(json([company, second])); });
    expect(screen.getByLabelText('当前企业')).toHaveValue(created!.id);
    expect(screen.getByRole('option', { name: '明确新建的合成企业' })).toBeInTheDocument();
  });
  it.each(['/api/company-workspaces', '/api/workspace-preference'])('restores saved company on GET retry after initial %s failure', async failedPath => {
    let failed = false;
    const request = fixture(async path => {
      if (path === failedPath && !failed) { failed = true; throw new Error('DESKTOP_CONNECTION_FAILED'); }
    }); mount(request);
    fireEvent.click(await screen.findByRole('button', { name: '核对企业档案' }));
    expect(await screen.findByText('已建档员工 10 人')).toBeInTheDocument();
    expect(screen.getByLabelText('当前企业')).toHaveValue(company.id);
    expect(request.mock.calls.some(([, init]) => init?.method === 'POST' || init?.method === 'PUT')).toBe(false);
  });
  it('restores each company search after switching to another company and back', async () => {
    const request = fixture(); mount(request);
    await screen.findByText('已建档员工 10 人');
    fireEvent.change(screen.getByLabelText('搜索员工'), { target: { value: 'SYN-6' } });
    await waitFor(() => expect(request.mock.calls.some(([path]) => path.includes('search=SYN-6'))).toBe(true));
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: second.id } });
    await waitFor(() => expect(screen.getByLabelText('搜索员工')).toHaveValue(''));
    await waitFor(() => expect(screen.getByLabelText('当前企业')).toBeEnabled());
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: company.id } });
    expect(await screen.findByLabelText('搜索员工')).toHaveValue('SYN-6');
  });
  it('retains filters and the last company page after a failed filtered read', async () => {
    let fail = false;
    const request = fixture(async path => {
      if (fail && path.includes('search=unavailable')) throw new Error('DESKTOP_CONNECTION_FAILED');
    }); mount(request);
    await screen.findByText('已建档员工 10 人'); fail = true;
    fireEvent.change(screen.getByLabelText('搜索员工'), { target: { value: 'unavailable' } });
    expect(await screen.findByRole('button', { name: '重试读取员工档案' })).toBeInTheDocument();
    expect(screen.getByLabelText('搜索员工')).toHaveValue('unavailable');
    expect(screen.getAllByRole('button', { name: /查看合成员/ })).toHaveLength(10);
  });
  it('keeps browsing available during processing and only reads the current analysis', async () => {
    const request = fixture(async path => {
      if (path.startsWith(`/api/company-workspaces/${company.id}/current?`)) return json({ ...projection,
        current_analysis: { analysis_id: 'current', company_id: company.id, role: 'current', assessment_profile: 'labor_materials_v1',
          status: 'extracting', current_stage: 'extracting', progress: 45, analysis_version: 0, company_version: 0, stale: true, pending_identity_count: 0 } });
      if (path.endsWith('/processing')) return json({ analysis_id: 'current', status: 'extracting', progress: 45, current_stage: 'extracting' });
      if (path.includes('/task?')) return json(taskMetadata('current', company.id, 'extracting', taskRun('current', company.id, 'running')));
    }); mount(request);
    expect(await screen.findByText('任务正在处理')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '员工' }));
    expect(await screen.findByLabelText('搜索员工')).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '报告' }));
    expect(await screen.findByText(/当前报告为实时草稿/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '工作台' }));
    expect(await screen.findByRole('button', { name: '选择企业材料' })).toBeDisabled();
  });
  it('shows actual priority findings from the current analysis only', async () => {
    const request = fixture(async path => {
      if (path.startsWith(`/api/company-workspaces/${company.id}/current?`)) return json({ ...projection,
        current_analysis: { analysis_id: 'current', status: 'completed', stale: false } });
      if (path === '/api/analyses/current/dashboard') return json({ summary: { high_count: 1, medium_count: 0, insufficient_data_count: 2 },
        findings: [{ id: 'priority', title: '合成合同签署待核查', severity: 'high', assessment_status: 'suspected_risk', rule_id: 'R01', requires_human_review: true }] });
    }); mount(request);
    expect(await screen.findByText('合成合同签署待核查')).toBeInTheDocument();
    expect(request.mock.calls.filter(([path]) => path.endsWith('/dashboard')).every(([path]) => path === '/api/analyses/current/dashboard')).toBe(true);
  });
  it('keeps the populated 1280 by 720 home employee-first while retaining partial and stale warnings', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 720 });
    const assessmentRevision = { input_revision: 'b'.repeat(64), result_revision: 'result-old',
      evaluated_input_revision: 'a'.repeat(64), fresh: false, check_date: '2026-09-01', check_date_explicit: true,
      evaluated_at: '2026-09-01T08:00:00', read_only: false, report_review_revision: 'c'.repeat(64),
      availability: 'available', completeness: 'partial' };
    const request = fixture(async path => {
      if (path.startsWith(`/api/company-workspaces/${company.id}/current?`)) return json({ ...projection,
        current_analysis: { analysis_id: 'current', company_id: company.id, assessment_profile: 'labor_materials_v1', status: 'completed',
          stale: false, assessment_revision: assessmentRevision } });
      if (path === '/api/analyses/current/dashboard') return json({
        summary: { high_count: 2, medium_count: 1, insufficient_data_count: 3 },
        findings: [
          { id: 'p1', title: '合成重点一', severity: 'high', requires_human_review: true },
          { id: 'p2', title: '合成重点二', severity: 'high', requires_human_review: true },
          { id: 'p3', title: '合成重点三', severity: 'medium', requires_human_review: true },
        ], overview: { assessment_revision: assessmentRevision, assessment_scope: { identifier: 'labor_materials_v1',
          display_label: '劳动用工材料体检', payroll_evaluated: false, attendance_evaluated: false,
          excluded_rule_codes: ['R08'], not_evaluated_reasons: { payroll: '工资核算未评估' }, settlement_document_label: '离职结算材料' } } });
    });
    mount(request);
    expect(await screen.findByText('已建档员工 10 人')).toBeInTheDocument();
    expect(screen.getByText('结果待重新评估')).toBeInTheDocument();
    expect(screen.getByText(/部分可用，仍有材料未完整读取/)).toBeInTheDocument();
    const priorities = (await screen.findByText(/当前重点：高风险 2 · 中风险 1 · 资料不足 3/)).closest('details');
    expect(priorities).not.toHaveAttribute('open');
    expect(screen.getAllByRole('button', { name: /查看合成员/ }).slice(0, 3)).toHaveLength(3);
  });
  it('restores selected local company and ten canonical records without global latest or provider calls', async () => {
    const request = fixture(); mount(request);
    expect(await screen.findByText('已建档员工 10 人')).toBeInTheDocument();
    expect(screen.getByRole('main')).toHaveClass('workbench-shell');
    expect(screen.getAllByRole('button', { name: /查看合成员/ })).toHaveLength(10);
    expect(screen.getAllByRole('cell', { name: '待补材料' })).toHaveLength(10);
    expect(screen.getAllByText('待确认')).toHaveLength(10);
    expect(screen.queryByText('高 0 · 中 0')).not.toBeInTheDocument();
    expect(request.mock.calls.every(([path, init]) => path !== '/api/analyses/latest' && (!init?.method || init.method === 'GET'))).toBe(true);
    for (const name of ['工作台', '员工', '材料', '报告', '设置']) expect(screen.getByRole('button', { name })).toBeEnabled();
  });
  it('keeps local setup in the same shell and reconciles an uncertain creation by caller UUID', async () => {
    let created: typeof company | null = null;
    const request = fixture(async (path, init) => {
      if (path === '/api/company-workspaces' && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)); created = { ...company, id: body.id, display_name: body.display_name };
        throw new Error('DESKTOP_REQUEST_TIMEOUT');
      }
      if (path === '/api/company-workspaces') return json(created ? [created] : []);
      if (path === '/api/workspace-preference') return json({ last_company_id: null, version: 0 });
      if (created && path === `/api/company-workspaces/${created.id}`) return json(created);
      if (created && path.startsWith(`/api/company-workspaces/${created.id}/current?`)) return json({ ...projection, company: created, employees: [], total: 0, enrolled_employee_count: 0 });
    }); mount(request);
    fireEvent.change(await screen.findByLabelText('企业名称'), { target: { value: '全新合成企业' } });
    fireEvent.click(screen.getByRole('button', { name: '建立本地档案' }));
    expect(await screen.findByText('已建档员工 0 人')).toBeInTheDocument();
    expect(request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    expect(created!.id).toMatch(/^[0-9a-f-]{36}$/);
  });
  it('does not create a current corpus after picker cancel', async () => {
    vi.mocked(selectEmploymentFiles).mockResolvedValue([]); const request = fixture(); mount(request);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    await waitFor(() => expect(selectEmploymentFiles).toHaveBeenCalled());
    expect(request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  });
  it('uses server search/filter/page and retains employee tab context through settings', async () => {
    const request = fixture(); mount(request);
    fireEvent.click(await screen.findByRole('button', { name: '员工' }));
    fireEvent.change(await screen.findByLabelText('搜索员工'), { target: { value: 'SYN-4' } });
    fireEvent.change(screen.getByLabelText('评估状态'), { target: { value: 'pending_evidence' } });
    await waitFor(() => expect(request.mock.calls.some(([path]) => path.includes('search=SYN-4') && path.includes('assessment_state=pending_evidence'))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(screen.getByRole('main')).not.toHaveClass('workbench-shell');
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(await screen.findByLabelText('搜索员工')).toHaveValue('SYN-4');
    expect(screen.getByLabelText('评估状态')).toHaveValue('pending_evidence');
  });
  it('a late company read cannot replace the newly selected company', async () => {
    let resolve!: (response: Response) => void;
    const request = fixture(async path => path.startsWith(`/api/company-workspaces/${company.id}/current?`)
      ? new Promise<Response>(done => { resolve = done; }) : undefined);
    mount(request);
    await screen.findByRole('option', { name: second.display_name });
    fireEvent.change(await screen.findByLabelText('当前企业'), { target: { value: second.id } });
    expect(await screen.findByText('已建档员工 10 人')).toBeInTheDocument();
    await act(async () => { resolve(json(projection)); });
    expect(screen.getByLabelText('当前企业')).toHaveValue(second.id);
    expect(screen.queryByRole('button', { name: /查看合成员0/ })).not.toBeInTheDocument();
  });
});
