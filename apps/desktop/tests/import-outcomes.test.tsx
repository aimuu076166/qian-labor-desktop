import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { selectEmploymentFiles } from '../src/lib/desktop';
import { companyServer, json, renderCompany } from './company-fixture';

vi.mock('../src/lib/desktop', () => ({ selectEmploymentFiles: vi.fn(), getProviderConfigurationStatus: vi.fn(),
  configureZhipuProvider: vi.fn(), markZhipuProviderValidated: vi.fn() }));

const results = [
  { index: 0, filename: 'first.csv', file_id: 'first', status: 'imported', error_code: null },
  { index: 1, filename: 'broken.pdf', file_id: null, status: 'error', error_code: 'DESKTOP_IMPORT_CONTENT_INVALID' },
  { index: 2, filename: 'last.csv', file_id: 'last', status: 'duplicate', error_code: null },
];
describe('ordinary native import outcomes in actual App', () => {
  it.each([false, true])('shows each result and never auto-processes (all errors=%s)', async allError => {
    const rows = allError ? results.map(r => ({ ...r, status: 'error', file_id: null, error_code: 'DESKTOP_IMPORT_CONTENT_INVALID' })) : results;
    const server = companyServer({ analysisId: allError ? null : 'current', override: async path => path.endsWith('/import-paths')
      ? json({ analysis_id: server.getAnalysisId(), files: [], results: rows }) : undefined });
    renderCompany(server);
    vi.mocked(selectEmploymentFiles).mockResolvedValueOnce(['/tmp/first.csv', '/tmp/broken.pdf', '/tmp/last.csv']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    const panel = await screen.findByRole('region', { name: '本次导入结果' });
    expect(within(panel).getByText('first.csv')).toBeInTheDocument();
    expect(within(panel).getByText('broken.pdf')).toBeInTheDocument();
    expect(within(panel).getByText('last.csv')).toBeInTheDocument();
    expect(within(panel).getAllByText(/DESKTOP_IMPORT_CONTENT_INVALID/)).toHaveLength(allError ? 3 : 1);
    expect(within(panel).getByText(allError ? '未导入新材料，3 项失败。' : '新导入 1 项，重复 1 项，失败 1 项。')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '开始分析' })).toBeInTheDocument();
    if (allError) {
      expect(screen.getByRole('button', { name: '开始分析' })).toBeDisabled();
      expect(screen.getByText('尚未添加材料。')).toBeInTheDocument();
    } else expect(screen.getByRole('cell', { name: 'synthetic-contract.docx' })).toBeInTheDocument();
    expect(server.request.mock.calls.some(([path]) => path.endsWith('/process'))).toBe(false);
  });

  it('keeps malformed outcomes unknown and locked through failed reconciliation and Settings', async () => {
    let posted = false; let failRead = true;
    const server = companyServer({ override: async path => {
      if (path.endsWith('/import-paths')) { posted = true; return json({ analysis_id: 'another-analysis', results }); }
      if (posted && failRead && path.endsWith('/current-analysis')) throw new Error('private GET error');
    } }); renderCompany(server);
    vi.mocked(selectEmploymentFiles).mockResolvedValueOnce(['/tmp/first.csv']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对导入结果' }));
    expect(await screen.findByText('核对未完成，请再次读取。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试本次导入' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始分析' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
    expect(screen.getByRole('button', { name: '添加材料' })).toBeDisabled();
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/import-paths'))).toHaveLength(1);
    expect(screen.queryByRole('region', { name: '本次导入结果' })).not.toBeInTheDocument();
    failRead = false;
    fireEvent.click(screen.getByRole('button', { name: '核对导入结果' }));
    await screen.findByRole('button', { name: '重试本次导入' });
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/import-paths'))).toHaveLength(1);
  });

  it('reconciles unknown import with GET before explicit duplicate-safe retry and preserves files', async () => {
    let attempts = 0;
    const server = companyServer({ override: async path => {
      if (path.endsWith('/import-paths')) {
        if (++attempts === 1) throw new Error('/private/raw-name DESKTOP_REQUEST_TIMEOUT');
        return json({ analysis_id: 'current', files: [], results: [results[2]] .map(r => ({ ...r, index: 0 })) });
      }
    } }); renderCompany(server);
    vi.mocked(selectEmploymentFiles).mockResolvedValueOnce(['/private/raw-name.csv']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    fireEvent.click(await screen.findByRole('button', { name: '核对导入结果' }));
    expect(attempts).toBe(1);
    fireEvent.click(await screen.findByRole('button', { name: '重试本次导入' }));
    expect(await screen.findByRole('region', { name: '本次导入结果' })).toHaveTextContent('重复 1 项');
    expect(attempts).toBe(2);
    expect(screen.queryByText(/\/private\/raw-name/)).not.toBeInTheDocument();
    const paths = server.request.mock.calls.map(([path]) => path);
    const first = paths.indexOf('/api/analyses/current/import-paths');
    const retry = paths.lastIndexOf('/api/analyses/current/import-paths');
    expect(paths.slice(first + 1, retry)).toContain('/api/analyses/current/workspace');
    expect(paths.slice(first + 1, retry)).toContain('/api/company-workspaces/company-one/current-analysis');
    expect(server.request.mock.calls.some(([path]) => path.endsWith('/process'))).toBe(false);
  });

  it('keeps company B material and process target after company A delayed resolve', async () => {
    let finish!: (value: Response) => void;
    let resolving = false;
    const server = companyServer({ override: async (path, init) => {
      if (path === '/api/company-workspaces') return json([server.company, { ...server.company, id: 'company-b', display_name: '合成企业B' }]);
      if (path === '/api/company-workspaces/company-one/current-analysis' && !init?.method) {
        resolving = true; return new Promise(resolve => { finish = resolve; });
      }
      if (path.startsWith('/api/company-workspaces/company-b/current?')) return json({ ...server.projection(),
        company: { ...server.company, id: 'company-b', display_name: '合成企业B' },
        current_analysis: { ...server.metadata(), analysis_id: 'current-b', company_id: 'company-b' } });
      if (path === '/api/analyses/current-b/workspace') return json({ analysis: { id: 'current-b', name: 'B材料档案', company_display_name: '合成企业B', status: 'uploading' },
        files: [{ id: 'b-file', filename: 'B.csv', status: 'uploaded', progress: 0, classified_kind: 'unknown', detected_kind: 'unknown', error_code: null, size_bytes: 1 }] });
      if (path.startsWith('/api/analyses/current-b/task?')) return json({ analysis_id: 'current-b', company_id: 'company-b', business_status: 'uploading', run: null,
        read_only: false, history: [], limit: 20, offset: 0, resume_preview: { reusable_file_ids: [], extraction_file_ids: ['b-file'] }, usage_notice: 'admitted_requests_may_consume_quota_unknown_is_not_zero' });
    } }); renderCompany(server);
    vi.mocked(selectEmploymentFiles).mockResolvedValueOnce(['/tmp/first.csv']);
    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    await waitFor(() => expect(resolving).toBe(true));
    // Force the selection event across the existing picker UI lock to exercise late-state safety.
    fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: 'company-b' } });
    await waitFor(() => expect(screen.getByLabelText('当前企业')).toHaveValue('company-b'));
    fireEvent.click(screen.getByRole('button', { name: '材料' }));
    await screen.findByText('B.csv');
    await act(async () => { finish(json(server.metadata())); });
    expect(screen.getByText('B.csv')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '开始分析' }));
    await waitFor(() => expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/current-b/task/start')).toBe(true));
    expect(server.request.mock.calls.some(([path]) => path === '/api/analyses/current/task/start')).toBe(false);
  });
});
