import { useEffect, useState } from 'react';
import type { EmployeeRecord } from '../workspace/useCompanyWorkspace';
import { describeOperationError } from '../../lib/errorMessages';

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
  employee_record_id?: string;
  expected_record_version?: number;
  display_name?: string;
  employee_number?: string;
  source_employee_id?: string;
  target_employee_id?: string;
  fact_ids: string[];
};

export type MatchingDraft = { employeeId: string; displayName: string; employeeNumber: string };
type MatchingReviewProps = {
  candidates: MatchCandidate[];
  currentCompanyId?: string;
  employeeRecordOptions?: EmployeeRecord[];
  draftCache?: Map<string, MatchingDraft>;
  submitting?: boolean;
  error?: string | null;
  onDecision: (payload: MatchDecisionPayload) => Promise<void>;
};

function suggestedNumber(candidate?: MatchCandidate): string {
  const ids = candidate?.extracted_fields.employee_ids;
  return Array.isArray(ids) && ids.length === 1 && typeof ids[0] === 'string'
    ? ids[0].slice(0, 80) : '';
}

export function MatchingReview(props: MatchingReviewProps) {
  const candidate = props.candidates[0];
  if (!candidate) return <section className="status-card" aria-label="matching-review-empty">
    <p>正在确认匹配结果…</p>
  </section>;
  return <CandidateReview key={candidate.id} {...props} candidate={candidate} />;
}

function CandidateReview({
  candidates, candidate, error, currentCompanyId, employeeRecordOptions = [], draftCache,
  submitting = false,
  onDecision,
}: MatchingReviewProps & { candidate: MatchCandidate }) {
  const [employeeId, setEmployeeId] = useState(
    draftCache?.get(candidate.id)?.employeeId ?? (currentCompanyId ? '' : candidate?.employee_id ?? ''),
  );
  const [displayName, setDisplayName] = useState(draftCache?.get(candidate.id)?.displayName ?? '');
  const [employeeNumber, setEmployeeNumber] = useState(draftCache?.get(candidate.id)?.employeeNumber ?? suggestedNumber(candidate));
  useEffect(() => { draftCache?.set(candidate.id, { employeeId, displayName, employeeNumber }); }, [draftCache, candidate.id, employeeId, displayName, employeeNumber]);
  const selectedRecord = employeeRecordOptions.find(item => item.id === employeeId);

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

      {error ? <p role="alert">{error === 'MATCH_EMPLOYEE_NUMBER_EXISTS' || error === 'WORKSPACE_EMPLOYEE_NUMBER_EXISTS'
        ? '该工号已存在，请选择已有员工确认归属，不要重复创建。'
        : `确认未完成：${describeOperationError(error)}输入已保留，请核对后重试。`}</p> : null}

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
          disabled={submitting}
          value={employeeId}
          onChange={(event) => setEmployeeId(event.target.value)}
        >
          <option value="">请选择归属员工</option>
          {currentCompanyId ? employeeRecordOptions.map(option => <option key={option.id} value={option.id}>
            {option.masked_name}{option.employee_number ? ` · ${option.employee_number}` : ''}{option.department ? ` · ${option.department}` : ''}
          </option>) : candidate.employee_options.map((option) => (
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
          disabled={submitting}
          value={displayName}
          maxLength={100}
          placeholder={currentCompanyId ? '用于企业本地员工档案' : '仅用于本次本地分析'}
          onChange={(event) => setDisplayName(event.target.value)}
        />

        <label className="field-label" htmlFor="match-new-number">确认工号（可选）</label>
        <input id="match-new-number" value={employeeNumber} maxLength={80} disabled={submitting}
          placeholder="核对原材料后填写；不确定可以留空"
          onChange={(event) => setEmployeeNumber(event.target.value)} />
        <p className="muted">工号用于将后续材料对应到同一员工。已有相同工号时，请选择上方归属员工。</p>

        <div className="match-actions">
          <button
            type="button"
            className="primary-action"
            disabled={submitting || !employeeId}
            onClick={() =>
              onDecision({
                candidate_id: candidate.id,
                ...(currentCompanyId && selectedRecord ? { decision: 'create_unknown' as const,
                  employee_record_id: selectedRecord.id, expected_record_version: selectedRecord.version, display_name: selectedRecord.masked_name }
                  : { decision: 'assign' as const, employee_id: employeeId }),
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
                ...(employeeNumber.trim() ? { employee_number: employeeNumber.trim() } : {}),
                fact_ids: candidate.fact_ids,
              })
            }
          >
            {currentCompanyId ? '新建员工并归属' : '创建未识别员工'}
          </button>
          {!currentCompanyId && candidate.employee_id && employeeId && employeeId !== candidate.employee_id ? (
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
