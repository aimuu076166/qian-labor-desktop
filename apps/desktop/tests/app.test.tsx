import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { selectEmploymentFiles } from '../src/lib/desktop';

vi.mock('../src/lib/desktop', () => ({
  selectEmploymentFiles: vi.fn(),
  getProviderConfigurationStatus: vi.fn(),
  configureZhipuProvider: vi.fn(),
  markZhipuProviderValidated: vi.fn(),
}));

const mockedSelect = vi.mocked(selectEmploymentFiles);
const validatedConfiguration = {
  provider: 'zhipu',
  configured: true,
  validated: true,
  textModel: 'glm-synthetic-text',
  visionModel: 'glm-synthetic-vision',
  baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
};

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
      configurationLoader: async () => validatedConfiguration,
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
            overview: {
              analysis_id: 'analysis-one',
              company_name: '完全虚构企业',
              status: 'completed',
              is_demo: false,
              summary: {
                employee_count: 1,
                high_count: 1,
                medium_count: 0,
                low_count: 0,
                insufficient_data_count: 1,
                coverage_rate: 0.6,
                affected_employee_count: 1,
                requires_human_review_count: 1,
                deadline_30_count: 0,
                classification_pending: false,
              },
              categories: [{ code: 'contract', label: '劳动合同', count: 1 }],
              departments: [{ name: '虚构制造部', count: 1 }],
              material_coverage: {
                overall: 0.6,
                classification_pending: false,
                items: [],
              },
              deadline_buckets: [],
              priority_findings: [],
            },
          }),
          { status: 200 },
        );
      }
      if (path === '/api/analyses/analysis-one/employees') {
        return new Response(JSON.stringify({
          items: [{
            id: 'employee-one', employee_number: 'F-100', masked_name: '虚构员**',
            department: '虚构制造部', job_title: '虚构操作员', employment_status: 'active',
            match_status: 'confirmed', risk_counts: { high: 1, medium: 0 },
            insufficient_data_count: 1, requires_human_review_count: 1, material_coverage: 0.6,
          }],
          total: 1, page: 1, page_size: 25, pages: 1, department_options: ['虚构制造部'],
        }), { status: 200 });
      }
      if (path === '/api/analyses/analysis-one/employees/employee-one') {
        return new Response(JSON.stringify({
          employee: {
            id: 'employee-one', employee_number: 'F-100', masked_name: '虚构员**',
            department: '虚构制造部', job_title: '虚构操作员', employment_status: 'active',
            match_status: 'confirmed',
          },
          findings: [],
        }), { status: 200 });
      }
      if (path === '/api/analyses/analysis-one/report') {
        return new Response(JSON.stringify({
          analysis_id: 'analysis-one', company_name: '完全虚构企业',
          generated_at: '2026-08-31T09:00:00Z', status: 'completed', is_demo: false,
          summary: {
            employee_count: 1, high_count: 1, medium_count: 0, low_count: 0,
            insufficient_data_count: 1, coverage_rate: 0.6, affected_employee_count: 1,
            requires_human_review_count: 1, deadline_30_count: 0, classification_pending: false,
          },
          material_coverage: { overall: 0.6, items: [] },
          employees: [{ id: 'employee-one', employee_number: 'F-100', masked_name: '虚构员**' }],
          findings: [{
            id: 'finding-one', rule_id: 'R01', title: '劳动合同签订事项待核查',
            severity_label: '高风险', status_label: '疑似风险', requires_human_review: true,
            employee_name: '虚构员**', sources: [{ file_name: 'fictional-contract.docx', locator_type: 'paragraph', location: { paragraph: 2 } }],
          }],
        }), { status: 200 });
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
      configurationLoader: async () => validatedConfiguration,
    });

    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    expect(screen.queryByText('无风险')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '查看员工台账' }));
    expect(await screen.findByRole('heading', { name: '员工台账' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /查看虚构员/ }));
    expect(await screen.findByRole('heading', { name: '虚构员**' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回员工台账' }));
    fireEvent.click(await screen.findByRole('button', { name: '返回风险概览' }));

    fireEvent.click(await screen.findByRole('button', { name: '生成体检报告' }));
    expect(await screen.findByRole('heading', { name: '企业用工风险体检报告' })).toBeInTheDocument();
    expect(screen.getByText(/fictional-contract\.docx/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回风险概览' }));

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

  it('routes matching_review to human review instead of claiming analysis completion', async () => {
    mockedSelect.mockResolvedValue(['/tmp/fictional-ambiguous.docx']);
    let reviewed = false;
    const request = vi.fn(async (path: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET';
      if (path === '/api/analyses' && method === 'POST') {
        return new Response(JSON.stringify({ id: 'analysis-review' }), { status: 201 });
      }
      if (path === '/api/analyses/analysis-review/import-paths' && method === 'POST') {
        return new Response(JSON.stringify({ files: [{ id: 'file-review' }] }), {
          status: 200,
        });
      }
      if (path === '/api/analyses/analysis-review/process' && method === 'POST') {
        return new Response(JSON.stringify({ status: 'queued' }), { status: 202 });
      }
      if (path === '/api/analyses/analysis-review/processing') {
        return new Response(
          JSON.stringify({
            analysis_id: 'analysis-review',
            status: reviewed ? 'completed' : 'matching_review',
            progress: reviewed ? 100 : 85,
            current_stage: reviewed ? 'completed' : 'matching_review',
          }),
          { status: 200 },
        );
      }
      if (path === '/api/analyses/analysis-review/matching-candidates') {
        return new Response(
          JSON.stringify({
            analysis_id: 'analysis-review',
            candidates: [
              {
                id: 'candidate-review',
                file_id: 'file-review',
                material_name: 'fictional-ambiguous.docx',
                employee_id: 'employee-review',
                employee_name: '虚构员**',
                employee_number: 'F-REVIEW',
                extracted_fields: { fact_ids: ['fact-review'] },
                fact_ids: ['fact-review'],
                score: 0.72,
                reasons: ['multiple_identifier_values'],
                status: 'pending',
                employee_options: [
                  {
                    employee_id: 'employee-review',
                    employee_name: '虚构员**',
                    employee_number: 'F-REVIEW',
                    department: '虚构部门',
                  },
                ],
              },
            ],
          }),
          { status: 200 },
        );
      }
      if (
        path === '/api/analyses/analysis-review/matching-decisions' &&
        method === 'POST'
      ) {
        reviewed = true;
        return new Response(
          JSON.stringify({ analysis_id: 'analysis-review', analysis_status: 'completed' }),
          { status: 200 },
        );
      }
      if (path === '/api/analyses/analysis-review/dashboard') {
        return new Response(
          JSON.stringify({
            summary: {
              analysis_id: 'analysis-review',
              status: 'completed',
              employee_count: 1,
              finding_count: 0,
              high_count: 0,
              medium_count: 0,
              insufficient_data_count: 0,
            },
            findings: [],
          }),
          { status: 200 },
        );
      }
      return new Response('{}', { status: 404 });
    });

    renderApp({
      backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:43123', token: 'memory-token' }),
      apiFactory: () => request,
      configurationLoader: async () => validatedConfiguration,
    });

    fireEvent.click(await screen.findByRole('button', { name: '选择企业材料' }));

    expect(
      await screen.findByRole('heading', { name: '请先确认员工匹配' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('分析完成')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '企业用工风险概览' })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalledWith('/api/analyses/analysis-review/dashboard');

    expect(await screen.findByText('fictional-ambiguous.docx')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认归属' }));

    expect(await screen.findByRole('heading', { name: '企业用工风险概览' })).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith(
      '/api/analyses/analysis-review/matching-decisions',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          candidate_id: 'candidate-review',
          decision: 'assign',
          employee_id: 'employee-review',
          fact_ids: ['fact-review'],
        }),
      }),
    );
  });

  it('blocks material import until a Keychain-backed Zhipu connection is validated', async () => {
    const configure = vi.fn(async () => ({
      ...validatedConfiguration,
      configured: true,
      validated: false,
    }));
    const validate = vi.fn(async () => validatedConfiguration);
    const request = vi.fn(async (path: string) => {
      if (path === '/api/provider/connection-test') {
        return new Response(JSON.stringify({ provider: 'zhipu', status: 'connected' }), {
          status: 200,
        });
      }
      return new Response('{}', { status: 404 });
    });

    renderApp({
      backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:43123', token: 'memory-token' }),
      apiFactory: () => request,
      configurationLoader: async () => ({
        provider: 'zhipu',
        configured: false,
        validated: false,
        textModel: '',
        visionModel: '',
        baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
      }),
      providerConfigurator: configure,
      providerValidator: validate,
    });

    expect(await screen.findByRole('heading', { name: '连接智谱 GLM' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '选择企业材料' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('智谱 API Key'), {
      target: { value: 'synthetic-ui-key-value' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));

    expect(await screen.findByRole('button', { name: '选择企业材料' })).toBeInTheDocument();
    expect(configure).toHaveBeenCalledOnce();
    expect(request).toHaveBeenCalledWith('/api/provider/connection-test', {
      method: 'POST',
    });
    expect(validate).toHaveBeenCalledOnce();
  });

  it('shows the stable provider error returned by a failed real connection test', async () => {
    const request = vi.fn(async (path: string) => {
      if (path === '/api/provider/connection-test') {
        return new Response(JSON.stringify({ detail: { code: 'AI_PROVIDER_ERROR' } }), {
          status: 502,
        });
      }
      return new Response('{}', { status: 404 });
    });

    renderApp({
      backendLoader: async () => ({ baseUrl: 'http://127.0.0.1:43123', token: 'memory-token' }),
      apiFactory: () => request,
      configurationLoader: async () => ({
        ...validatedConfiguration,
        configured: false,
        validated: false,
      }),
      providerConfigurator: async () => ({
        ...validatedConfiguration,
        validated: false,
      }),
      providerValidator: async () => validatedConfiguration,
    });

    fireEvent.change(await screen.findByLabelText('智谱 API Key'), {
      target: { value: 'synthetic-ui-key-value' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('AI_PROVIDER_ERROR');
    expect(screen.queryByRole('button', { name: '选择企业材料' })).not.toBeInTheDocument();
  });
});
