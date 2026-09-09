import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { companyServer, json, renderCompany, syntheticFinding, syntheticConfiguration, taskMetadata, taskRun, taskReceipt } from './company-fixture';

const originalScope = { identifier: 'legacy_full_v1', display_label: '合成旧版完整范围', excluded_rule_codes: [], excluded_rule_ids: [],
  not_evaluated_reasons: {}, payroll_evaluated: true, attendance_evaluated: true, settlement_document_label: '旧版结算' };
const historical = { id: 'history-one', name: '合成已归属历史', company_display_name: '同名公司', assessment_scope: originalScope,
  status: 'completed', file_count: 2, employee_count: 2, created_at: '2025-01-02T00:00:00',
  relation: 'historical', company_id: 'company-one', can_adopt: false, adoption_blocker_code: 'WORKSPACE_ANALYSIS_ALREADY_BOUND' };
const legacy = { ...historical, id: 'legacy', name: '合成未归属记录', company_id: null, relation: 'unbound', can_adopt: true, adoption_blocker_code: null };
const snapshot = (id: string) => ({ id, masked_name: `合成${id}`, employee_number: null, department: '合成部',
  job_title: null, employment_status: 'active', match_status: 'confirmed', risk_counts: { high: 0, medium: 0 },
  insufficient_data_count: 0, requires_human_review_count: 0, material_coverage: 1 });
function historyServer(options: { failPut?: boolean; savedAfterFailure?: boolean; failReconcile?: boolean;
  override?: (path: string, init?: RequestInit) => Promise<Response | undefined> } = {}) {
  let putCount = 0;
  const server = companyServer({ override: async (path, init) => {
    const override = await options.override?.(path, init); if (override) return override;
    if (path.startsWith('/api/company-workspaces/company-one/analyses?')) {
      const params = new URLSearchParams(path.split('?')[1]); const page = Number(params.get('page'));
      return json({ page, pages: 2, total: 26, page_size: 25, items: [params.get('relation') === 'unbound' ? legacy : { ...historical, name: `${historical.name}${page}` }] });
    }
    if (path.endsWith('/legacy/binding')) {
      if (init?.method === 'PUT') { putCount++; if (options.failPut) throw new Error('network'); }
      if (putCount && options.failReconcile && init?.method !== 'PUT') return json({}, 503);
      return json({ company_id: 'company-one', analysis_id: 'legacy', bound: putCount > 0 && (!options.failPut || options.savedAfterFailure),
        role: putCount > 0 && (!options.failPut || options.savedAfterFailure) ? 'historical' : null,
        company_version: putCount ? 4 : 2, analysis_version: putCount ? 5 : 3,
        bindings: [], excluded_merged_snapshot_ids: ['merged'] });
    }
    if (path.startsWith('/api/analyses/legacy/employees?')) {
      const page = Number(new URLSearchParams(path.split('?')[1]).get('page'));
      return json({ page, pages: 2, page_size: 100, total: 3, items: page === 1 ? [snapshot('one'), snapshot('merged')] : [snapshot('two')], department_options: [] });
    }
    if (path.startsWith('/api/company-workspaces/company-one/employees?')) {
      const params = new URLSearchParams(path.split('?')[1]); const page = Number(params.get('page'));
      return json({ page, pages: 2, total: 26, page_size: 25, items: [{ ...server.record, id: page === 1 ? 'same-name-first' : 'chosen-record',
        masked_name: '同名**', employee_number: page === 1 ? 'SYN-1' : 'SYN-26', version: 7 }] });
    }
    if (path.endsWith('/employees/chosen-record')) return json({ ...server.record, id: 'chosen-record', version: 8 });
    return undefined;
  } });
  return server;
}
async function openHistory() {
  fireEvent.click(await screen.findByRole('button', { name: '报告' }));
  fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
}
async function openAdoption() {
  await openHistory();
  fireEvent.click(await screen.findByRole('tab', { name: '未归属旧记录' }));
  fireEvent.click(await screen.findByRole('button', { name: '归属合成未归属记录' }));
  await screen.findByLabelText('合成two的归属方式');
}
function chooseCreates() {
  for (const id of ['one', 'two']) fireEvent.change(screen.getByLabelText(`合成${id}的归属方式`), { target: { value: 'create' } });
}
const puts = (server: ReturnType<typeof historyServer>) => server.request.mock.calls.filter(([, init]) => init?.method === 'PUT' && String(init.body).includes('decisions'));

