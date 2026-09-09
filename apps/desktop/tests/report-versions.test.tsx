import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { companyServer, json, renderCompany, syntheticConfiguration } from './company-fixture';

const scope = { company_id: 'company-one', analysis_id: 'current' };
const context = { input_revision: 'a'.repeat(64), result_revision: null, review_revision: 'b'.repeat(64), context_signature: 'c'.repeat(64) };
const key = 'qian-report-request-v1:company-one/current';
const payload = { analysis_id: 'current', company_name: '冻结合成企业', generated_at: '2026-09-09T00:00:00Z', status: 'completed', is_demo: false,
  report_status: 'draft', limitations: ['无证据不代表安全'], summary: { employee_count: 0, high_count: 0, medium_count: 0, low_count: 0,
    insufficient_data_count: 0, coverage_rate: 0, affected_employee_count: 0, requires_human_review_count: 0, deadline_30_count: 0, classification_pending: false },
  material_coverage: { overall: 0, items: [] }, employees: [], findings: [], facts: [{ id: 'synthetic-fact', fact_type: 'employment.contract.exists',
    filename: 'synthetic-contract.docx', original_value: true, effective_value: false, human_confirmed: true, revision_valid: true, basis_pending: false,
    source_valid: true, latest_revision: { reason: '已核对合成签署页', created_at: '2026-09-09' },
    sources: [{ id: 'synthetic-source', location: { paragraph: 3 }, excerpt: '完全虚构签署页摘录', provenance: 'locally_located' }] }],
  advisories: [{ id: 'synthetic-advisory', filename: 'synthetic-contract.docx', issue: '合成工资条款待核对', checks: ['核对原条款'], next_action: '人工复核',
    unverified_references: ['合成引用尚未核实'], source: { location: { paragraph: 4 }, excerpt: '合成工资条款', provenance: 'locally_located' },
    handling: { decision: 'checking', reason: '合成复核中' } }] };
