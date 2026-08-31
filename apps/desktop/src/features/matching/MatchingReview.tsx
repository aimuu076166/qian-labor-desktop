import { useEffect, useState } from 'react';

export type EmployeeOption = {
  employee_id: string;
  employee_name: string;
  employee_number: string | null;
  department: string | null;
};

export type MatchCandidate = {
  id: string;
  file_id: string | null;
  material_name: string | null;
  employee_id: string | null;
  employee_name: string;
  employee_number: string | null;
  extracted_fields: Record<string, unknown>;
  fact_ids: string[];
  score: number;
  reasons: string[];
  status: string;
  employee_options: EmployeeOption[];
};

export type MatchDecisionPayload = {
  candidate_id: string;
  decision: 'assign' | 'create_unknown' | 'merge' | 'unmatched';
  employee_id?: string;
  display_name?: string;
  source_employee_id?: string;
  target_employee_id?: string;
  fact_ids: string[];
};

type MatchingReviewProps = {
  candidates: MatchCandidate[];
  submitting?: boolean;
  onDecision: (payload: MatchDecisionPayload) => Promise<void>;
};

export function MatchingReview({
  candidates,
  submitting = false,
  onDecision,
}: MatchingReviewProps) {
  const candidate = candidates[0];
  const [employeeId, setEmployeeId] = useState(
    candidate?.employee_id ?? candidate?.employee_options[0]?.employee_id ?? '',
  );
  const [displayName, setDisplayName] = useState('');

  useEffect(() => {
    setEmployeeId(candidate?.employee_id ?? candidate?.employee_options[0]?.employee_id ?? '');
    setDisplayName('');
  }, [candidate?.id, candidate?.employee_id, candidate?.employee_options]);

  if (!candidate) {
    return (
      <section className="status-card" aria-label="matching-review-empty">
        <p>正在确认匹配结果…</p>
      </section>
    );
  }

  return (
    <section className="matching-review" aria-labelledby="matching-review-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">人工匹配</p>
          <h2 id="matching-review-title">请先确认员工匹配</h2>
          <p className="muted">
            还有 {candidates.length} 项材料需要确认。全部确认后才会继续风险计算。
          </p>
        </div>
      </div>

      <article className="match-card">
        <div className="match-evidence">
          <span>材料</span>
          <strong>{candidate.material_name ?? '未命名材料'}</strong>
          <span>识别结果</span>
          <strong>
            {candidate.employee_name}
            {candidate.employee_number ? ` · ${candidate.employee_number}` : ''}
          </strong>
        </div>

        <label className="field-label" htmlFor="match-employee">
          归属员工
        </label>
        <select
          id="match-employee"
          value={employeeId}
          onChange={(event) => setEmployeeId(event.target.value)}
        >
          {candidate.employee_options.map((option) => (
            <option key={option.employee_id} value={option.employee_id}>
              {option.employee_name}
              {option.employee_number ? ` · ${option.employee_number}` : ''}
              {option.department ? ` · ${option.department}` : ''}
            </option>
          ))}
        </select>

        <label className="field-label" htmlFor="match-new-display-name">
          新建人员显示名
        </label>
        <input
          id="match-new-display-name"
          value={displayName}
          maxLength={100}
          placeholder="仅用于本次本地分析"
          onChange={(event) => setDisplayName(event.target.value)}
        />

        <div className="match-actions">
          <button
            type="button"
            className="primary-action"
            disabled={submitting || !employeeId}
            onClick={() =>
              onDecision({
                candidate_id: candidate.id,
                decision: 'assign',
                employee_id: employeeId,
                fact_ids: candidate.fact_ids,
              })
            }
          >
            {submitting ? '正在确认…' : '确认归属'}
          </button>
          <button
            type="button"
            className="secondary-action"
            disabled={submitting || !displayName.trim()}
            onClick={() =>
              onDecision({
                candidate_id: candidate.id,
                decision: 'create_unknown',
                display_name: displayName.trim(),
                fact_ids: candidate.fact_ids,
              })
            }
          >
            创建未识别员工
          </button>
          {candidate.employee_id && employeeId && employeeId !== candidate.employee_id ? (
            <button
              type="button"
              className="secondary-action"
              disabled={submitting}
              onClick={() =>
                onDecision({
                  candidate_id: candidate.id,
                  decision: 'merge',
                  source_employee_id: candidate.employee_id!,
                  target_employee_id: employeeId,
                  fact_ids: candidate.fact_ids,
                })
              }
            >
              合并重复员工
            </button>
          ) : null}
          <button
            type="button"
            className="text-action"
            disabled={submitting}
            onClick={() =>
              onDecision({
                candidate_id: candidate.id,
                decision: 'unmatched',
                fact_ids: candidate.fact_ids,
              })
            }
          >
            暂不归属员工
          </button>
        </div>
      </article>
    </section>
  );
}
