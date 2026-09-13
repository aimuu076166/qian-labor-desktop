import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { companyServer, json, renderCompany } from './company-fixture';

const historical = { id: 'older', name: '合成历史体检', company_display_name: '完全虚构企业', status: 'partial',
  company_id: 'company-one', relation: 'historical', can_adopt: false, adoption_blocker_code: 'WORKSPACE_ANALYSIS_ALREADY_BOUND',
  file_count: 3, employee_count: 1, created_at: '2025-01-02T00:00:00' };
const files = ['a', 'b', 'c'].map(id => ({ id, filename: `synthetic-${id}.docx`, status: 'uploaded', progress: 0,
  detected_kind: 'unknown', classified_kind: 'unknown', error_code: null, size_bytes: 100, fact_count: 0 }));
const workspace = { analysis: { id: 'older', name: historical.name, company_display_name: historical.company_display_name, status: 'partial' }, files };
function serverFor(options: { analysisId?: string | null; override?: (path: string, init?: RequestInit) => Promise<Response | undefined> } = {}) {
  return companyServer({ analysisId: options.analysisId, override: async (path, init) => {
    const result = await options.override?.(path, init); if (result) return result;
    if (path === '/api/analyses/older/workspace') return json(workspace);
    return undefined;
  } });
}
async function openHistory() {
  await screen.findByRole('button', { name: /合成员/ });
  fireEvent.click(screen.getByRole('button', { name: '报告' }));
  fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
  await screen.findByRole('button', { name: '打开合成历史体检' });
}
async function openCopy() {
  await openHistory(); fireEvent.click(screen.getByRole('button', { name: '复用合成历史体检材料' }));
  await screen.findByLabelText('选择synthetic-a.docx');
}
const writes = (server: ReturnType<typeof serverFor>) => server.request.mock.calls.filter(([, init]) => init?.method === 'POST');
const deletes = (server: ReturnType<typeof serverFor>) => server.request.mock.calls.filter(([, init]) => init?.method === 'DELETE');