function accepted(body: Record<string, unknown>, version = 1) {
  const snapshot = { ...scope, id: `snapshot-${version}`, version, created_at: payload.generated_at, content_sha256: 'd'.repeat(64), ...context, payload };
  return { request: { ...scope, ...body, outcome: 'accepted', error_code: null, version_id: snapshot.id, content_sha256: snapshot.content_sha256 }, snapshot };
}
function fixture() {
  let receipt: ReturnType<typeof accepted> | null = null;
  const savedRequests = new Map<unknown, ReturnType<typeof accepted>>();
  const versions = new Map<string, ReturnType<typeof accepted>['snapshot']>();
  function save(body: Record<string, unknown>) {
    receipt = savedRequests.get(body.request_id) ?? accepted(body, versions.size + 1);
    savedRequests.set(body.request_id, receipt); versions.set(receipt.snapshot.id, receipt.snapshot); return receipt;
  }
  let submit: (body: Record<string, unknown>) => Promise<Response> = async body => json(save(body), 201);
  let reconcile: () => Promise<Response> = async () => json(receipt ?? { request: null, snapshot: null });
  let stale = false;
  let available = true;
  const server = companyServer({ override: async (path, init) => {
    if (!path.includes('/report-versions')) return undefined;
    if (path.includes('/requests/')) return reconcile();
    if (init?.method === 'POST') return submit(JSON.parse(String(init.body)));
    if (/\/report-versions\/snapshot-/.test(path)) return json({ snapshot: versions.get(path.split('/').pop()!), current_context_available: available, current_context: available ? context : null, stale });
    return json({ items: [...versions.values()].reverse(), total: versions.size, page: 1, page_size: 20, pages: 1,
      current_context_available: available, current_context: available ? context : null, warning: null });
  } });
  return { server, submit: (fn: typeof submit) => { submit = fn; }, reconcile: (fn: typeof reconcile) => { reconcile = fn; },
    save, stale: () => { stale = true; }, unavailable: () => { available = false; } };
}
async function reports() {
  await screen.findByRole('button', { name: /合成员/ });
  fireEvent.click(screen.getByRole('button', { name: '报告' }));
  await screen.findByRole('button', { name: '生成并保存报告版本' });
}
const posts = (f: ReturnType<typeof fixture>) => f.server.request.mock.calls.filter(([path, init]) => path.includes('/report-versions') && init?.method === 'POST');
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
describe('immutable reports through actual App', () => {
  it('announces context and detail loading and offers retry while retaining the previously saved report', async () => {
    const f = fixture(); f.save({});
    let release!: (response: Response) => void; let waiting = true; let fail = false;
    const api = async (path: string, init?: RequestInit) => {
      if (path.includes('/report-versions') && !init?.method) {
        if (fail) throw new Error('DESKTOP_CONNECTION_FAILED');
        if (waiting) return new Promise<Response>(resolve => { release = resolve; });
      }
      return f.server.request(path, init);
    };
    renderCompany(f.server, { apiFactory: () => api }); await reports();
    expect(screen.getByRole('status', { name: '报告读取状态' })).toHaveTextContent('正在读取报告版本和当前依据');
    waiting = false;
    await act(async () => release(await f.server.request('/api/company-workspaces/company-one/analyses/current/report-versions?page=1')));
    waiting = true; fireEvent.click(await screen.findByRole('button', { name: '查看版本 1' }));
    expect(screen.getByRole('status', { name: '报告读取状态' })).toHaveTextContent('正在读取已保存报告');
    waiting = false;
    await act(async () => release(await f.server.request('/api/company-workspaces/company-one/analyses/current/report-versions/snapshot-1')));
    await screen.findByText('冻结合成企业'); fail = true;
    fireEvent.click(screen.getByRole('button', { name: '刷新报告版本' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试读取报告' }));
    expect(screen.getByText('冻结合成企业')).toBeInTheDocument();
    const retry = await screen.findByRole('button', { name: '重试读取报告' });
    fail = false; fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试读取报告' })).not.toBeInTheDocument());
    expect(posts(f)).toHaveLength(0);
  });
  it('opens owned historical snapshots even when its current live-report GET fails', async () => {
    const saved = { ...accepted({}).snapshot, analysis_id: 'older', payload: { ...payload, analysis_id: 'older' } };
    const server = companyServer({ override: async path => {
      if (path.startsWith('/api/company-workspaces/company-one/analyses?')) return json({ page: 1, pages: 1, page_size: 25, total: 1,
        items: [{ id: 'older', name: '合成已保存历史', company_id: 'company-one', relation: 'historical', can_adopt: false,
          company_display_name: '虚构企业', status: 'completed', file_count: 1, employee_count: 0, created_at: '2025-01-01' }] });
      if (path === '/api/analyses/older/dashboard') return json({ summary: { analysis_id: 'older', status: 'completed', employee_count: 0,
        finding_count: 0, high_count: 0, medium_count: 0, insufficient_data_count: 0 }, findings: [] });
      if (path === '/api/analyses/older/workspace') return json({ analysis: { id: 'older', status: 'completed', name: '合成已保存历史' }, files: [] });
      if (path === '/api/analyses/older/report') return json({ detail: { code: 'REPORT_SOURCE_INVALID' } }, 500);
      if (path.includes('/analyses/older/report-versions/')) return json({ snapshot: saved, stale: true, current_context_available: false, current_context: null });
      if (path.includes('/analyses/older/report-versions')) return json({ items: [saved], total: 1, pages: 1, page: 1,
        current_context_available: false, current_context: null, warning: 'REPORT_SOURCE_INVALID' });
      return undefined;
    } });
    renderCompany(server); await screen.findByRole('button', { name: /合成员/ });
    fireEvent.click(screen.getByRole('button', { name: '报告' })); fireEvent.click(screen.getByRole('button', { name: '历史体检' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开合成已保存历史' }));
    fireEvent.click(await screen.findByRole('button', { name: '生成体检报告' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看版本 1' }));
    expect(await screen.findByText('冻结合成企业')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeDisabled();
    expect(server.request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
  });
  it('switches stored versions and keeps their content readable when current context becomes unavailable', async () => {
    const f = fixture(); renderCompany(f.server); await reports();
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' })); await screen.findByText(/已保存版本 1/);
    await waitFor(() => expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' })); await screen.findByText(/已保存版本 2/);
    fireEvent.click(await screen.findByRole('button', { name: '查看版本 1' })); await screen.findByText(/已保存版本 1/);
    f.unavailable(); fireEvent.click(screen.getByRole('button', { name: '刷新报告版本' }));
    await screen.findByText(/当前来源无法核验/);
    expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeDisabled();
    expect(screen.getByText('冻结合成企业')).toBeInTheDocument();
    expect(screen.getByText(/已保存版本 1/)).toBeInTheDocument(); expect(posts(f)).toHaveLength(2);
  });
  it('creates only on explicit action without a configured provider, prints frozen metadata and restores selected version after Settings', async () => {
    const f = fixture(); renderCompany(f.server, { configurationLoader: async () => ({ ...syntheticConfiguration, configured: false, validated: false }) });
    await reports(); expect(posts(f)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await screen.findByText('冻结合成企业');
    expect(screen.getByText(/已保存版本 1/)).toBeInTheDocument();
    expect(screen.getByText('无证据不代表安全')).toBeInTheDocument();
    expect(screen.getByText('原始值：是')).toBeInTheDocument();
    expect(screen.getByText('有效值：否')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /书面劳动合同/ })).toBeInTheDocument();
    expect(screen.getByText('完全虚构签署页摘录')).toBeInTheDocument();
    expect(screen.getByText(/未经核验引用：合成引用尚未核实/)).toBeInTheDocument();
    expect(screen.queryByText(/真实模型分析/)).not.toBeInTheDocument();
    expect(localStorage.getItem(key)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '报告' }));
    await screen.findByText('冻结合成企业'); expect(posts(f)).toHaveLength(1);
    expect(f.server.request.mock.calls.filter(([path]) => path.includes('connection-test') || path.endsWith('/reevaluate'))).toHaveLength(0);
  });
  it('persists before POST, retains original CAS across null, remount and explicit same-request retry', async () => {
    const f = fixture(); let original!: Record<string, unknown>;
    f.submit(async body => { original = body; expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ ...scope, ...body }); throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    const mounted = renderCompany(f.server); await reports();
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await screen.findByRole('button', { name: '重试原报告请求' }); const journal = localStorage.getItem(key);
    mounted.unmount(); renderCompany(f.server); await reports();
    await waitFor(() => expect(f.server.request.mock.calls.some(([path]) => path.includes('/requests/'))).toBe(true));
    expect(localStorage.getItem(key)).toBe(journal); expect(posts(f)).toHaveLength(1);
    expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeDisabled();
    f.submit(async body => { expect(body).toEqual(original); return json(f.save(body), 200); });
    fireEvent.click(screen.getByRole('button', { name: '重试原报告请求' }));
    await screen.findByText('冻结合成企业'); expect(posts(f)).toHaveLength(2); expect(localStorage.getItem(key)).toBeNull();
  });
  it.each([401, 403, 404, 409, 500])('retains unknown journal after unreceipted %s and mismatched receipt', async status => {
    const f = fixture(); let body!: Record<string, unknown>;
    f.submit(async input => { body = input; return json({ detail: { code: 'DESKTOP_ANALYSIS_BUSY' } }, status); });
    renderCompany(f.server); await reports(); fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await screen.findByRole('button', { name: '重试原报告请求' }); const journal = localStorage.getItem(key);
    f.reconcile(async () => json(accepted({ ...body, expected_context_signature: 'e'.repeat(64) })));
    fireEvent.click(screen.getByRole('button', { name: '核对报告请求' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('回执'));
    expect(localStorage.getItem(key)).toBe(journal);
    expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeDisabled();
  });
  it('resolves accepted-but-lost response by GET without a second POST', async () => {
    const f = fixture(); f.submit(async body => { f.save(body); throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    renderCompany(f.server); await reports(); fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await screen.findByRole('button', { name: '核对报告请求' });
    fireEvent.click(screen.getByRole('button', { name: '核对报告请求' }));
    await screen.findByText('冻结合成企业'); expect(posts(f)).toHaveLength(1); expect(localStorage.getItem(key)).toBeNull();
  });
  it('does not send if journal persistence fails', async () => {
    const f = fixture(); renderCompany(f.server); await reports();
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('保存请求'));
    expect(posts(f)).toHaveLength(0); spy.mockRestore();
  });
  it('keeps a malformed persisted journal fail-closed even after a successful context refresh', async () => {
    localStorage.setItem(key, '{"request_id":"damaged"}');
    const f = fixture(); renderCompany(f.server); await reports();
    await waitFor(() => expect(f.server.request.mock.calls.some(([path]) => path.includes('/report-versions?'))).toBe(true));
    expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeDisabled();
    expect(localStorage.getItem(key)).toBe('{"request_id":"damaged"}'); expect(posts(f)).toHaveLength(0);
  });
  it('allows explicit fresh generation only after a fully matching durable rejection', async () => {
    const f = fixture(); let old!: Record<string, unknown>;
    f.submit(async body => { old = body; return json({ request: { ...scope, ...body, outcome: 'rejected',
      error_code: 'REPORT_VERSION_CONFLICT', version_id: null, content_sha256: null }, snapshot: null }, 409); });
    renderCompany(f.server); await reports(); fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeEnabled());
    expect(localStorage.getItem(key)).toBeNull(); expect(posts(f)).toHaveLength(1);
    const first = old; f.submit(async body => { expect(body.request_id).not.toBe(first.request_id); return json(f.save(body), 201); });
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' })); await screen.findByText('冻结合成企业');
  });
  it('renews the actual App API, retains the original UUID/CAS after null GET, and ignores an older API receipt', async () => {
    const f = fixture(); let restarted = false; let original!: Record<string, unknown>; let finishOld!: (r: Response) => void;
    f.submit(async body => { original = body; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    const oldApi = async (path: string, init?: RequestInit) => path.includes('/report-versions/requests/')
      ? new Promise<Response>(resolve => { finishOld = resolve; }) : f.server.request(path, init);
    const newCalls: string[] = [];
    const newApi = async (path: string, init?: RequestInit) => {
      newCalls.push(path); return path === '/api/provider/connection-test' ? json({ status: 'connected' }) : f.server.request(path, init);
    };
    renderCompany(f.server, { backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:1', token: restarted ? 'new' : 'old' }),
      apiFactory: info => info.token === 'old' ? oldApi : newApi, providerConfigurator: async () => { restarted = true; return syntheticConfiguration; },
      providerValidator: async () => syntheticConfiguration });
    await reports(); fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对报告请求' })); await waitFor(() => expect(finishOld).toBeDefined());
    const first = original; const journal = localStorage.getItem(key);
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.change(await screen.findByLabelText('智谱 API Key'), { target: { value: 'synthetic-report-fixture' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '重试原报告请求' })).toBeEnabled());
    expect(localStorage.getItem(key)).toBe(journal); expect(posts(f)).toHaveLength(1);
    await act(async () => { finishOld(json(accepted(first))); });
    expect(localStorage.getItem(key)).toBe(journal);
    f.submit(async body => { expect(body).toEqual(first); return json(f.save(body), 200); });
    fireEvent.click(screen.getByRole('button', { name: '重试原报告请求' })); await screen.findByText('冻结合成企业');
    expect(posts(f)).toHaveLength(2); expect(newCalls.some(path => path.endsWith('/report-versions'))).toBe(true);
  });
  it('ignores old A-away-A reconciliation after a newer B request is pending', async () => {
    const f = fixture(); let old!: Record<string, unknown>; let finish!: (r: Response) => void;
    f.submit(async body => { old = body; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    renderCompany(f.server); await reports(); fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await screen.findByRole('button', { name: '核对报告请求' });
    f.reconcile(async () => new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: '核对报告请求' })); await waitFor(() => expect(finish).toBeDefined());
    const finishOld = finish; const first = old;
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    f.reconcile(async () => json(f.save(first)));
    fireEvent.click(screen.getByRole('button', { name: '报告' })); await screen.findByText('冻结合成企业');
    fireEvent.click(screen.getByRole('button', { name: '生成并保存报告版本' }));
    await screen.findByRole('button', { name: '核对报告请求' }); const journal = localStorage.getItem(key);
    await act(async () => { finishOld(json(accepted(first))); });
    expect(localStorage.getItem(key)).toBe(journal); expect(JSON.parse(journal!).request_id).not.toBe(first.request_id);
    expect(screen.getByRole('button', { name: '生成并保存报告版本' })).toBeDisabled();
  });
});
