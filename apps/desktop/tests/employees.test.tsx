import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  EmployeeDetail,
  EmployeeLedger,
} from '../src/features/employees/EmployeeLedger';

describe('employee ledger', () => {
  const scope = {
    identifier: 'labor_materials_v1' as const, display_label: '用工材料体检 v1',
    excluded_rule_codes: ['R09', 'R13', 'R14', 'R15'], excluded_rule_ids: ['A', 'B', 'C', 'D'],
    not_evaluated_reasons: { R16: '无法核实社保延续来源，本项暂未评估。' },
    payroll_evaluated: false, attendance_evaluated: false,
    settlement_document_label: '结算文件（final_pay），不要求工资表，不计算工资或补偿',
  };

  it('propagates the server scope to the employee ledger and detail', () => {
    const ledger = { total: 0, page: 1, pages: 0, page_size: 25, department_options: [], items: [],
      assessment_scope: scope };
    const { unmount } = render(<EmployeeLedger payload={ledger} onBack={vi.fn()} onSelectEmployee={vi.fn()} />);
    expect(screen.getByText(/工资核算与考勤核算未评估/)).toBeInTheDocument();
    unmount();
    render(<EmployeeDetail payload={{ assessment_scope: scope, employee: { id: 'synthetic',
      employee_number: 'SYN-001', masked_name: '虚构员工', department: '未分组', job_title: null,
      employment_status: 'active', match_status: 'confirmed' }, findings: [] }} onBack={vi.fn()}
      onSelectFinding={vi.fn()} />);
    expect(screen.getByText(/无法核实社保延续来源/)).toBeInTheDocument();
  });
  it('opens retired findings separately without describing them as resolved', () => {
    const onSelectFinding = vi.fn();
    render(<EmployeeDetail payload={{ employee: { id: 'synthetic', employee_number: 'SYN-001',
      masked_name: '虚构员工', department: '未分组', job_title: null,
      employment_status: 'active', match_status: 'confirmed' }, findings: [], retired_findings: [{
        id: 'retired-one', rule_id: 'R01', title: '历史合同事项', summary: '虚构摘要', category: 'contract',
        severity: 'high', severity_label: '高风险', assessment_status: 'risk', status_label: '存在风险',
        requires_human_review: true, review_status: 'open', review_status_label: '待处理',
        employee_id: 'synthetic', employee_name: '虚构员工', department: '未分组', due_date: null,
      }] }} onBack={vi.fn()} onSelectFinding={onSelectFinding} />);
    expect(screen.getByRole('heading', { name: '历史事项（不计入当前统计）' })).toBeInTheDocument();
    expect(screen.getByText('退出当前结果不代表风险已解决，原有复核记录仍可查阅。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /历史合同事项.*查看历史复核/ }));
    expect(onSelectFinding).toHaveBeenCalledWith('retired-one');
  });
  it('labels unknown employee coverage as pending instead of a definite percentage', () => {
    render(<EmployeeLedger payload={{ total: 1, page: 1, pages: 1, page_size: 25, department_options: [],
      items: [{ id: 'synthetic', employee_number: 'SYN-001', masked_name: '虚构员工', department: '未分组',
        job_title: null, employment_status: 'unknown', match_status: 'confirmed', risk_counts: { high: 0, medium: 0 },
        insufficient_data_count: 0, requires_human_review_count: 1, material_coverage: 0 }] }}
      onBack={vi.fn()} onSelectEmployee={vi.fn()} />);
    expect(screen.getAllByText('待确认')).toHaveLength(2);
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
  });
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
