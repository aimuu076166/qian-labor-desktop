import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { selectEmploymentFiles } from '../src/lib/desktop';

vi.mock('../src/lib/desktop', () => ({
  selectEmploymentFiles: vi.fn(),
}));

const mockedSelect = vi.mocked(selectEmploymentFiles);

function renderApp(props: React.ComponentProps<typeof App>) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <App {...props} />
    </QueryClientProvider>,
  );
}

describe('App', () => {
  beforeEach(() => mockedSelect.mockReset());

  it('renders the desktop product identity without a web access-code gate', async () => {
    renderApp({
      backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:43123', token: 'test-token' }),
      apiFactory: () => async () => new Response('{}', { status: 200 }),
    });
    expect(screen.getByRole('heading', { name: '企安用工' })).toBeInTheDocument();
    expect(screen.getByText('本地优先劳动用工风险体检')).toBeInTheDocument();
    expect(screen.queryByText('访问码')).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '选择企业材料' })).toBeInTheDocument();
  });

  it('runs the desktop path from native selection through dashboard, source detail, and deletion', async () => {
    mockedSelect.mockResolvedValue(['/tmp/fictional-contract.docx']);
    const request = vi.fn(async (path: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET';
      if (path === '/api/analyses' && method === 'POST') {
        return new Response(JSON.stringify({ id: 'analysis-one' }), { status: 201 });
      }
      if (path === '/api/analyses/analysis-one/import-paths' && method === 'POST') {
        return new Response(JSON.stringify({ files: [{ id: 'file-one' }] }), { status: 200 });
      }
      if (path === '/api/analyses/analysis-one/process' && method === 'POST') {
        return new Response(JSON.stringify({ status: 'queued', queue_mode: 'desktop' }), {
          status: 202,
        });
      }
      if (path === '/api/analyses/analysis-one/processing') {
        return new Response(
          JSON.stringify({
            analysis_id: 'analysis-one',
            status: 'completed',
            progress: 100,
            current_stage: 'completed',
            files: [{ id: 'file-one', filename: 'fictional-contract.docx', status: 'processed' }],
            jobs: [],
          }),
          { status: 200 },
        );
      }
      if (path === '/api/analyses/analysis-one/dashboard') {
        return new Response(
          JSON.stringify({
            summary: {
              analysis_id: 'analysis-one',
              status: 'completed',
              employee_count: 1,
              finding_count: 2,
              high_count: 1,
              medium_count: 0,
              insufficient_data_count: 1,
            },
            findings: [
              {
                id: 'finding-one',
                rule_id: 'CONTRACT_MISSING_ACTIVE',
                title: '在职员工合同材料缺失',
                severity: 'high',
                assessment_status: 'suspected_risk',
                requires_human_review: true,
              },
              {
                id: 'finding-two',
                rule_id: 'MATERIAL_COVERAGE_LOW',
                title: '关键材料覆盖率不足',
                severity: 'info',
                assessment_status: 'insufficient_data',
                requires_human_review: false,
              },
            ],
          }),
          { status: 200 },
        );
      }
      if (path === '/api/findings/finding-one') {
        return new Response(
          JSON.stringify({
            id: 'finding-one',
            analysis_id: 'analysis-one',
            rule_id: 'CONTRACT_MISSING_ACTIVE',
            title: '在职员工合同材料缺失',
            severity: 'high',
            assessment_status: 'suspected_risk',
            requires_human_review: true,
            summary: '本次材料中未发现书面劳动合同，请核对。',
            sources: [
              {
                id: 'source-one',
                file_id: 'file-one',
                file_name: 'fictional-contract.docx',
                locator_type: 'paragraph',
                location: { paragraph: 2 },
                excerpt: '完全虚构来源摘录',
              },
            ],
          }),
          { status: 200 },
        );
      }
      if (path === '/api/analyses/analysis-one' && method === 'DELETE') {
        return new Response(JSON.stringify({ id: 'analysis-one', status: 'deleted' }), {
          status: 200,
        });
      }
      return new Response('{}', { status: 404 });
    });

    renderApp({
      backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:43123', token: 'memory-token' }),
      apiFactory: () => request,
    });

    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    expect(screen.queryByText('无风险')).not.toBeInTheDocument();

    const findingTitle = screen.getByText('在职员工合同材料缺失');
    const findingButton = findingTitle.closest('button');
    expect(findingButton).not.toBeNull();
    fireEvent.click(findingButton!);

    expect(await screen.findByText('fictional-contract.docx')).toBeInTheDocument();
    expect(screen.getByText(/第 2 段/)).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /返回风险概览/ }));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '删除本次分析' }));

    expect(await screen.findByRole('button', { name: '选择企业材料' })).toBeInTheDocument();
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        '/api/analyses/analysis-one',
        expect.objectContaining({ method: 'DELETE' }),
      ),
    );
  });
});
