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
