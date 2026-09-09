import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { companyServer, json, renderCompany, syntheticConfiguration } from './company-fixture';
import type { TaskRun } from '../src/features/processing/useAnalysisTask';

const scope = { analysis_id: 'current', company_id: 'company-one' };
const run = (state: TaskRun['state'] = 'running', version = 1): TaskRun => ({ ...scope, id: 'run-one', parent_run_id: null,
  state, version, in_flight: state === 'running', created_at: '2026-09-09T00:00:00', completed_at: null });
const metadata = (current: ReturnType<typeof run> | null, business = current?.state ?? 'uploading') => ({ ...scope,
  business_status: business, run: current, read_only: false, history: current ? [current] : [], limit: 20, offset: 0,
  resume_preview: { reusable_file_ids: ['file'], extraction_file_ids: ['pending', 'outdated'] },
  usage_notice: 'admitted_requests_may_consume_quota_unknown_is_not_zero' });
const files = ['file', 'pending', 'outdated'].map((id, i) => ({ id, filename: `synthetic-${id}.docx`,
  status: i ? 'interrupted' : 'processed', progress: i ? 30 : 100, detected_kind: 'contract', classified_kind: 'contract',
  error_code: i ? 'PROCESSING_INTERRUPTED' : null, size_bytes: 100, fact_count: i ? 0 : 4 }));
