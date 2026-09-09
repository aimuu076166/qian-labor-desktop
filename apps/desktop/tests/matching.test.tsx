import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  MatchingReview,
  type MatchCandidate,
} from '../src/features/matching/MatchingReview';

function candidate(overrides: Partial<MatchCandidate> = {}): MatchCandidate {
  return {
    id: 'candidate-one',
    file_id: 'file-one',
    material_name: '虚构待匹配材料.docx',
    employee_id: 'employee-source',
    employee_name: '虚构员**',
    employee_number: 'F-001',
    extracted_fields: { fact_ids: ['fact-one'] },
    fact_ids: ['fact-one'],
    score: 0.72,
    reasons: ['multiple_identifier_values'],
    status: 'pending',
    employee_options: [
      {
        employee_id: 'employee-source',
        employee_name: '虚构员**',
        employee_number: 'F-001',
        department: '虚构一部',
      },
      {
        employee_id: 'employee-target',
        employee_name: '虚构同**',
        employee_number: 'F-002',
        department: '虚构二部',
      },
    ],
    ...overrides,
  };
}

describe('MatchingReview', () => {
  it('requires explicit stable record selection and submits its version without name merging', async () => {
    const onDecision = vi.fn(async () => undefined);
    render(<MatchingReview candidates={[candidate()]} currentCompanyId="company" employeeRecordOptions={[{
      id: 'record', company_id: 'company', masked_name: '合成员**', employee_number: 'SYN-1', department: null,
      job_title: null, lifecycle_status: 'active', version: 7, created_at: '2026-09-01',
    }]} onDecision={onDecision} />);
    expect(screen.getByLabelText('归属员工')).toHaveValue('');
    expect(screen.getByRole('button', { name: '确认归属' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('归属员工'), { target: { value: 'record' } });
    fireEvent.click(screen.getByRole('button', { name: '确认归属' }));
    expect(onDecision).toHaveBeenCalledWith(expect.objectContaining({ employee_record_id: 'record', expected_record_version: 7 }));
    expect(screen.queryByRole('button', { name: '合并重复员工' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '新建员工并归属' })).toBeInTheDocument();
  });
  it('keeps manual input when the same candidate is refreshed', () => {
    const onDecision = vi.fn();
    const view = render(<MatchingReview candidates={[candidate()]} onDecision={onDecision} />);
    fireEvent.change(screen.getByLabelText('新建人员显示名'), { target: { value: '人工确认名称' } });
    fireEvent.change(screen.getByLabelText('确认工号（可选）'), { target: { value: 'SYN-010' } });
    view.rerender(<MatchingReview candidates={[candidate({ score: 0.73 })]} onDecision={onDecision} />);
    expect(screen.getByLabelText('新建人员显示名')).toHaveValue('人工确认名称');
    expect(screen.getByLabelText('确认工号（可选）')).toHaveValue('SYN-010');
    view.rerender(<MatchingReview candidates={[candidate({ id: 'candidate-two' })]} onDecision={onDecision} />);
    expect(screen.getByLabelText('新建人员显示名')).toHaveValue('');
  });
  it('lets the user confirm a suggested employee number before creating a person', async () => {
    const onDecision = vi.fn(async () => undefined);
    render(<MatchingReview candidates={[candidate({ employee_id: null,
      employee_number: null, employee_options: [],
      extracted_fields: { employee_ids: ['SYN-010'], fact_ids: ['fact-one'] },
    })]} onDecision={onDecision} />);
    expect(screen.getByLabelText('确认工号（可选）')).toHaveValue('SYN-010');
    fireEvent.change(screen.getByLabelText('新建人员显示名'), { target: { value: '虚构人员' } });
    fireEvent.change(screen.getByLabelText('确认工号（可选）'), { target: { value: 'SYN-009' } });
    fireEvent.click(screen.getByRole('button', { name: '创建未识别员工' }));
    await waitFor(() => expect(onDecision).toHaveBeenCalledWith({ candidate_id: 'candidate-one',
      decision: 'create_unknown', display_name: '虚构人员', employee_number: 'SYN-009',
      fact_ids: ['fact-one'] }));
  });
  it('creates a new unknown employee for facts without a safe existing match', async () => {
    const onDecision = vi.fn(async () => undefined);
    render(
      <MatchingReview
        candidates={[
          candidate({
            employee_id: null,
            employee_name: '未识别人员',
            employee_number: null,
            employee_options: [],
          }),
        ]}
        onDecision={onDecision}
      />,
    );

    fireEvent.change(screen.getByLabelText('新建人员显示名'), {
      target: { value: '虚构临时人员' },
    });
    fireEvent.click(screen.getByRole('button', { name: '创建未识别员工' }));

    await waitFor(() =>
      expect(onDecision).toHaveBeenCalledWith({
        candidate_id: 'candidate-one',
        decision: 'create_unknown',
        display_name: '虚构临时人员',
        fact_ids: ['fact-one'],
      }),
    );
  });

  it('keeps the material unmatched only after an explicit action', async () => {
    const onDecision = vi.fn(async () => undefined);
    render(<MatchingReview candidates={[candidate()]} onDecision={onDecision} />);

    fireEvent.click(screen.getByRole('button', { name: '暂不归属员工' }));

    await waitFor(() =>
      expect(onDecision).toHaveBeenCalledWith({
        candidate_id: 'candidate-one',
        decision: 'unmatched',
        fact_ids: ['fact-one'],
      }),
    );
  });

  it('shows each pending material and lets the user review one candidate at a time', () => {
    const onDecision = vi.fn(async () => undefined);
    render(<MatchingReview candidates={[candidate(), candidate({
      id: 'candidate-two', material_name: '另一份虚构材料.xlsx', fact_ids: ['fact-two'],
      extracted_fields: { employee_ids: ['F-002'], fact_ids: ['fact-two'] }, employee_number: 'F-002',
    })]} onDecision={onDecision} />);

    expect(screen.getByText(/还有 2 项匹配事项/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /虚构待匹配材料\.docx/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /另一份虚构材料\.xlsx/ }));
    expect(screen.getByText('另一份虚构材料.xlsx')).toBeInTheDocument();
    expect(screen.getByLabelText('确认工号（可选）')).toHaveValue('F-002');
  });

  it('merges a duplicate source employee into the selected target', async () => {
    const onDecision = vi.fn(async () => undefined);
    render(<MatchingReview candidates={[candidate()]} onDecision={onDecision} />);

    fireEvent.change(screen.getByLabelText('归属员工'), {
      target: { value: 'employee-target' },
    });
    fireEvent.click(screen.getByRole('button', { name: '合并重复员工' }));

    await waitFor(() =>
      expect(onDecision).toHaveBeenCalledWith({
        candidate_id: 'candidate-one',
        decision: 'merge',
        source_employee_id: 'employee-source',
        target_employee_id: 'employee-target',
        fact_ids: ['fact-one'],
      }),
    );
  });
});
