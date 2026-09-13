import { QueryClient } from '@tanstack/react-query';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { selectEmploymentFiles } from '../src/lib/desktop';
import { companyServer, renderCompany, syntheticConfiguration, json, taskReceipt } from './company-fixture';

vi.mock('../src/lib/desktop', () => ({ selectEmploymentFiles: vi.fn(), getProviderConfigurationStatus: vi.fn(),
  configureZhipuProvider: vi.fn(), markZhipuProviderValidated: vi.fn() }));

describe('persistent company material workspace', () => {
  it('refreshes a rejected native creation CAS through GET while preserving its UUID and selected paths', async () => {
    const server = companyServer({ analysisId: null, override: async (path, init) => {
      if (path === '/api/company-workspaces/company-one') return new Response(JSON.stringify(server.company));
      if (path.endsWith('/current-analysis') && init?.method === 'POST' && JSON.parse(String(init.body)).expected_company_version !== server.company.version)
        return new Response(JSON.stringify({ detail: { code: 'WORKSPACE_VERSION_CONFLICT' } }), { status: 409 });
      return undefined;
    } }); renderCompany(server);
    await screen.findByText('已建档员工 1 人'); server.company.version = 4;
    vi.mocked(selectEmploymentFiles).mockResolvedValueOnce(['/tmp/synthetic-contract.docx']);
    fireEvent.click(screen.getByRole('button', { name: '选择企业材料' }));
    await screen.findByRole('button', { name: '核对当前材料' });
    await waitFor(() => expect(screen.getByLabelText('当前企业')).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '核对当前材料' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续导入已选材料' }));
    await screen.findByRole('cell', { name: 'synthetic-contract.docx' });
    const creates = server.request.mock.calls.filter(([path, init]) => path.endsWith('/current-analysis') && init?.method === 'POST').map(([, init]) => JSON.parse(String(init?.body)));
    expect(creates).toEqual([{ id: expect.any(String), expected_company_version: 0 }, { id: creates[0].id, expected_company_version: 4 }]);
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/import-paths'))).toHaveLength(1);
  });
  it('retains the overview return target when an import completes while settings is open', async () => {
    const server = companyServer(); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '查看当前风险概览' }));
    await screen.findByRole('heading', { name: '企业用工风险概览' });
    let finish!: (paths: string[]) => void;
    vi.mocked(selectEmploymentFiles).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: '选择企业材料' }));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    await act(async () => { finish(['/tmp/synthetic-contract.docx']); });
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    expect(screen.getByText(/下列已保存结果尚未按最新材料更新/)).toBeInTheDocument();
  });
  it('reconciles uncertain corpus creation by the same UUID, never repeats POST, and imports only after explicit continuation', async () => {
    let posted = false; let failRead = true;
    const server = companyServer({ analysisId: null, override: async (path, init) => {
      if (path.endsWith('/current-analysis') && init?.method === 'POST') {
        server.setAnalysisId(JSON.parse(String(init.body)).id); server.setStatus('uploading'); posted = true;
        throw new Error('DESKTOP_REQUEST_TIMEOUT');
      }
      if (path.endsWith('/current-analysis') && posted && failRead) throw new Error('DESKTOP_CONNECTION_FAILED');
    } }); renderCompany(server);
    vi.mocked(selectEmploymentFiles).mockResolvedValue(['/tmp/synthetic-contract.docx']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(await screen.findByRole('button', { name: '核对当前材料' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path.endsWith('/import-paths'))).toBe(false);
    failRead = false; fireEvent.click(screen.getByRole('button', { name: '核对当前材料' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续导入已选材料' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    expect(server.request.mock.calls.filter(([path, init]) => path.endsWith('/current-analysis') && init?.method === 'POST')).toHaveLength(1);
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/import-paths'))).toHaveLength(1);
  });
  it('does not replay an uncertain process call and permits reading its actual status', async () => {
    let submitted!: RequestInit;
    const server = companyServer({ status: 'uploading', override: async (path, init) => {
      if (path.endsWith('/task/start')) { submitted = init!; server.setStatus('extracting'); throw new Error('DESKTOP_REQUEST_TIMEOUT'); }
      if (path.includes('/task/requests/')) return json(taskReceipt('current', submitted, 'running'));
    } }); renderCompany(server);
    await screen.findByText('已建档员工 1 人'); fireEvent.click(screen.getByRole('button', { name: '材料' }));
    fireEvent.click(await screen.findByRole('button', { name: '开始分析' }));
    expect(await screen.findByRole('button', { name: '核对处理状态' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '核对处理状态' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对处理状态' })).not.toBeInTheDocument());
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/task/start'))).toHaveLength(1);
    expect(screen.getByRole('button', { name: '员工' })).toBeEnabled();
  });
  it('preserves an uncertain matching draft through settings and back', async () => {
    const server = companyServer({ status: 'matching_review', override: async path => {
      if (path.endsWith('/matching-decisions')) throw new Error('DESKTOP_REQUEST_TIMEOUT');
    } }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '确认员工匹配' }));
    fireEvent.change(await screen.findByLabelText('新建人员显示名'), { target: { value: '合成待核对姓名' } });
    fireEvent.change(screen.getByLabelText('确认工号（可选）'), { target: { value: 'SYN-042' } });
    fireEvent.click(screen.getByRole('button', { name: '新建员工并归属' }));
    await screen.findByRole('button', { name: '核对已保存的结果' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(await screen.findByLabelText('新建人员显示名')).toHaveValue('合成待核对姓名');
    expect(screen.getByLabelText('确认工号（可选）')).toHaveValue('SYN-042');
  });
  it('opens explicitly selected historical materials read-only without replacing current corpus', async () => {
    const server = companyServer(); renderCompany(server);
    await screen.findByText('已建档员工 1 人');
    fireEvent.click(screen.getByRole('button', { name: '报告' }));
    fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开合成历史体检' }));
    expect(await screen.findByText(/历史材料只读/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '添加材料' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '开始分析' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '材料' }));
    expect(await screen.findByRole('button', { name: '添加材料' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/current/workspace')).toBe(true);
    expect(server.request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  });

  it.each([true, false])('reconciles uncertain matching without replay (committed=%s)', async committed => {
    const server = companyServer({ status: 'matching_review', override: async (path, init) => {
      if (path.endsWith('/matching-decisions') && init?.method === 'POST') {
        if (committed) server.setStatus('completed');
        throw new Error('DESKTOP_REQUEST_TIMEOUT');
      }
    } }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '确认员工匹配' }));
    fireEvent.change(await screen.findByLabelText('新建人员显示名'), { target: { value: '合成新员工' } });
    fireEvent.click(screen.getByRole('button', { name: '新建员工并归属' }));
    expect(await screen.findByRole('button', { name: '核对已保存的结果' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '新建员工并归属' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '核对已保存的结果' }));
    if (committed) expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    else {
      await waitFor(() => expect(screen.getByRole('button', { name: '新建员工并归属' })).toBeEnabled());
      expect(screen.getByLabelText('新建人员显示名')).toHaveValue('合成新员工');
    }
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/matching-decisions'))).toHaveLength(1);
  });

  it('locks company mutation during a picker but permits settings browsing without stealing navigation', async () => {
    const server = companyServer(); renderCompany(server);
    let finish!: (paths: string[]) => void;
    vi.mocked(selectEmploymentFiles).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(screen.getByLabelText('当前企业')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    await act(async () => { finish(['/tmp/synthetic-contract.docx']); });
    expect(await screen.findByRole('heading', { name: '连接智谱 GLM' })).toBeInTheDocument();
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/import-paths'))).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '返回工作区' }));
    expect(await screen.findByLabelText('搜索员工')).toBeInTheDocument();
  });

  it('keeps matching draft through failed background refresh and GET retry', async () => {
    let failed = false;
    const server = companyServer({ status: 'matching_review', override: async path => {
      if (failed && path.endsWith('/matching-candidates')) throw new Error('DESKTOP_CONNECTION_FAILED');
    } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderCompany(server, {}, client);
    fireEvent.click(await screen.findByRole('button', { name: '确认员工匹配' }));
    fireEvent.change(await screen.findByLabelText('新建人员显示名'), { target: { value: '人工确认名称' } });
    fireEvent.change(screen.getByLabelText('确认工号（可选）'), { target: { value: 'SYN-010' } });
    failed = true;
    await act(async () => { await client.refetchQueries({ queryKey: ['desktop-matching', 'current'] }); });
    expect(await screen.findByRole('alert')).toHaveTextContent('读取失败');
    expect(screen.getByLabelText('新建人员显示名')).toHaveValue('人工确认名称');
    failed = false;
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByLabelText('确认工号（可选）')).toHaveValue('SYN-010');
  });

  it.each(['processing', 'matching'] as const)('keeps recoverable %s read errors in context', async page => {
    let failed = true;
    const server = companyServer({ status: page === 'processing' ? 'extracting' : 'matching_review', override: async path => {
      if (failed && path.endsWith(page === 'processing' ? '/processing' : '/matching-candidates')) throw new Error('DESKTOP_CONNECTION_FAILED');
    } }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: page === 'processing' ? '查看处理进度' : '确认员工匹配' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('读取失败');
    failed = false;
    fireEvent.click(screen.getByRole('button', { name: '重试读取' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('imports locally before configuration and returns from settings to the same material list', async () => {
    const server = companyServer({ analysisId: null }); renderCompany(server, {
      configurationLoader: async () => ({ ...syntheticConfiguration, configured: false, validated: false }),
    });
    vi.mocked(selectEmploymentFiles).mockResolvedValue(['/tmp/synthetic-contract.docx']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path.endsWith('/process'))).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '配置模型后分析' }));
    expect(await screen.findByRole('heading', { name: '连接智谱 GLM' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回工作区' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
  });

  it('supplements the current corpus without creating another analysis', async () => {
    const server = companyServer(); renderCompany(server);
    vi.mocked(selectEmploymentFiles).mockResolvedValue(['/tmp/synthetic-contract.docx']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/current/import-paths')).toBe(true);
    expect(server.request.mock.calls.some(([path, init]) => path.endsWith('/current-analysis') && init?.method === 'POST')).toBe(false);
  });

  it('can explicitly leave the company to create another local archive without creating an empty analysis', async () => {
    const server = companyServer(); renderCompany(server);
    await screen.findByText('已建档员工 1 人');
    fireEvent.click(screen.getByRole('button', { name: '材料' }));
    expect(await screen.findByText('synthetic-contract.docx')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: '' } });
    expect(await screen.findByLabelText('企业名称')).toBeInTheDocument();
    expect(server.request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  });
});
