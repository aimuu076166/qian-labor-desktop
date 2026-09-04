import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  EmployeeDetail,
  EmployeeLedger,
} from '../src/features/employees/EmployeeLedger';

describe('employee ledger', () => {
  it('shows masked employee data, risk counts, coverage, and opens detail', () => {
    const onSelectEmployee = vi.fn();
    render(
      <EmployeeLedger
        payload={{
          items: [
            {
              id: 'employee-one',
              employee_number: 'F-100',
              masked_name: '虚构员**',
              department: '虚构制造部',
              job_title: '虚构操作员',
              employment_status: 'active',
              match_status: 'confirmed',
              risk_counts: { high: 1, medium: 0 },
              insufficient_data_count: 1,
              requires_human_review_count: 1,
              material_coverage: 0.6,
            },
          ],
          total: 1,
          page: 1,
          page_size: 25,
          pages: 1,
          department_options: ['虚构制造部'],
        }}
        onSelectEmployee={onSelectEmployee}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByText('虚构员**')).toBeInTheDocument();
    expect(screen.getByText('60%')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /查看虚构员/ }));
    expect(onSelectEmployee).toHaveBeenCalledWith('employee-one');
  });

  it('keeps insufficient-data and human-review semantics in employee detail', () => {
    const onSelectFinding = vi.fn();
    render(
      <EmployeeDetail
        payload={{
          employee: {
            id: 'employee-one',
            employee_number: 'F-100',
            masked_name: '虚构员**',
            department: '虚构制造部',
            job_title: '虚构操作员',
            employment_status: 'active',
            match_status: 'confirmed',
          },
          findings: [
            {
              id: 'finding-one',
              rule_id: 'R01',
              title: '劳动合同签订事项待核查',
              summary: '完全虚构的风险摘要',
              category: 'contract',
              severity: 'high',
              severity_label: '高风险',
              assessment_status: 'insufficient_data',
              status_label: '资料不足',
              requires_human_review: true,
              review_status: 'open',
              review_status_label: '待处理',
              employee_id: 'employee-one',
              employee_name: '虚构员**',
              department: '虚构制造部',
              due_date: null,
            },
          ],
        }}
        onSelectFinding={onSelectFinding}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByText('资料不足')).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /劳动合同签订事项待核查/ }));
    expect(onSelectFinding).toHaveBeenCalledWith('finding-one');
  });
});
