import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ReportView } from '../src/features/report/ReportView';

describe('analysis report', () => {
  it('renders consistent findings and invokes the macOS print flow', () => {
    const onPrint = vi.fn();
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
        onPrint={onPrint}
      />,
    );

    expect(screen.getByRole('heading', { name: '企业用工风险体检报告' })).toBeInTheDocument();
    expect(screen.getByText('完全虚构企业')).toBeInTheDocument();
    expect(screen.getByText(/虚构合同\.docx/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打印或保存 PDF' }));
    expect(onPrint).toHaveBeenCalledOnce();
  });
});
