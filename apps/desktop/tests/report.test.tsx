import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { invoke } from '@tauri-apps/api/core';
import { ReportView } from '../src/features/report/ReportView';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));

describe('analysis report', () => {
  beforeEach(() => { vi.mocked(invoke).mockReset(); });

  function renderReport() {
    render(
      <ReportView
        payload={{
          analysis_id: 'analysis-one',
          company_name: '完全虚构企业',
          generated_at: '2026-08-31T09:00:00Z',
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
          material_coverage: { overall: 0.6, items: [] },
          employees: [{ id: 'employee-one', masked_name: '虚构员**', employee_number: 'F-100' }],
          findings: [
            {
              id: 'finding-one',
              rule_id: 'R01',
              title: '劳动合同签订事项待核查',
              severity_label: '高风险',
              status_label: '资料不足',
              requires_human_review: true,
              employee_name: '虚构员**',
              sources: [
                {
                  file_name: '虚构合同.docx',
                  locator_type: 'paragraph',
                  location: { paragraph: 2 },
                },
              ],
            },
          ],
        }}
        onBack={vi.fn()}
      />,
    );
  }

  it('renders findings and requests native printing through the real desktop bridge', async () => {
    let finishPrint!: () => void;
    vi.mocked(invoke).mockImplementation(() => new Promise<void>((resolve) => { finishPrint = resolve; }));
    renderReport();
    expect(screen.getByRole('heading', { name: '企业用工风险体检报告' })).toBeInTheDocument();
    expect(screen.getByText('完全虚构企业')).toBeInTheDocument();
    expect(screen.getByText(/虚构合同\.docx/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打印或保存 PDF' }));
    expect(invoke).toHaveBeenCalledWith('print_analysis_report');
    expect(invoke).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: '正在打开打印窗口…' })).toBeDisabled();
    finishPrint();
    await waitFor(() => expect(screen.getByRole('button', { name: '打印或保存 PDF' })).toBeEnabled());
  });

  it('shows a safe error if native printing fails and allows another attempt', async () => {
    vi.mocked(invoke).mockRejectedValueOnce(new Error('private native details')).mockResolvedValueOnce(undefined);
    renderReport();
    fireEvent.click(screen.getByRole('button', { name: '打印或保存 PDF' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('无法打开系统打印窗口，请重试。');
    expect(screen.queryByText(/private native details/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打印或保存 PDF' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
