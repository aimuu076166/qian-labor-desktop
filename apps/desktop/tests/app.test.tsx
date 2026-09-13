import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { selectEmploymentFiles } from '../src/lib/desktop';
import { companyServer, json, renderCompany, syntheticConfiguration } from './company-fixture';

vi.mock('../src/lib/desktop', () => ({ selectEmploymentFiles: vi.fn(), getProviderConfigurationStatus: vi.fn(),
  configureZhipuProvider: vi.fn(), markZhipuProviderValidated: vi.fn() }));

describe('desktop production integration', () => {
  it.each(['设置', '模型设置'].flatMap(entry => ['return', 'save'].map(action => ({ entry, action }))))(
    'retains detail destination after repeated $entry entry and $action', async ({ entry, action }) => {
      const server = companyServer({ override: async path => path === '/api/provider/connection-test' ? json({ status: 'connected' }) : undefined });
      const configure = vi.fn(async () => ({ ...syntheticConfiguration, validated: false }));
      const validate = vi.fn(async () => syntheticConfiguration);
      renderCompany(server, { providerConfigurator: configure, providerValidator: validate });
      fireEvent.click(await screen.findByRole('button', { name: '查看合成员**详情' }));
      await screen.findByRole('heading', { name: '合成员**' });
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      await screen.findByRole('heading', { name: '连接智谱 GLM' });
      fireEvent.click(screen.getByRole('button', { name: entry }));
      if (action === 'save') {
        fireEvent.change(screen.getByLabelText('智谱 API Key'), { target: { value: 'synthetic-repeat-settings-key' } });
        fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
      } else fireEvent.click(screen.getByRole('button', { name: '返回工作区' }));
      expect(await screen.findByRole('heading', { name: '合成员**' })).toBeInTheDocument();
      if (action === 'save') { expect(configure).toHaveBeenCalledOnce(); expect(validate).toHaveBeenCalledOnce(); }
    });
  it.each(['extracting', 'failed'])('does not display raw errors in current %s material state', async status => {
    const server = companyServer({ status, override: async path => path.endsWith('/processing') ? json({
      analysis_id: 'current', status: 'failed', progress: 100, current_stage: 'failed',
      files: [{ error_code: 'private material raw failure' }],
    }) : undefined });
    renderCompany(server);
    await screen.findByText('已建档员工 1 人');
    fireEvent.click(screen.getByRole('button', { name: '材料' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    expect(screen.queryByText(/private material raw failure/)).not.toBeInTheDocument();
  });

  it('renders local setup without a web access-code or provider gate', async () => {
    const server = companyServer({ exists: false, analysisId: null });
    renderCompany(server, { configurationLoader: async () => ({ ...syntheticConfiguration, configured: false, validated: false }) });
    expect(await screen.findByRole('heading', { name: '建立企业本地档案' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '企安用工' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/访问码/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('智谱 API Key')).not.toBeInTheDocument();
  });

  it('reopens the selected company on its employee workbench rather than globally latest batch', async () => {
    const server = companyServer(); renderCompany(server);
    expect(await screen.findByText('已建档员工 1 人')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '查看合成员**详情' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/latest')).toBe(false);
    expect(server.request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  });

  it('preserves failed materials and permits explicit retry of that same current corpus', async () => {
    const server = companyServer({ status: 'failed' }); renderCompany(server);
    await screen.findByText('已建档员工 1 人');
    fireEvent.click(await screen.findByRole('button', { name: '材料' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    await waitFor(() => expect(server.request).toHaveBeenCalledWith('/api/analyses/current/task/start', expect.objectContaining({ method: 'POST',
      body: expect.stringContaining('request_id') })));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
  });

  it('runs native selection, current import, analysis, canonical detail, report and source trace with current deletion absent', async () => {
    vi.mocked(selectEmploymentFiles).mockResolvedValue(['/tmp/synthetic-contract.docx']);
    const server = companyServer({ analysisId: null }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(await screen.findByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    const create = server.request.mock.calls.find(([path, init]) => path.endsWith('/current-analysis') && init?.method === 'POST');
    expect(JSON.parse(String(create?.[1]?.body))).toEqual({ id: expect.stringMatching(/^[0-9a-f-]{36}$/), expected_company_version: 0 });
    fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看员工台账' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看合成员**详情' }));
    expect(await screen.findByRole('heading', { name: '合成员**' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path.endsWith('/employees/record-one'))).toBe(true);
    expect(server.request.mock.calls.some(([path]) => path.endsWith('/employees/snapshot-one'))).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /合成合同事项待核查/ }));
    expect(await screen.findByText('完全虚构来源摘录')).toBeInTheDocument();
    expect(screen.getByText('第 2 段')).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /返回员工详情/ }));
    expect(await screen.findByRole('heading', { name: '合成员**' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '报告' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看当前报告草稿' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险体检报告' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '删除本次分析' })).not.toBeInTheDocument();
    expect(server.request.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);
  });

  it('exposes pending matching, explicitly binds a stable employee and then refreshes results', async () => {
    const server = companyServer({ status: 'matching_review' }); renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '确认员工匹配' }));
    expect(await screen.findByRole('heading', { name: '请先确认员工匹配' })).toBeInTheDocument();
    expect(screen.queryByText('分析完成')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认归属' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('归属员工'), { target: { value: 'record-one' } });
    fireEvent.click(screen.getByRole('button', { name: '确认归属' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    expect(server.request).toHaveBeenCalledWith('/api/analyses/current/matching-decisions', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ candidate_id: 'candidate', decision: 'create_unknown',
        employee_record_id: 'record-one', expected_record_version: 0, display_name: '合成员**', fact_ids: ['fact'] }),
    }));
  });

  it('validates write-only GLM configuration only after explicit save and returns to context', async () => {
    const server = companyServer({ override: async path => path === '/api/provider/connection-test' ? json({ status: 'connected' }) : undefined });
    const configure = vi.fn(async () => ({ ...syntheticConfiguration, validated: false }));
    const validate = vi.fn(async () => syntheticConfiguration);
    renderCompany(server, { configurationLoader: async () => ({ ...syntheticConfiguration, configured: false, validated: false }),
      providerConfigurator: configure, providerValidator: validate });
    await screen.findByText('已建档员工 1 人');
    fireEvent.click(screen.getByRole('button', { name: '模型设置' }));
    fireEvent.change(await screen.findByLabelText('智谱 API Key'), { target: { value: 'synthetic-ui-key-value' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
    expect(await screen.findByRole('button', { name: '选择企业材料' })).toBeInTheDocument();
    expect(configure).toHaveBeenCalledOnce(); expect(validate).toHaveBeenCalledOnce();
    expect(server.request).toHaveBeenCalledWith('/api/provider/connection-test', { method: 'POST' });
    expect(screen.queryByDisplayValue('synthetic-ui-key-value')).not.toBeInTheDocument();
  });

  it('shows stable provider connection failure without exposing the key', async () => {
    const server = companyServer({ override: async path => path === '/api/provider/connection-test'
      ? json({ detail: { code: 'AI_PROVIDER_ERROR' } }, 502) : undefined });
    renderCompany(server, { providerConfigurator: async () => ({ ...syntheticConfiguration, validated: false }) });
    fireEvent.click(await screen.findByRole('button', { name: '模型设置' }));
    fireEvent.change(await screen.findByLabelText('智谱 API Key'), { target: { value: 'synthetic-ui-key-value' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('AI_PROVIDER_ERROR');
    expect(screen.queryByRole('button', { name: '选择企业材料' })).not.toBeInTheDocument();
  });
});