describe('historical material actions in the real App', () => {
  it('refreshes stale creation CAS from the company GET and retains the caller UUID for explicit copy retry', async () => {
    const server = serverFor({ analysisId: null, override: async (path, init) => {
      if (path === '/api/company-workspaces/company-one') return json(server.company);
      if (path.endsWith('/current-analysis') && init?.method === 'POST' && JSON.parse(String(init.body)).expected_company_version !== server.company.version) return json({ detail: { code: 'WORKSPACE_VERSION_CONFLICT' } }, 409);
      if (path.endsWith('/import-historical')) return json({ analysis_id: server.getAnalysisId(), source_analysis_id: 'older', results: [
        { source_file_id: 'a', file_id: 'copy-a', status: 'imported', error_code: null }], requires_explicit_start: true, provider_quota_notice: '手动分析可能使用额度' });
      return undefined;
    } });
    renderCompany(server); await openCopy(); server.company.version = 3;
    fireEvent.click(screen.getByLabelText('选择synthetic-a.docx')); fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    await screen.findByText(/WORKSPACE_VERSION_CONFLICT/);
    expect(writes(server)).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    await screen.findByRole('table', { name: '材料复用结果' });
    const creates = writes(server).filter(([path]) => path.endsWith('/current-analysis')).map(([, init]) => JSON.parse(String(init?.body)));
    expect(creates).toEqual([{ id: expect.any(String), expected_company_version: 0 }, { id: creates[0].id, expected_company_version: 3 }]);
    expect(server.request.mock.calls.some(([path, init]) => path === '/api/company-workspaces/company-one' && !init?.method)).toBe(true);
  });
  it.each(['copy', 'delete'])('keeps unknown %s reconciliation recoverable when the other workflow is opened and cancelled during its GET', async action => {
    let waiting = false; let finish!: (response: Response) => void;
    const server = serverFor({ override: async (path, init) => {
      if (path.endsWith('/import-historical') || init?.method === 'DELETE') throw new Error('DESKTOP_REQUEST_TIMEOUT');
      if (waiting && path === `/api/analyses/${action === 'copy' ? 'current' : 'older'}/workspace`) return new Promise<Response>(resolve => { finish = resolve; });
      return undefined;
    } });
    renderCompany(server);
    if (action === 'copy') {
      await openCopy(); fireEvent.click(screen.getByLabelText('选择synthetic-a.docx')); fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
      await screen.findByRole('button', { name: '核对复用结果' }); waiting = true; fireEvent.click(screen.getByRole('button', { name: '核对复用结果' }));
    } else {
      await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
      await screen.findByRole('button', { name: '核对删除结果' }); waiting = true; fireEvent.click(screen.getByRole('button', { name: '核对删除结果' }));
    }
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: action === 'copy' ? '删除合成历史体检' : '复用合成历史体检材料' }));
    const cancel = screen.queryByRole('button', { name: action === 'copy' ? '取消删除' : '取消复用' }); if (cancel) fireEvent.click(cancel);
    waiting = false;
    await act(async () => finish(json({ ...workspace, analysis: { ...workspace.analysis, id: action === 'copy' ? 'current' : 'older' } })));
    await waitFor(() => expect(screen.getByRole('button', { name: action === 'copy' ? '确认复用所选材料' : '确认删除' })).toBeEnabled());
    expect(action === 'copy' ? writes(server) : deletes(server)).toHaveLength(1);
  });
  it('retains the unknown DELETE lock for an unbound record when another company views it', async () => {
    const other = { id: 'company-two', display_name: '另一虚构企业', version: 0, created_at: historical.created_at };
    const server = serverFor({ override: async (path, init) => {
      if (path === '/api/company-workspaces') return json([server.company, other]);
      if (path.startsWith('/api/company-workspaces/company-two/current?')) return json({ ...server.projection(), company: other, current_analysis: null, employees: [], total: 0, enrolled_employee_count: 0 });
      if (path.includes('/analyses?')) return json({ page: 1, pages: 1, total: 1, page_size: 25, items: [{ ...historical, company_id: null, relation: 'unbound' }] });
      if (init?.method === 'DELETE') throw new Error('DESKTOP_REQUEST_TIMEOUT');
      return undefined;
    } });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByRole('button', { name: '核对删除结果' });
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-two' } }); await screen.findByText('已建档员工 0 人');
    fireEvent.click(screen.getByRole('button', { name: '报告' })); fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
    fireEvent.click(await screen.findByRole('button', { name: '删除合成历史体检' }));
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled(); expect(screen.getByRole('button', { name: '核对删除结果' })).toBeInTheDocument();
    expect(deletes(server)).toHaveLength(1);
  });
  it('removes a deleted historical binding from the open canonical employee without removing the employee', async () => {
    let finish!: (response: Response) => void;
    const server = serverFor({ override: async (path, init) => {
      if (init?.method === 'DELETE') return new Promise<Response>(resolve => { finish = resolve; });
      if (path.endsWith('/employees/record-one')) return json({ ...server.record, current_binding: null, bindings: [],
        historical_bindings: [{ snapshot_id: 'old-snapshot', employee_record_id: 'record-one', company_id: 'company-one', analysis_id: 'older', created_at: historical.created_at }] });
      return undefined;
    } });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: '工作台' })); fireEvent.click(await screen.findByRole('button', { name: /合成员/ }));
    await screen.findByRole('button', { name: /查看历史员工快照/ });
    await act(async () => finish(json({ id: 'older', status: 'deleted' })));
    expect(screen.queryByRole('button', { name: /查看历史员工快照/ })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '合成员**' })).toBeInTheDocument();
  });
  it('a deleted preview cannot remain the Settings return target', async () => {
    let finish!: (response: Response) => void; let removed = false;
    const server = serverFor({ override: async (path, init) => {
      if (init?.method === 'DELETE') return new Promise<Response>(resolve => { finish = resolve; });
      if (removed && path.includes('/analyses?')) return json({ items: [], page: 1, pages: 0, total: 0, page_size: 25 });
      return undefined;
    } });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: '打开合成历史体检' })); await screen.findByRole('heading', { name: '企业用工风险概览' });
    fireEvent.click(screen.getByRole('button', { name: '设置' })); removed = true;
    await act(async () => finish(json({ id: 'older', status: 'deleted' })));
    expect(screen.getByRole('heading', { name: '连接智谱 GLM' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回工作区' })); await screen.findByText('没有历史体检记录。');
    expect(screen.queryByRole('heading', { name: '企业用工风险概览' })).not.toBeInTheDocument();
  });
  it.each(['copy', 'delete'])('late %s response refreshes only the originating company and never changes the new company', async action => {
    let finish!: (response: Response) => void;
    const other = { id: 'company-two', display_name: '另一虚构企业', version: 0, created_at: '2026-09-01T00:00:00' };
    const server = serverFor({ override: async (path, init) => {
      if (path === '/api/company-workspaces') return json([server.company, other]);
      if (path.startsWith('/api/company-workspaces/company-two/current?')) return json({ ...server.projection(), company: other, current_analysis: null, employees: [], total: 0, enrolled_employee_count: 0 });
      if (action === 'copy' && path.endsWith('/import-historical') || action === 'delete' && init?.method === 'DELETE') return new Promise<Response>(resolve => { finish = resolve; });
      return undefined;
    } });
    renderCompany(server);
    if (action === 'copy') { await openCopy(); fireEvent.click(screen.getByLabelText('选择synthetic-a.docx')); fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' })); }
    else { await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' })); }
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-two' } });
    await screen.findByText('已建档员工 0 人');
    await act(async () => finish(json(action === 'delete' ? { id: 'older', status: 'deleted' } : { analysis_id: 'current', source_analysis_id: 'older',
      results: [{ source_file_id: 'a', file_id: 'new-a', status: 'imported', error_code: null }], requires_explicit_start: true, provider_quota_notice: '手动分析可能使用额度' })));
    expect(screen.getByLabelText('当前企业')).toHaveValue('company-two'); expect(screen.getByText('已建档员工 0 人')).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: '材料复用结果' })).not.toBeInTheDocument(); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(writes(server).some(([path]) => path.includes('company-two'))).toBe(false);
  });
  it.each([true, false])('deleted historical reads cannot reopen an old dashboard after deletion (success=%s)', async success => {
    let finish!: (response: Response) => void; let removed = false;
    const server = serverFor({ override: async (path, init) => {
      if (path === '/api/analyses/older/dashboard') return new Promise<Response>(resolve => { finish = resolve; });
      if (init?.method === 'DELETE') { removed = true; return json({ id: 'older', status: 'deleted' }); }
      if (removed && path.includes('/analyses?')) return json({ items: [], page: 1, pages: 0, total: 0, page_size: 25 });
      return undefined;
    } });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '打开合成历史体检' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByText('没有历史体检记录。');
    await act(async () => finish(success ? json({ summary: { analysis_id: 'older', status: 'partial', employee_count: 1, finding_count: 0, high_count: 0, medium_count: 0, insufficient_data_count: 0 }, findings: [] }) : json({}, 500)));
    expect(screen.getByText('没有历史体检记录。')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '企业用工风险概览' })).not.toBeInTheDocument(); expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it.each([false, true])('creates once with caller UUID/CAS and reconciles unknown creation before explicit copy (readFails=%s)', async readFails => {
    let posted = false; let failRead = readFails;
    const server = serverFor({ analysisId: null, override: async (path, init) => {
      if (path.endsWith('/current-analysis') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)); server.setAnalysisId(body.id); server.setStatus('uploading'); posted = true;
        throw new Error('DESKTOP_REQUEST_TIMEOUT');
      }
      if (posted && failRead && path.endsWith('/current-analysis')) throw new Error('DESKTOP_CONNECTION_FAILED');
      if (path.endsWith('/import-historical')) return json({ analysis_id: server.getAnalysisId(), source_analysis_id: 'older',
        results: [{ source_file_id: 'a', file_id: 'new-a', status: 'imported', error_code: null }], requires_explicit_start: true, provider_quota_notice: '手动分析可能使用额度' });
      return undefined;
    } });
    renderCompany(server); await openCopy(); fireEvent.click(screen.getByLabelText('选择synthetic-a.docx'));
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    if (readFails) {
      await screen.findByRole('button', { name: '核对复用结果' });
      expect(writes(server).filter(([path]) => path.endsWith('/import-historical'))).toHaveLength(0);
      failRead = false; fireEvent.click(screen.getByRole('button', { name: '核对复用结果' }));
      await waitFor(() => expect(screen.getByRole('button', { name: '确认复用所选材料' })).toBeEnabled());
      expect(writes(server)).toHaveLength(1);
      fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    }
    await screen.findByRole('table', { name: '材料复用结果' });
    const creates = writes(server).filter(([path]) => path.endsWith('/current-analysis'));
    expect(creates).toHaveLength(1); const body = JSON.parse(String(creates[0][1]?.body));
    expect(body).toEqual({ id: expect.stringMatching(/^[0-9a-f-]{36}$/), expected_company_version: 0 });
    expect(writes(server)).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '前往当前材料' })); await screen.findByText('当前体检材料');
  });
  it('retries an uncommitted creation with the same UUID after GET confirms none', async () => {
    let failed = false;
    const server = serverFor({ analysisId: null, override: async (path, init) => {
      if (!failed && path.endsWith('/current-analysis') && init?.method === 'POST') { failed = true; throw new Error('DESKTOP_REQUEST_TIMEOUT'); }
      if (path.endsWith('/import-historical')) return json({ analysis_id: server.getAnalysisId(), source_analysis_id: 'older', results: [
        { source_file_id: 'a', file_id: null, status: 'error', error_code: 'HISTORICAL_COPY_FAILED' }], requires_explicit_start: true, provider_quota_notice: '手动分析可能使用额度' });
      return undefined;
    } });
    renderCompany(server); await openCopy(); fireEvent.click(screen.getByLabelText('选择synthetic-a.docx'));
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    await screen.findByText(/结果尚未核实。请先读取/);
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    await screen.findByRole('table', { name: '材料复用结果' });
    const creates = writes(server).filter(([path]) => path.endsWith('/current-analysis'));
    expect(creates).toHaveLength(2); expect(creates[0][1]?.body).toBe(creates[1][1]?.body);
    expect(screen.getByLabelText('选择synthetic-a.docx')).toBeChecked();
  });
  it('does not copy when concurrent creation resolves to a different caller UUID', async () => {
    const server = serverFor({ analysisId: null, override: async (path, init) => {
      if (path.endsWith('/current-analysis') && init?.method === 'POST') { server.setAnalysisId('another-current'); throw new Error('WORKSPACE_VERSION_CONFLICT'); }
      return undefined;
    } });
    renderCompany(server); await openCopy(); fireEvent.click(screen.getByLabelText('选择synthetic-a.docx'));
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    await screen.findByText(/WORKSPACE_CURRENT_CONFLICT/); expect(writes(server)).toHaveLength(1);
  });
  it('blocks unknown deletion reopening until GET reconciliation and requires explicit second confirmation for an existing target', async () => {
    const server = serverFor({ override: async (_path, init) => { if (init?.method === 'DELETE') throw new Error('DESKTOP_REQUEST_TIMEOUT'); return undefined; } });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' })); await screen.findByRole('button', { name: '核对删除结果' });
    fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' }));
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '核对删除结果' }));
    await screen.findByText(/记录仍存在/); expect(deletes(server)).toHaveLength(1);
    expect(screen.getByRole('button', { name: '确认删除' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '取消删除' })); expect(deletes(server)).toHaveLength(1);
  });
  it('clamps the final page after deletion using fresh server totals', async () => {
    let removed = false;
    const server = serverFor({ override: async (path, init) => {
      if (init?.method === 'DELETE') { removed = true; return json({ id: 'older', status: 'deleted' }); }
      if (path.includes('/analyses?')) {
        const page = Number(new URLSearchParams(path.split('?')[1]).get('page'));
        return json({ page, pages: removed ? 1 : 2, total: removed ? 25 : 26, page_size: 25,
          items: page === 2 ? removed ? [] : [historical] : [{ ...historical, id: 'first', name: '第一页历史' }] });
      }
      return undefined;
    } });
    renderCompany(server); await screen.findByRole('button', { name: /合成员/ });
    fireEvent.click(screen.getByRole('button', { name: '报告' })); fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
    await screen.findByText('第一页历史'); fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    fireEvent.click(await screen.findByRole('button', { name: '删除合成历史体检' })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByText('第 1 / 1 页 · 共 25 次体检'); expect(screen.getByText('第一页历史')).toBeInTheDocument();
  });
  it('never offers current or other-company DELETE or unbound file reuse, even in an invalid list response', async () => {
    const server = serverFor({ override: async path => path.includes('/analyses?') ? json({ page: 1, pages: 1, total: 4, page_size: 25, items: [
      { ...historical, id: 'current', name: '当前材料' }, { ...historical, id: 'other', name: '别家历史', company_id: 'company-two' },
      { ...historical, id: 'unbound', name: '未归属', company_id: null, relation: 'unbound' },
      { ...historical, id: 'busy', name: '正在运行', status: 'extracting' }] }) : undefined });
    renderCompany(server); await screen.findByRole('button', { name: /合成员/ });
    fireEvent.click(screen.getByRole('button', { name: '报告' })); fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
    await screen.findByRole('button', { name: '打开当前材料' });
    for (const name of ['当前材料', '别家历史', '正在运行']) expect(screen.queryByRole('button', { name: `删除${name}` })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复用未归属材料' })).not.toBeInTheDocument(); expect(deletes(server)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '删除未归属' })); expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
  it('defaults blank, empty/cancel writes nothing, and preserves selection through settings', async () => {
    const server = serverFor({ analysisId: null }); renderCompany(server); await openCopy();
    expect(screen.getByLabelText('选择synthetic-a.docx')).not.toBeChecked();
    expect(screen.getByRole('button', { name: '确认复用所选材料' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '取消复用' })); expect(writes(server)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '复用合成历史体检材料' }));
    fireEvent.click(await screen.findByLabelText('选择synthetic-a.docx'));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(await screen.findByLabelText('选择synthetic-a.docx')).toBeChecked(); expect(writes(server)).toHaveLength(0);
  });
  it('uses existing corpus, displays all ordered outcomes and only explicitly navigates to current materials', async () => {
    const server = serverFor({ override: async path => path.endsWith('/import-historical') ? json({ analysis_id: 'current', source_analysis_id: 'older',
      results: [{ source_file_id: 'a', file_id: 'new-a', status: 'imported', error_code: null },
        { source_file_id: 'b', file_id: 'new-b', status: 'duplicate', error_code: null },
        { source_file_id: 'c', file_id: null, status: 'error', error_code: 'HISTORICAL_FILE_MISSING' }], requires_explicit_start: true,
      provider_quota_notice: '材料复用不会自动分析；手动开始重新分析可能消耗模型服务额度。' }) : undefined });
    renderCompany(server); await openCopy();
    for (const file of files) fireEvent.click(screen.getByLabelText(`选择${file.filename}`));
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    const results = await screen.findByRole('table', { name: '材料复用结果' });
    expect(within(results).getAllByRole('row').slice(1).map(row => row.textContent)).toEqual([
      expect.stringContaining('已复制'), expect.stringContaining('已存在'), expect.stringContaining('HISTORICAL_FILE_MISSING')]);
    expect(screen.getByLabelText('选择synthetic-c.docx')).toBeChecked();
    expect(writes(server)).toHaveLength(1);
    expect(JSON.parse(String(writes(server)[0][1]?.body))).toEqual({ source_analysis_id: 'older', file_ids: ['a', 'b', 'c'] });
    expect(screen.getByText(/不会自动分析/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '前往当前材料' }));
    await screen.findByText('当前体检材料'); expect(screen.queryByText('历史体检材料')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加材料' })).toBeEnabled();
  });
  it('unknown copy requires GET reconciliation and an explicit retry, never guesses source success', async () => {
    const server = serverFor({ override: async path => { if (path.endsWith('/import-historical')) throw new Error('network'); return undefined; } });
    renderCompany(server); await openCopy(); fireEvent.click(screen.getByLabelText('选择synthetic-a.docx'));
    fireEvent.click(screen.getByRole('button', { name: '确认复用所选材料' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对复用结果' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '确认复用所选材料' })).toBeEnabled());
    expect(writes(server)).toHaveLength(1); expect(screen.queryByRole('table', { name: '材料复用结果' })).not.toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/current/workspace')).toBe(true);
  });
  it('deletion identifies the historical target, cancel restores focus and confirm sends one DELETE', async () => {
    let removed = false;
    const server = serverFor({ override: async (path, init) => {
      if (init?.method === 'DELETE') { removed = true; return json({ id: 'older', status: 'deleted' }); }
      if (removed && path.includes('/analyses?')) return json({ items: [], page: 1, pages: 0, total: 0, page_size: 25 });
      return undefined;
    } });
    renderCompany(server); await openHistory(); const trigger = screen.getByRole('button', { name: '删除合成历史体检' });
    fireEvent.click(trigger); const dialog = screen.getByRole('dialog', { name: '确认删除本次体检' });
    expect(dialog).toHaveTextContent('合成历史体检'); expect(dialog).toHaveTextContent('2026-09-01');
    expect(dialog).toHaveTextContent('员工档案保留'); expect(screen.getByRole('button', { name: '取消删除' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: '取消删除' })); expect(trigger).toHaveFocus(); expect(deletes(server)).toHaveLength(0);
    fireEvent.click(trigger); fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByText('没有历史体检记录。'); expect(deletes(server)).toHaveLength(1); expect(deletes(server)[0][0]).toBe('/api/analyses/older');
  });
  it('unknown DELETE reconciles 404 with GET and late success cannot leave Settings', async () => {
    let attempted = false;
    const server = serverFor({ override: async (path, init) => {
      if (init?.method === 'DELETE') { attempted = true; throw new Error('network'); }
      if (attempted && path === '/api/analyses/older/workspace') return json({}, 404);
      if (attempted && path.includes('/analyses?')) return json({ items: [], page: 1, pages: 0, total: 0, page_size: 25 });
      return undefined;
    } });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对删除结果' }));
    await screen.findByText('没有历史体检记录。'); expect(deletes(server)).toHaveLength(1);
  });
  it('late deletion cannot navigate away from Settings', async () => {
    let finish!: (response: Response) => void;
    const server = serverFor({ override: async (_path, init) => init?.method === 'DELETE' ? new Promise<Response>(resolve => { finish = resolve; }) : undefined });
    renderCompany(server); await openHistory(); fireEvent.click(screen.getByRole('button', { name: '删除合成历史体检' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' })); await waitFor(() => expect(deletes(server)).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    await act(async () => finish(json({ id: 'older', status: 'deleted' })));
    expect(screen.getByRole('heading', { name: '连接智谱 GLM' })).toBeInTheDocument();
  });
});