describe('company historical adoption in the production App', () => {
  it('uses company/relation/page totals, preserves page through settings, and never requests global history', async () => {
    const server = historyServer(); renderCompany(server); await openHistory();
    expect(await screen.findByText('合成已归属历史1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    await screen.findByText('合成已归属历史2');
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(await screen.findByText('合成已归属历史2')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    expect(await screen.findByText('合成未归属记录')).toBeInTheDocument();
    expect(screen.queryByText('合成已归属历史2')).not.toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path.startsWith('/api/analyses?page'))).toBe(false);
    expect(server.request.mock.calls.some(([path]) => path.includes('relation=historical&page=2&page_size=25'))).toBe(true);
  });
  it('enumerates every snapshot page, excludes only binding merged IDs, and requires explicit record selection beyond page one', async () => {
    const server = historyServer(); renderCompany(server); await openAdoption();
    expect(screen.queryByLabelText('合成merged的归属方式')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认归属到企业' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('合成one的归属方式'), { target: { value: 'link' } });
    expect(screen.getByLabelText('合成one关联员工')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: '员工下一页' }));
    await screen.findByRole('option', { name: /SYN-26/ });
    fireEvent.change(screen.getByLabelText('合成one关联员工'), { target: { value: 'chosen-record' } });
    fireEvent.change(screen.getByLabelText('搜索企业员工'), { target: { value: '别的搜索' } });
    expect(screen.getByLabelText('合成one关联员工')).toHaveValue('chosen-record');
    fireEvent.change(screen.getByLabelText('合成two的归属方式'), { target: { value: 'create' } });
    fireEvent.change(screen.getByLabelText('合成two新员工姓名'), { target: { value: '合成新员工' } });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(await screen.findByLabelText('合成two新员工姓名')).toHaveValue('合成新员工');
    fireEvent.click(screen.getByRole('button', { name: '确认归属到企业' }));
    expect(await screen.findByText(/已归属到完全虚构企业/)).toBeInTheDocument();
    const body = JSON.parse(String(puts(server)[0][1]?.body));
    expect(body).toEqual({ expected_company_version: 2, expected_analysis_version: 3, decisions: [
      { action: 'link', snapshot_id: 'one', employee_record_id: 'chosen-record', expected_record_version: 7 },
      { action: 'create', snapshot_id: 'two', record: { id: expect.any(String), display_name: '合成新员工' } },
    ] });
    expect(body.decisions[1].record.id).toMatch(/^[0-9a-f-]{36}$/);
  });
  it.each([false, true])('never repeats an uncertain PUT; GET reconciliation preserves UUIDs (saved=$saved)', async saved => {
    const server = historyServer({ failPut: true, savedAfterFailure: saved }); renderCompany(server); await openAdoption(); chooseCreates();
    fireEvent.click(screen.getByRole('button', { name: '确认归属到企业' }));
    await screen.findByRole('button', { name: '核对已保存的归属' });
    expect(puts(server)).toHaveLength(1);
    expect(screen.getByRole('button', { name: '确认归属到企业' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '核对已保存的归属' }));
    if (saved) expect(await screen.findByText(/已归属到完全虚构企业/)).toBeInTheDocument();
    else {
      await screen.findByText(/版本已重新读取/);
      fireEvent.click(screen.getByRole('button', { name: '确认归属到企业' }));
      await waitFor(() => expect(puts(server)).toHaveLength(2));
      const bodies = puts(server).map(([, init]) => JSON.parse(String(init?.body)));
      expect(bodies[1].decisions).toEqual(bodies[0].decisions);
      expect(bodies[1].expected_company_version).toBe(4);
    }
    expect(puts(server)).toHaveLength(saved ? 1 : 2);
  });
  it('keeps mutations disabled when reconciliation GET fails and cancel has no mutation', async () => {
    const server = historyServer({ failPut: true, failReconcile: true }); renderCompany(server); await openAdoption(); chooseCreates();
    fireEvent.click(screen.getByRole('button', { name: '取消归属' }));
    expect(puts(server)).toHaveLength(0);
    fireEvent.click(await screen.findByRole('button', { name: '归属合成未归属记录' }));
    await screen.findByLabelText('合成two的归属方式'); chooseCreates();
    fireEvent.click(screen.getByRole('button', { name: '确认归属到企业' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对已保存的归属' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '核对已保存的归属' })).toBeEnabled());
    expect(screen.getByRole('button', { name: '确认归属到企业' })).toBeDisabled();
    expect(puts(server)).toHaveLength(1);
  });
  it('rejects duplicate canonical targets without PUT', async () => {
    const server = historyServer(); renderCompany(server); await openAdoption();
    for (const id of ['one', 'two']) {
      fireEvent.change(screen.getByLabelText(`合成${id}的归属方式`), { target: { value: 'link' } });
      fireEvent.change(screen.getByLabelText(`合成${id}关联员工`), { target: { value: 'same-name-first' } });
    }
    expect(screen.getByRole('button', { name: '确认归属到企业' })).toBeDisabled();
    expect(puts(server)).toHaveLength(0);
  });
  it('shows historical date/scope, readonly source and report, and returns to the historical list', async () => {
    const server = historyServer({ override: async path => {
      if (path === '/api/findings/history-finding') return json({ ...syntheticFinding, id: 'history-finding', analysis_id: 'history-one' });
      if (path === '/api/analyses/history-one/dashboard') return json({ summary: { analysis_id: 'history-one', status: 'completed', employee_count: 1,
        finding_count: 1, high_count: 1, medium_count: 0, insufficient_data_count: 0 }, findings: [{ ...syntheticFinding, id: 'history-finding', analysis_id: 'history-one' }] });
      return undefined;
    } }); renderCompany(server); await openHistory();
    fireEvent.click(await screen.findByRole('button', { name: '打开合成已归属历史1' }));
    expect(await screen.findByText(/历史日期：2025-01-02/)).toBeInTheDocument();
    expect(screen.getByText('历史完整评估范围：合成旧版完整范围')).toBeInTheDocument();
    fireEvent.click(within(screen.getByLabelText('风险与资料事项')).getByRole('button', { name: /合成合同事项待核查/ }));
    expect(await screen.findByText('完全虚构来源摘录')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '处理本项风险' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /返回风险概览/ }));
    fireEvent.click(await screen.findByRole('button', { name: '生成体检报告' }));
    await screen.findByText('报告草稿（读取时生成，尚未锁定版本）');
    expect(screen.getByText(/历史日期：2025-01-02/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回历史列表' }));
    expect(await screen.findByText('合成已归属历史1')).toBeInTheDocument();
    expect(server.getAnalysisId()).toBe('current');
    expect(server.request.mock.calls.some(([, init]) => ['POST', 'DELETE'].includes(init?.method ?? 'GET'))).toBe(false);
  });
  it('opens the canonical historical snapshot by explicit binding and returns to its employee record', async () => {
    const server = historyServer({ override: async path => {
      if (path.endsWith('/employees/record-one')) return json({ ...server.record, current_binding: null, bindings: [], historical_bindings: [
        { analysis_id: 'history-one', snapshot_id: 'historical-snapshot', employee_record_id: 'record-one', company_id: 'company-one', created_at: '2025-01-02' }] });
      if (path.endsWith('/history-one/binding')) return json({ company_id: 'company-one', analysis_id: 'history-one', bound: true, role: 'historical', bindings: [], excluded_merged_snapshot_ids: [], company_version: 1, analysis_version: 1 });
      if (path.endsWith('/employees/historical-snapshot')) return json({ employee: snapshot('historical-snapshot'), findings: [] });
      return undefined;
    } }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '查看合成员**详情' }));
    fireEvent.click(await screen.findByRole('button', { name: /查看历史员工快照/ }));
    expect(await screen.findByRole('heading', { name: '合成historical-snapshot' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回员工档案' }));
    expect(await screen.findByRole('heading', { name: '合成员**' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/history-one/employees/historical-snapshot')).toBe(true);
  });
  it.each([
    { status: 'matching_review', blocker: 'WORKSPACE_ANALYSIS_NOT_SETTLED' },
    { status: 'failed', blocker: 'WORKSPACE_MATCHING_UNRESOLVED' },
  ])('offers explicit legacy matching for the real $status/$blocker contract without processing the current corpus', async ({ status, blocker }) => {
    const server = historyServer({ override: async path => {
      if (path.includes('relation=unbound')) return json({ page: 1, pages: 1, total: 1, items: [{ ...legacy, can_adopt: false,
        adoption_blocker_code: blocker, status }] });
      if (path === '/api/analyses/legacy/matching-candidates') return json({ analysis_id: 'legacy', candidates: [], current_company_id: null });
      return undefined;
    } }); renderCompany(server); await openHistory();
    fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认旧记录人员匹配' }));
    expect(await screen.findByText(/旧记录人员匹配/)).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/legacy/matching-candidates')).toBe(true);
    expect(server.request.mock.calls.some(([path, init]) => path.endsWith('/process') && init?.method === 'POST')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '返回历史列表' }));
    expect(await screen.findByText('合成未归属记录')).toBeInTheDocument();
  });
  it.each(['banner', 'reconcile'].flatMap(entry => ['completed', 'failed'].map(status => ({ entry, status }))))(
    'clears historical context on current processing $entry entry and $status result', async ({ entry, status }) => {
      let finish!: (r: Response) => void;
      let submitted!: RequestInit;
      let receiptReady = false;
      const server = historyServer({ override: async (path, init) => {
        if (path === '/api/analyses/current/task/start' && init?.method === 'POST') { submitted = init; throw new Error('network'); }
        if (path.includes('/current/task/requests/')) return json(receiptReady ? taskReceipt('current', submitted, status === 'failed' ? 'failed' : 'completed')
          : { analysis_id: 'current', company_id: 'company-one', outcome: 'unknown', request: null, run: null });
        if (path === '/api/analyses/current/processing') return new Promise<Response>(resolve => { finish = resolve; });
        if (path === '/api/analyses/history-one/workspace') return json({ analysis: { ...historical, status: 'failed' }, files: [] });
        return undefined;
      } });
      server.setStatus(entry === 'banner' ? 'queued' : 'uploading'); renderCompany(server);
      await screen.findByRole('button', { name: '查看合成员**详情' });
      if (entry === 'reconcile') {
        fireEvent.click(await screen.findByRole('button', { name: '材料' }));
        fireEvent.click(await screen.findByRole('button', { name: '开始分析' }));
        await screen.findByRole('button', { name: '核对处理状态' });
      }
      await openHistory(); fireEvent.click(await screen.findByRole('button', { name: '打开合成已归属历史1' }));
      expect(await screen.findByLabelText('历史浏览上下文')).toHaveTextContent('2025-01-02');
      fireEvent.click(screen.getByRole('button', { name: '材料' }));
      await screen.findByRole('heading', { name: '合成材料档案' });
      fireEvent.click(screen.getByRole('button', { name: '工作台' }));
      fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
      await waitFor(() => expect(finish).toBeDefined());
      server.setStatus(status);
      receiptReady = true;
      if (entry === 'reconcile') fireEvent.click(await screen.findByRole('button', { name: '核对处理状态' }));
      else fireEvent.click(await screen.findByRole('button', { name: '刷新任务状态' }));
      await act(async () => { finish(json({ analysis_id: 'current', status, progress: 100, current_stage: status,
        files: status === 'failed' ? [{ error_code: 'AI_TIMEOUT' }] : [] })); });
      if (status === 'failed') await screen.findByRole('heading', { name: '无法继续本次分析' });
      else await screen.findByRole('heading', { name: '企业用工风险概览' });
      expect(screen.queryByLabelText('历史浏览上下文')).not.toBeInTheDocument();
      expect(screen.queryByText(/历史日期：2025-01-02/)).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '继续分析此旧记录' })).not.toBeInTheDocument();
      if (status === 'failed') expect(screen.getByRole('button', { name: '重新分析' })).toBeInTheDocument();
    });
  it('clears adoption selection and ignores late reads when the selected company is cleared', async () => {
    let finish!: (r: Response) => void;
    const server = historyServer({ override: async path => path.endsWith('/legacy/binding') ? new Promise<Response>(resolve => { finish = resolve; }) : undefined });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '归属合成未归属记录' }));
    await waitFor(() => expect(finish).toBeDefined());
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: '' } });
    await act(async () => { finish(json({ company_id: 'company-one', analysis_id: 'legacy', bound: false, role: null,
      company_version: 2, analysis_version: 3, bindings: [], excluded_merged_snapshot_ids: [] })); });
    await openHistory();
    expect(await screen.findByText('请先选择企业，再查看历史记录。')).toBeInTheDocument();
    expect(screen.queryByLabelText('历史记录归属')).not.toBeInTheDocument();
  });
  it.each(['normal', 'uncertain', 'bound', 'settings'])('continues only the explicit unbound legacy target with reconciliation (%s)', async mode => {
    let processCalls = 0;
    let submitted!: RequestInit;
    const server = historyServer({ override: async (path, init) => {
      if (path.includes('relation=unbound')) return json({ page: 1, pages: 1, total: 1, page_size: 25, items: [{ ...legacy,
        status: 'uploading', can_adopt: false, adoption_blocker_code: 'WORKSPACE_ANALYSIS_NOT_SETTLED' }] });
      if (path.endsWith('/legacy/binding')) return json({ company_id: 'company-one', analysis_id: 'legacy', bound: mode === 'bound',
        role: mode === 'bound' ? 'historical' : null, company_version: 1, analysis_version: 1, bindings: [], excluded_merged_snapshot_ids: [] });
      if (path === '/api/analyses/legacy/workspace') return json({ analysis: { ...legacy, status: 'uploading' }, files: [{ id: 'old-file', filename: 'synthetic-old.txt', status: 'uploaded' }] });
      if (path.startsWith('/api/analyses/legacy/task?')) return json(taskMetadata('legacy', null, processCalls ? 'completed' : 'uploading', processCalls ? taskRun('legacy', null) : null));
      if (path === '/api/analyses/legacy/task/start') { processCalls++; submitted = init!; if (mode === 'uncertain') throw new Error('network'); return json(taskReceipt('legacy', submitted), 202); }
      if (path.includes('/legacy/task/requests/')) return json(taskReceipt('legacy', submitted));
      if (path === '/api/analyses/legacy/processing') return json({ analysis_id: 'legacy', status: processCalls ? 'completed' : 'uploading', progress: 100 });
      return undefined;
    } });
    renderCompany(server, mode === 'settings' ? { configurationLoader: async () => ({ ...syntheticConfiguration, configured: false, validated: false }) } : {});
    await openHistory(); fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开合成未归属记录' }));
    await screen.findByText('synthetic-old.txt');
    expect(processCalls).toBe(0);
    expect(screen.getByText(/模型额度/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '继续分析此旧记录' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '继续分析此旧记录' }));
    if (mode === 'settings') {
      await screen.findByRole('heading', { name: '连接智谱 GLM' });
      fireEvent.click(screen.getByRole('button', { name: '返回工作区' }));
      expect(await screen.findByText('synthetic-old.txt')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '继续分析此旧记录' })).toBeInTheDocument();
      expect(processCalls).toBe(0);
    } else if (mode === 'bound') {
      expect(await screen.findByText(/WORKSPACE_HISTORICAL_READ_ONLY/)).toBeInTheDocument(); expect(processCalls).toBe(0);
    } else if (mode === 'uncertain') {
      fireEvent.click(await screen.findByRole('button', { name: '核对处理状态' }));
      await waitFor(() => expect(server.request.mock.calls.some(([path]) => path.includes('/legacy/task/requests/'))).toBe(true));
      expect(processCalls).toBe(1);
    } else await waitFor(() => expect(processCalls).toBe(1));
    expect(server.request.mock.calls.some(([path, init]) => path === '/api/analyses/current/task/start' && init?.method === 'POST')).toBe(false);
    expect(server.getAnalysisId()).toBe('current');
  });
  it('does not submit a legacy start after its binding check returns to a changed navigation generation', async () => {
    let finish!: (response: Response) => void;
    const server = historyServer({ override: async path => {
      if (path.includes('relation=unbound')) return json({ page: 1, pages: 1, total: 1, page_size: 25, items: [{ ...legacy, status: 'uploading' }] });
      if (path.endsWith('/legacy/workspace')) return json({ analysis: { ...legacy, status: 'uploading' }, files: [{ id: 'old-file', filename: 'synthetic-old.txt', status: 'uploaded' }] });
      if (path.endsWith('/legacy/binding')) return new Promise<Response>(resolve => { finish = resolve; });
      return undefined;
    } }); renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开合成未归属记录' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '继续分析此旧记录' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '继续分析此旧记录' })); await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    await act(async () => finish(json({ company_id: 'company-one', analysis_id: 'legacy', bound: false, role: null,
      company_version: 1, analysis_version: 1, bindings: [], excluded_merged_snapshot_ids: [] })));
    expect(server.request.mock.calls.some(([path, init]) => path.includes('/task/') && init?.method === 'POST')).toBe(false);
    expect(screen.getByRole('heading', { name: '连接智谱 GLM' })).toBeInTheDocument();
  });
  it('does not leak the first company list or a late historical open into another company', async () => {
    let finish!: (r: Response) => void;
    const server = historyServer({ override: async path => {
      if (path === '/api/company-workspaces') return json([server.company, { ...server.company, id: 'company-two', display_name: '另一合成企业' }]);
      if (path.startsWith('/api/company-workspaces/company-two/current?')) return json({ ...server.projection(), company: { ...server.company, id: 'company-two' }, employees: [], current_analysis: null });
      if (path.startsWith('/api/company-workspaces/company-two/analyses?')) return json({ items: [], page: 1, total: 0, pages: 0, page_size: 25 });
      if (path === '/api/analyses/history-one/workspace') return new Promise<Response>(resolve => { finish = resolve; });
      return undefined;
    } }); renderCompany(server); await openHistory();
    fireEvent.click(await screen.findByRole('button', { name: '打开合成已归属历史1' }));
    await waitFor(() => expect(finish).toBeDefined());
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-two' } });
    await openHistory(); expect(await screen.findByText('没有历史体检记录。')).toBeInTheDocument();
    await act(async () => { finish(json({ analysis: { ...historical, status: 'failed' }, files: [] })); });
    expect(screen.getByText('没有历史体检记录。')).toBeInTheDocument();
    expect(screen.queryByText('合成已归属历史1')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('历史浏览上下文')).not.toBeInTheDocument();
  });
  it('requires successful GET of all snapshot pages and recovers a read failure without PUT', async () => {
    let failed = true;
    const server = historyServer({ override: async path => {
      if (path.startsWith('/api/analyses/legacy/employees?page=2') && failed) return json({}, 500);
      return undefined;
    } }); renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '归属合成未归属记录' }));
    await screen.findByRole('button', { name: '核对已保存的归属' });
    expect(screen.getByRole('button', { name: '确认归属到企业' })).toBeDisabled();
    failed = false; fireEvent.click(screen.getByRole('button', { name: '核对已保存的归属' }));
    await screen.findByLabelText('合成two的归属方式'); expect(puts(server)).toHaveLength(0);
  });
  it('refreshes linked record versions after CAS conflict while preserving deliberate selection', async () => {
    let submitted = false;
    const server = historyServer({ override: async (path, init) => {
      if (path.endsWith('/legacy/binding') && init?.method === 'PUT') { submitted = true; return json({ detail: { code: 'WORKSPACE_VERSION_CONFLICT' } }, 409); }
      if (path.endsWith('/legacy/binding') && submitted) return json({ company_id: 'company-one', analysis_id: 'legacy', bound: false, role: null,
        company_version: 9, analysis_version: 10, excluded_merged_snapshot_ids: ['merged'], bindings: [] });
      return undefined;
    } }); renderCompany(server); await openAdoption();
    fireEvent.change(screen.getByLabelText('合成one的归属方式'), { target: { value: 'link' } });
    fireEvent.click(screen.getByRole('button', { name: '员工下一页' })); await screen.findByRole('option', { name: /SYN-26/ });
    fireEvent.change(screen.getByLabelText('合成one关联员工'), { target: { value: 'chosen-record' } });
    fireEvent.change(screen.getByLabelText('合成two的归属方式'), { target: { value: 'create' } });
    fireEvent.click(screen.getByRole('button', { name: '确认归属到企业' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对已保存的归属' }));
    await screen.findByText(/版本已重新读取/);
    fireEvent.click(screen.getByRole('button', { name: '确认归属到企业' }));
    await waitFor(() => expect(puts(server)).toHaveLength(2));
    const body = JSON.parse(String(puts(server)[1][1]?.body));
    expect(body.expected_company_version).toBe(9);
    expect(body.decisions[0]).toEqual({ action: 'link', snapshot_id: 'one', employee_record_id: 'chosen-record', expected_record_version: 8 });
  });
  it('keeps historical readonly context when returning to the canonical record fails', async () => {
    let returning = false;
    const server = historyServer({ override: async path => {
      if (path.endsWith('/employees/record-one')) return returning ? json({}, 503) : json({ ...server.record, current_binding: null, bindings: [], historical_bindings: [
        { analysis_id: 'history-one', snapshot_id: 'historical-snapshot', employee_record_id: 'record-one', company_id: 'company-one', created_at: '2025-01-02' }] });
      if (path.endsWith('/history-one/binding')) return json({ company_id: 'company-one', analysis_id: 'history-one', bound: true, role: 'historical', bindings: [], excluded_merged_snapshot_ids: [], company_version: 1, analysis_version: 1 });
      if (path.endsWith('/employees/historical-snapshot')) return json({ employee: snapshot('historical-snapshot'), findings: [] });
      return undefined;
    } }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '查看合成员**详情' }));
    fireEvent.click(await screen.findByRole('button', { name: /查看历史员工快照/ }));
    await screen.findByRole('heading', { name: '合成historical-snapshot' }); returning = true;
    fireEvent.click(screen.getByRole('button', { name: '返回员工档案' }));
    await screen.findByRole('button', { name: '重试读取结果' });
    expect(screen.getByLabelText('历史浏览上下文')).toHaveTextContent('只读浏览');
  });
  it('keeps a failed explicit legacy process on its own materials with GET retry, not current mutation controls', async () => {
    const server = historyServer({ override: async path => {
      if (path.includes('relation=unbound')) return json({ page: 1, pages: 1, total: 1, page_size: 25, items: [{ ...legacy, status: 'failed' }] });
      if (path.endsWith('/legacy/workspace')) return json({ analysis: { ...legacy, status: 'failed' }, files: [{ id: 'old-file', filename: 'synthetic-old.txt', status: 'failed', error_code: 'AI_TIMEOUT' }] });
      if (path.endsWith('/legacy/process')) return json({ status: 'queued' }, 202);
      if (path.endsWith('/legacy/processing')) return json({ analysis_id: 'legacy', status: 'failed', progress: 100, files: [{ error_code: 'AI_TIMEOUT' }] });
      return undefined;
    } }); renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('tab', { name: '未归属旧记录' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开合成未归属记录' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '继续分析此旧记录' })).toBeEnabled());
    fireEvent.click(await screen.findByRole('button', { name: '继续分析此旧记录' }));
    await waitFor(() => expect(server.request.mock.calls.some(([path]) => path.endsWith('/legacy/processing'))).toBe(true));
    expect(await screen.findByText('synthetic-old.txt')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '选择其他材料' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新分析' })).not.toBeInTheDocument();
  });
});