function fixture(initial: ReturnType<typeof run> | null = null) {
  let current = initial;
  let failRead = false;
  let failFiles = false;
  let command: ((path: string, init: RequestInit) => Promise<Response>) | null = null;
  let receipt: ((path: string) => Promise<Response>) | null = null;
  const server = companyServer({ status: initial?.state ?? 'uploading', override: async (path, init) => {
    if (path.includes('/task/requests/')) return receipt ? receipt(path) : json({ ...scope, outcome: 'unknown', request: null, run: null });
    if (path.includes('/task?')) { if (failRead) throw new Error('DESKTOP_CONNECTION_FAILED'); return json(metadata(current)); }
    if (path.includes('/task/') && init?.method === 'POST') return command ? command(path, init) : json({}, 500);
    if (path.endsWith('/workspace')) { if (failFiles) throw new Error('DESKTOP_CONNECTION_FAILED'); return json({ analysis: { id: 'current', name: '合成材料档案', company_display_name: '完全虚构企业', status: current?.state ?? 'uploading' }, files }); }
    if (path.endsWith('/processing')) return json({ ...scope, status: current?.state ?? 'uploading', progress: 35,
      current_stage: current?.state ?? 'uploading', files, task_run: current });
    return undefined;
  } });
  return { server, setRun: (next: typeof current) => { current = next; }, failRead: (value: boolean) => { failRead = value; },
    failFiles: (value: boolean) => { failFiles = value; },
    onCommand: (fn: NonNullable<typeof command>) => { command = fn; }, onReceipt: (fn: NonNullable<typeof receipt>) => { receipt = fn; } };
}
const posts = (f: ReturnType<typeof fixture>) => f.server.request.mock.calls.filter(([, init]) => init?.method === 'POST');
const bodyOf = (init: RequestInit) => JSON.parse(String(init.body));
function accepted(init: RequestInit, operation: string, current: ReturnType<typeof run>) {
  const body = bodyOf(init);
  return json({ ...scope, outcome: 'accepted', run: current, request: { ...scope, id: body.request_id,
    run_id: current.id, operation, expected_run_id: body.expected_run_id, expected_version: body.expected_version,
    outcome: 'accepted', error_code: null, created_at: current.created_at } }, 202);
}
async function materials() {
  await screen.findByRole('button', { name: /合成员/ });
  fireEvent.click(screen.getByRole('button', { name: '材料' }));
  await screen.findByRole('heading', { name: '合成材料档案' });
}
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
describe('task lifecycle through actual App', () => {
  it('does not replace a same-scope older journal appearing before a fresh submission', async () => {
    const f = fixture(); renderCompany(f.server); await materials();
    const older = JSON.stringify({ analysisId: 'current', operation: 'start', body: { request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      company_id: 'company-one', expected_run_id: null, expected_version: null } });
    localStorage.setItem('qian-task-request-v1:company-one/current', older);
    fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    expect(await screen.findByText(/无法保存.*请求.*未发送/)).toBeInTheDocument();
    expect(localStorage.getItem('qian-task-request-v1:company-one/current')).toBe(older); expect(posts(f)).toHaveLength(0);
  });
  it('explains journal write failure without a POST or overwriting an older journal', async () => {
    const f = fixture(); const older = 'synthetic older unknown journal';
    localStorage.setItem('qian-task-request-v1:other/analysis', older);
    renderCompany(f.server); await materials();
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Quota', 'QuotaExceededError'); });
    try {
      fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
      expect(await screen.findByText(/无法保存.*请求.*未发送/)).toBeInTheDocument();
      expect(posts(f)).toHaveLength(0);
      expect(localStorage.getItem('qian-task-request-v1:other/analysis')).toBe(older);
      expect(localStorage.getItem('qian-task-request-v1:company-one/current')).toBeNull();
    } finally { write.mockRestore(); }
  });
  it.each([[401, 'DESKTOP_TOKEN_REQUIRED'], [403, 'DESKTOP_TOKEN_INVALID'], [404, 'TASK_SCOPE_INVALID'],
    [409, 'WORKSPACE_HISTORICAL_READ_ONLY'], [409, 'DESKTOP_ANALYSIS_BUSY'], [409, 'ANALYSIS_HAS_NO_FILES'],
    [409, 'TASK_OWNER_UNAVAILABLE'], [422, 'TASK_OPERATION_INVALID']] as const)(
    'retains the unknown original request after retry %s %s until its matching receipt arrives', async (status, code) => {
      const f = fixture(); let submitted!: RequestInit;
      f.onCommand(async (_path, init) => { submitted = init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
      renderCompany(f.server); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
      await screen.findByRole('button', { name: '重试原请求' });
      const journal = localStorage.getItem('qian-task-request-v1:company-one/current');
      f.onCommand(async () => json({ detail: { code } }, status));
      fireEvent.click(screen.getByRole('button', { name: '重试原请求' }));
      fireEvent.click(await screen.findByRole('button', { name: '确认重试原请求' }));
      await waitFor(() => expect(posts(f)).toHaveLength(2));
      await waitFor(() => expect(screen.queryByRole('button', { name: '确认重试原请求' })).not.toBeInTheDocument());
      expect(localStorage.getItem('qian-task-request-v1:company-one/current')).toBe(journal);
      expect(posts(f)[1][1]!.body).toBe(submitted.body);
      expect(screen.getByRole('button', { name: '开始分析' })).toBeDisabled();
      f.onReceipt(async () => { f.setRun(run()); return accepted(submitted, 'start', run()); });
      fireEvent.click(screen.getByRole('button', { name: '核对处理状态' }));
      await waitFor(() => expect(localStorage.getItem('qian-task-request-v1:company-one/current')).toBeNull());
      expect(posts(f)).toHaveLength(2);
    });
  it('retains reconciliation ownership across A-away-A before allowing a newer unknown request', async () => {
    const f = fixture(); const other = { ...f.server.company, id: 'company-two', display_name: '另一虚构企业' };
    let finish!: (r: Response) => void; let submitted!: RequestInit; let receiptCalls = 0;
    f.onCommand(async (_path, init) => { submitted = init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    f.onReceipt(async () => { receiptCalls++; return new Promise<Response>(resolve => { finish = resolve; }); });
    const api = async (path: string, init?: RequestInit) => {
      if (path === '/api/company-workspaces') return json([f.server.company, other]);
      if (path.startsWith('/api/company-workspaces/company-two/current?')) return json({ ...f.server.projection(), company: other, current_analysis: null, employees: [], total: 0, enrolled_employee_count: 0 });
      return f.server.request(path, init);
    };
    renderCompany(f.server, { apiFactory: () => api }); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对处理状态' }));
    await waitFor(() => expect(finish).toBeTypeOf('function')); const first = submitted;
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-two' } }); await screen.findByText('已建档员工 0 人');
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-one' } });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    expect(await screen.findByRole('button', { name: '重试原请求' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '核对处理状态' })).toBeDisabled(); expect(receiptCalls).toBe(1);
    f.setRun(run()); await act(async () => finish(accepted(first, 'start', run())));
    fireEvent.click(await screen.findByRole('button', { name: '取消分析' }));
    await screen.findByRole('button', { name: '重试原请求' });
    const second = JSON.parse(localStorage.getItem('qian-task-request-v1:company-one/current')!);
    expect(second.body.request_id).not.toBe(bodyOf(first).request_id); expect(second.operation).toBe('cancel');
    expect(posts(f)).toHaveLength(2);
  });
  it('keeps one local submit in flight when leaving A and returning to its pending journal', async () => {
    const f = fixture(); const other = { ...f.server.company, id: 'company-two', display_name: '另一虚构企业' };
    let finish!: (r: Response) => void;
    f.onCommand(async () => new Promise<Response>(resolve => { finish = resolve; }));
    const api = async (path: string, init?: RequestInit) => {
      if (path === '/api/company-workspaces') return json([f.server.company, other]);
      if (path.startsWith('/api/company-workspaces/company-two/current?')) return json({ ...f.server.projection(), company: other, current_analysis: null, employees: [], total: 0, enrolled_employee_count: 0 });
      return f.server.request(path, init);
    };
    renderCompany(f.server, { apiFactory: () => api }); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-two' } }); await screen.findByText('已建档员工 0 人');
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-one' } });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    expect(await screen.findByRole('button', { name: '重试原请求' })).toBeDisabled();
    await act(async () => finish(json({}, 500)));
    await waitFor(() => expect(screen.getByRole('button', { name: '重试原请求' })).toBeEnabled()); expect(posts(f)).toHaveLength(1);
  });
  it.each(['receipt', 'error'] as const)('ignores an old backend R1 reconciliation %s while R2 owns the journal and read lock', async outcome => {
    const f = fixture(); let restarted = false; let first!: RequestInit;
    let finishOld!: (r: Response) => void; let finishNew!: (r: Response) => void;
    f.onCommand(async (_path, init) => { first ??= init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    const oldApi = async (path: string, init?: RequestInit) => path.includes('/task/requests/')
      ? new Promise<Response>(resolve => { finishOld = resolve; }) : f.server.request(path, init);
    const newApi = async (path: string, init?: RequestInit) => path === '/api/provider/connection-test'
      ? json({ status: 'connected' }) : f.server.request(path, init);
    renderCompany(f.server, { backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:1', token: restarted ? 'new' : 'old' }),
      apiFactory: info => info.token === 'old' ? oldApi : newApi,
      providerConfigurator: async () => { restarted = true; f.setRun(run()); return syntheticConfiguration; },
      providerValidator: async () => syntheticConfiguration });
    await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对处理状态' }));
    await waitFor(() => expect(finishOld).toBeTypeOf('function'));
    f.onReceipt(async () => accepted(first, 'start', run()));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.change(await screen.findByLabelText('智谱 API Key'), { target: { value: 'synthetic-fixture-key' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '取消分析' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '取消分析' }));
    await screen.findByRole('button', { name: '重试原请求' });
    const secondJournal = localStorage.getItem('qian-task-request-v1:company-one/current');
    expect(JSON.parse(secondJournal!).body.request_id).not.toBe(bodyOf(first).request_id);
    f.onReceipt(async () => new Promise<Response>(resolve => { finishNew = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: '核对处理状态' }));
    await waitFor(() => expect(finishNew).toBeTypeOf('function'));
    await act(async () => finishOld(outcome === 'receipt' ? accepted(first, 'start', run()) : json({ detail: { code: 'TASK_SCOPE_INVALID' } }, 404)));
    expect(localStorage.getItem('qian-task-request-v1:company-one/current')).toBe(secondJournal);
    expect(screen.getByRole('button', { name: '重试原请求' })).toBeDisabled();
    expect(screen.queryByText(/TASK_SCOPE_INVALID/)).not.toBeInTheDocument(); expect(posts(f)).toHaveLength(2);
    await act(async () => finishNew(json({ ...scope, outcome: 'unknown', request: null, run: null })));
    expect(screen.getByRole('button', { name: '重试原请求' })).toBeEnabled();
    expect(localStorage.getItem('qian-task-request-v1:company-one/current')).toBe(secondJournal);
  });
  it('requires configuration for original start retry, uses the refreshed backend, and never resumes automatically after Settings', async () => {
    const f = fixture(); f.onCommand(async () => { throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    const first = renderCompany(f.server); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await screen.findByRole('button', { name: '核对处理状态' }); const originalBody = posts(f)[0][1]!.body; first.unmount();
    let generation = 0;
    const refreshedCalls: string[] = [];
    renderCompany(f.server, { configurationLoader: async () => ({ ...syntheticConfiguration, validated: false }),
      apiFactory: () => { const instance = ++generation; return async (path, init) => {
        if (instance > 1) refreshedCalls.push(path);
        if (path === '/api/provider/connection-test') return json({ status: 'connected' });
        return f.server.request(path, init);
      }; }, providerConfigurator: async () => syntheticConfiguration, providerValidator: async () => syntheticConfiguration });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试原请求' })); fireEvent.click(await screen.findByRole('button', { name: '确认重试原请求' }));
    await screen.findByRole('heading', { name: '连接智谱 GLM' }); expect(posts(f)).toHaveLength(1);
    fireEvent.change(screen.getByLabelText('智谱 API Key'), { target: { value: 'synthetic-retry-key' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
    await screen.findByRole('button', { name: '重试原请求' }); expect(posts(f)).toHaveLength(1);
    f.onCommand(async (_path, init) => { f.setRun(run()); return accepted(init, 'start', run()); });
    fireEvent.click(screen.getByRole('button', { name: '重试原请求' })); fireEvent.click(await screen.findByRole('button', { name: '确认重试原请求' }));
    await waitFor(() => expect(posts(f)).toHaveLength(2)); expect(posts(f)[1][1]!.body).toBe(originalBody);
    expect(refreshedCalls).toContain('/api/analyses/current/task/start');
  });
  it.each(['accepted', 'unknown', 'conflict', 'mismatch'] as const)('explicitly retries exactly the original request after session restart (%s)', async outcome => {
    const f = fixture(); let submitted!: RequestInit;
    f.onCommand(async (_path, init) => { submitted = init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    const first = renderCompany(f.server); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await screen.findByRole('button', { name: '核对处理状态' });
    for (let i = 0; i < 2; i++) { fireEvent.click(screen.getByRole('button', { name: '核对处理状态' }));
      await waitFor(() => expect(screen.getByRole('button', { name: '核对处理状态' })).toBeEnabled()); }
    first.unmount(); sessionStorage.clear(); renderCompany(f.server);
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    const originalBody = submitted.body;
    f.onCommand(async (_path, init) => {
      if (outcome === 'unknown') throw new Error('DESKTOP_REQUEST_TIMEOUT');
      if (outcome === 'conflict') return json({ detail: { code: 'TASK_VERSION_CONFLICT' } }, 409);
      if (outcome === 'mismatch') { const envelope = await accepted(init, 'start', run()).json(); return json({ ...envelope, request: { ...envelope.request, expected_version: 999 } }); }
      f.setRun(run()); return accepted(init, 'start', run());
    });
    fireEvent.click(await screen.findByRole('button', { name: '重试原请求' }));
    await screen.findByText(/此前未被接受，本次重试可能开始处理并消耗模型额度/);
    expect(posts(f)).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '确认重试原请求' }));
    await waitFor(() => expect(posts(f)).toHaveLength(2));
    expect(posts(f)[1][1]!.body).toBe(originalBody);
    if (outcome === 'unknown' || outcome === 'mismatch') await screen.findByRole('button', { name: '核对处理状态' });
    else if (outcome === 'conflict') { await screen.findByText(/TASK_VERSION_CONFLICT/); expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument(); }
    else await waitFor(() => expect(screen.queryByRole('button', { name: '核对处理状态' })).not.toBeInTheDocument());
  });
  it('retries a lost accepted cancel response without model configuration and preserves its original CAS', async () => {
    const f = fixture(run()); let call = 0;
    f.onCommand(async (_path, init) => { call++; f.setRun(run('cancelled', 3));
      if (call === 1) throw new Error('DESKTOP_REQUEST_TIMEOUT'); return accepted(init, 'cancel', run('cancelled', 3)); });
    renderCompany(f.server, { configurationLoader: async () => ({ ...syntheticConfiguration, validated: false }) });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' })); fireEvent.click(await screen.findByRole('button', { name: '取消分析' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试原请求' })); fireEvent.click(await screen.findByRole('button', { name: '确认重试原请求' }));
    await screen.findAllByText('任务已取消'); expect(posts(f)).toHaveLength(2);
    expect(posts(f)[1][1]!.body).toBe(posts(f)[0][1]!.body);
  });
  it.each(['task', 'files'] as const)('refreshes retry resume files and keeps original CAS; %s read failure never opens stale confirmation', async failedRead => {
    const f = fixture(run('interrupted', 4));
    f.onCommand(async () => { throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    renderCompany(f.server); fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '恢复分析' })); fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await screen.findByRole('button', { name: '核对处理状态' }); (failedRead === 'task' ? f.failRead : f.failFiles)(true);
    fireEvent.click(await screen.findByRole('button', { name: '重试原请求' })); await screen.findByText(/DESKTOP_CONNECTION_FAILED/);
    expect(screen.queryByRole('button', { name: '确认重试原请求' })).not.toBeInTheDocument(); expect(posts(f)).toHaveLength(1);
    f.failRead(false); f.failFiles(false); f.setRun(run('interrupted', 8));
    fireEvent.click(screen.getByRole('button', { name: '重试原请求' }));
    await screen.findByText(/需重新提取 2 份/);
    fireEvent.click(await screen.findByRole('button', { name: '确认重试原请求' })); await waitFor(() => expect(posts(f)).toHaveLength(2));
    expect(posts(f)[1][1]!.body).toBe(posts(f)[0][1]!.body); expect(bodyOf(posts(f)[1][1]!)).toMatchObject({ expected_version: 4 });
  });
  it('reads a fresh owned resume preview after supplementation before enabling confirmation', async () => {
    const f = fixture(run('interrupted', 4)); let supplemented = false;
    const api = async (path: string, init?: RequestInit) => {
      if (supplemented && path.includes('/task?')) return json({ ...metadata(run('interrupted', 5)),
        resume_preview: { reusable_file_ids: [], extraction_file_ids: ['file', 'pending', 'outdated'] } });
      return f.server.request(path, init);
    };
    renderCompany(f.server, { apiFactory: () => api }); await materials();
    await waitFor(() => expect(screen.getByRole('button', { name: '恢复分析' })).toBeEnabled());
    supplemented = true; fireEvent.click(screen.getByRole('button', { name: '恢复分析' }));
    await screen.findByText(/需重新提取 3 份/); expect(screen.getByText(/可复用 0 份/)).toBeInTheDocument();
    expect(posts(f)).toHaveLength(0);
    f.onCommand(async (_path, init) => accepted(init, 'resume', run()));
    fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await waitFor(() => expect(posts(f)).toHaveLength(1));
    expect(bodyOf(posts(f)[0][1]!)).toMatchObject({ expected_version: 5 });
  });
  it('does not let an unmounted App response erase a still-unresolved request from durable storage', async () => {
    const f = fixture(); let submitted!: RequestInit; let finish!: (r: Response) => void;
    f.onCommand(async (_path, init) => { submitted = init; return new Promise<Response>(resolve => { finish = resolve; }); });
    const first = renderCompany(f.server); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await waitFor(() => expect(finish).toBeTypeOf('function')); first.unmount();
    const second = renderCompany(f.server); await screen.findByRole('button', { name: '核对处理状态' });
    await act(async () => finish(accepted(submitted, 'start', run()))); second.unmount();
    renderCompany(f.server); await screen.findByRole('button', { name: '核对处理状态' });
    expect(posts(f)).toHaveLength(1);
  });
  it.each(['pending', 'rejected'] as const)('uses receipt outcome rather than HTTP 202 for cancel %s', async outcome => {
    const f = fixture(run()); let submitted!: RequestInit;
    f.onCommand(async (_path, init) => {
      submitted = init; const envelope = await accepted(init, 'cancel', run()).json();
      return json({ ...envelope, outcome, request: { ...envelope.request, outcome, error_code: outcome === 'rejected' ? 'TASK_SUBMISSION_FAILED' : null } }, 202);
    });
    renderCompany(f.server); fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消分析' }));
    if (outcome === 'pending') {
      await screen.findByRole('button', { name: '核对处理状态' });
      f.onReceipt(async () => { throw new Error('DESKTOP_CONNECTION_FAILED'); });
      fireEvent.click(screen.getByRole('button', { name: '核对处理状态' })); await screen.findByText(/DESKTOP_CONNECTION_FAILED/);
      expect(screen.getByRole('button', { name: '取消分析' })).toBeDisabled();
      f.onReceipt(async () => { f.setRun(run('cancelled', 3)); return accepted(submitted, 'cancel', run('cancelled', 3)); });
      fireEvent.click(screen.getByRole('button', { name: '核对处理状态' })); await screen.findAllByText('任务已取消');
    } else {
      await screen.findByText(/TASK_SUBMISSION_FAILED/); expect(screen.queryByRole('button', { name: '核对处理状态' })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '取消分析' })).toBeDisabled();
      fireEvent.click(screen.getByRole('button', { name: '刷新任务状态' })); await waitFor(() => expect(screen.getByRole('button', { name: '取消分析' })).toBeEnabled());
    }
    expect(posts(f)).toHaveLength(1);
  });
  it('reconciles an old accepted receipt without replacing the latest resumed run', async () => {
    const f = fixture(); let submitted!: RequestInit;
    f.onCommand(async (_path, init) => { submitted = init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    renderCompany(f.server); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await screen.findByRole('button', { name: '核对处理状态' });
    f.setRun({ ...run('interrupted', 7), id: 'new-run', parent_run_id: 'run-one' });
    f.onReceipt(async () => accepted(submitted, 'start', run('completed', 3)));
    fireEvent.click(screen.getByRole('button', { name: '核对处理状态' })); await screen.findByText('任务已中断');
    fireEvent.click(screen.getByRole('button', { name: '恢复分析' }));
    f.onCommand(async (_path, init) => accepted(init, 'resume', { ...run('running', 0), id: 'next-run', parent_run_id: 'new-run' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await waitFor(() => expect(posts(f)).toHaveLength(2));
    expect(bodyOf(posts(f)[1][1]!)).toMatchObject({ expected_run_id: 'new-run', expected_version: 7 });
  });
  it('keeps idle matching_review uncancellable and performs no extraction at startup or on GET', async () => {
    const f = fixture(run('completed', 3));
    const api = async (path: string, init?: RequestInit) => path.includes('/task?') ? json(metadata(run('completed', 3), 'matching_review')) : f.server.request(path, init);
    renderCompany(f.server, { apiFactory: () => api }); fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    expect(screen.queryByRole('button', { name: '取消分析' })).not.toBeInTheDocument();
    expect(posts(f)).toHaveLength(0);
  });
  it('does not let a delayed earlier metadata read resurrect a cancelled task', async () => {
    const f = fixture(run()); let hold = false; let finish!: (r: Response) => void;
    const api = async (path: string, init?: RequestInit) => {
      if (hold && path.includes('/task?')) { hold = false; return new Promise<Response>(resolve => { finish = resolve; }); }
      return f.server.request(path, init);
    };
    f.onCommand(async (_path, init) => { const next = run('cancelled', 3); f.setRun(next); return accepted(init, 'cancel', next); });
    renderCompany(f.server, { apiFactory: () => api });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    hold = true; fireEvent.click(await screen.findByRole('button', { name: '刷新任务状态' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: '取消分析' })); await screen.findAllByText('任务已取消');
    await act(async () => finish(json(metadata(run()))));
    expect(screen.queryByRole('button', { name: '取消分析' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '恢复分析' })).toBeInTheDocument();
  });
  it('refreshes interrupted state on Settings backend replacement and ignores old host GET completion', async () => {
    const f = fixture(run()); let restarted = false; let hold = false; let finish!: (r: Response) => void;
    let finishProgress!: (r: Response) => void;
    const oldApi = async (path: string, init?: RequestInit) => {
      if (hold && path.includes('/task?')) return new Promise<Response>(resolve => { finish = resolve; });
      if (path.endsWith('/processing')) return new Promise<Response>(resolve => { finishProgress = resolve; });
      return f.server.request(path, init);
    };
    const newApi = vi.fn(async (path: string, init?: RequestInit) => path === '/api/provider/connection-test' ? json({ status: 'connected' }) : f.server.request(path, init));
    renderCompany(f.server, { backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:1', token: restarted ? 'new' : 'old' }),
      apiFactory: info => info.token === 'old' ? oldApi : newApi,
      providerConfigurator: async () => { restarted = true; f.setRun(run('interrupted', 4)); return syntheticConfiguration; },
      providerValidator: async () => syntheticConfiguration });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' })); hold = true;
    fireEvent.click(await screen.findByRole('button', { name: '刷新任务状态' })); await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.change(await screen.findByLabelText('智谱 API Key'), { target: { value: 'synthetic-fixture-key' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
    await screen.findAllByText('任务已中断'); await act(async () => finish(json(metadata(run()))));
    await act(async () => finishProgress(json({ ...scope, status: 'extracting', current_stage: 'extracting', progress: 95, files, task_run: run() })));
    expect(screen.queryByRole('button', { name: '取消分析' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '恢复分析' })).toBeEnabled();
    await waitFor(() => expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '35'));
    expect(newApi.mock.calls.some(([path]) => path.includes('/task?company_id=company-one'))).toBe(true);
    expect(posts(f)).toHaveLength(0);
  });
  it('retains unknown resume across App unmount and rejects a mismatched rejected receipt', async () => {
    const f = fixture(run('interrupted', 4)); let submitted!: RequestInit;
    f.onCommand(async (_path, init) => { submitted = init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    const first = renderCompany(f.server); fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '恢复分析' })); fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await screen.findByRole('button', { name: '核对处理状态' }); first.unmount(); sessionStorage.clear();
    f.onReceipt(async () => { const value = await accepted(submitted, 'start', run('interrupted', 4)).json();
      return json({ ...value, outcome: 'rejected', request: { ...value.request, outcome: 'rejected', error_code: 'TASK_SUBMISSION_FAILED' } }); });
    renderCompany(f.server); fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    await screen.findByText(/TASK_RESPONSE_INVALID/); expect(screen.getByRole('button', { name: '恢复分析' })).toBeDisabled();
    expect(posts(f)).toHaveLength(1);
  });
  it.each([false, true])('keeps A pending commands out of B and ignores late callbacks (retry=%s)', async retry => {
    const f = fixture(); const other = { ...f.server.company, id: 'company-two', display_name: '另一虚构企业' };
    let finish!: (r: Response) => void; let submitted!: RequestInit;
    let calls = 0;
    f.onCommand(async (_path, init) => { submitted = init; calls++; if (retry && calls === 1) throw new Error('DESKTOP_REQUEST_TIMEOUT'); return new Promise<Response>(resolve => { finish = resolve; }); });
    const api = async (path: string, init?: RequestInit) => {
      if (path === '/api/company-workspaces') return json([f.server.company, other]);
      if (path.startsWith('/api/company-workspaces/company-two/current?')) return json({ ...f.server.projection(), company: other, current_analysis: null, employees: [], total: 0, enrolled_employee_count: 0 });
      return f.server.request(path, init);
    };
    renderCompany(f.server, { apiFactory: () => api }); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    if (retry) { fireEvent.click(await screen.findByRole('button', { name: '重试原请求' })); fireEvent.click(await screen.findByRole('button', { name: '确认重试原请求' })); }
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-two' } }); await screen.findByText('已建档员工 0 人');
    await act(async () => finish(accepted(submitted, 'start', run())));
    expect(screen.getByText('已建档员工 0 人')).toBeInTheDocument();
    expect(screen.queryByLabelText('任务控制')).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: '查看处理进度' })).not.toBeInTheDocument();
    expect(posts(f)).toHaveLength(retry ? 2 : 1);
  });
  it('keeps a caller UUID after two unknown GETs and Settings, then resolves only its receipt with one POST', async () => {
    const f = fixture(); let submitted!: RequestInit; let reads = 0;
    f.onCommand(async (_path, init) => { submitted = init; throw new Error('DESKTOP_REQUEST_TIMEOUT'); });
    f.onReceipt(async () => { reads++; return reads <= 2 ? json({ ...scope, outcome: 'unknown', request: null, run: null }) : accepted(submitted, 'start', run()); });
    renderCompany(f.server); await materials(); fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await screen.findByRole('button', { name: '核对处理状态' });
    expect(bodyOf(submitted)).toEqual({ request_id: expect.any(String), company_id: 'company-one', expected_run_id: null, expected_version: null });
    for (let i = 0; i < 2; i++) { fireEvent.click(screen.getByRole('button', { name: '核对处理状态' })); await waitFor(() => expect(reads).toBe(i + 1)); }
    expect(screen.getByRole('button', { name: '开始分析' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '设置' })); fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对处理状态' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对处理状态' })).not.toBeInTheDocument());
    expect(posts(f)).toHaveLength(1);
    expect(f.server.request.mock.calls.filter(([path]) => path.includes('/task/requests/')).every(([path]) => path.includes(bodyOf(submitted).request_id))).toBe(true);
  });
  it('boots interrupted without a model gate and only resumes after a named file preview and confirmation', async () => {
    const f = fixture(run('interrupted', 4));
    f.onCommand(async (_path, init) => { f.setRun(run('running', 0)); return accepted(init, 'resume', run('running', 0)); });
    renderCompany(f.server); await screen.findByText('任务已中断'); expect(posts(f)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '恢复分析' }));
    await screen.findByText(/可复用 1 份/); expect(screen.getByText(/需重新提取 2 份/)).toBeInTheDocument();
    expect(screen.getAllByText(/synthetic-outdated.docx/).length).toBeGreaterThan(0); expect(posts(f)).toHaveLength(0);
    fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await waitFor(() => expect(posts(f)).toHaveLength(1));
    expect(bodyOf(posts(f)[0][1]!)).toMatchObject({ company_id: 'company-one', expected_run_id: 'run-one', expected_version: 4 });
  });
  it('cancels an in-flight call without configuration and distinguishes acknowledgement from stopped', async () => {
    const f = fixture(run());
    f.onCommand(async (_path, init) => { const next = { ...run('cancel_requested', 2), in_flight: true }; f.setRun(next); return accepted(init, 'cancel', next); });
    renderCompany(f.server, { configurationLoader: async () => ({ ...syntheticConfiguration, validated: false }) });
    fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消分析' }));
    await screen.findByText('已请求取消，正在等待当前调用结束');
    expect(screen.getByText(/已经发出的模型请求可能继续消耗额度/)).toBeInTheDocument();
    expect(screen.getByText(/已完成 1 份/)).toBeInTheDocument();
    f.setRun(run('cancelled', 3));
    fireEvent.click(screen.getByRole('button', { name: '刷新任务状态' }));
    await screen.findAllByText('任务已取消'); expect(screen.getByRole('button', { name: '恢复分析' })).toBeEnabled();
    expect(posts(f)).toHaveLength(1);
  });
  it('requires an explicit fresh read and deliberate retry after stale resume CAS rejection', async () => {
    const f = fixture(run('interrupted', 4));
    f.onCommand(async () => { f.setRun(run('interrupted', 5)); return json({ detail: { code: 'TASK_VERSION_CONFLICT' } }, 409); });
    renderCompany(f.server); fireEvent.click(await screen.findByRole('button', { name: '查看处理进度' }));
    fireEvent.click(await screen.findByRole('button', { name: '恢复分析' })); fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await screen.findByText(/TASK_VERSION_CONFLICT/); expect(posts(f)).toHaveLength(1);
    expect(screen.getByRole('button', { name: '恢复分析' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '刷新任务状态' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '恢复分析' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '恢复分析' })); fireEvent.click(await screen.findByRole('button', { name: '确认恢复分析' }));
    await waitFor(() => expect(posts(f)).toHaveLength(2));
    expect(bodyOf(posts(f)[1][1]!)).toMatchObject({ expected_version: 5 });
    expect(bodyOf(posts(f)[1][1]!).request_id).not.toBe(bodyOf(posts(f)[0][1]!).request_id);
  });
  it('keeps read failure inline with GET retry and never creates a task at idle boot', async () => {
    const f = fixture(); f.failRead(true); renderCompany(f.server);
    fireEvent.click(await screen.findByRole('button', { name: '重试读取任务' }));
    expect(posts(f)).toHaveLength(0); f.failRead(false);
    fireEvent.click(screen.getByRole('button', { name: '重试读取任务' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试读取任务' })).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: '查看处理进度' })).not.toBeInTheDocument();
  });
});
