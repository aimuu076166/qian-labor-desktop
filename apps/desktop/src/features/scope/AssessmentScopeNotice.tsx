import type { AssessmentScope } from '../../lib/api';

export function AssessmentScopeNotice({ scope, compact = false }: { scope?: AssessmentScope; compact?: boolean }) {
  if (!scope || !['labor_materials_v1', 'legacy_full_v1'].includes(scope.identifier)) {
    return (
      <section className="assessment-scope assessment-scope-unknown" aria-label="本次评估范围">
        <strong>评估范围尚未确认</strong>
        <p>无法确认工资、考勤项目是否已评估；当前结果不得视为安全结论。</p>
      </section>
    );
  }

  if (scope.identifier === 'legacy_full_v1') {
    return (
      <section className="assessment-scope" aria-label="本次评估范围">
        <strong>历史完整评估范围：{scope.display_label}</strong>
        <p>这是历史分析采用的完整规则范围，未按当前材料体检范围重新解释。</p>
      </section>
    );
  }

  const limitations = Object.values(scope.not_evaluated_reasons).filter(Boolean);
  return (
    <section className="assessment-scope" aria-label="本次评估范围">
      <strong>本次范围：{scope.display_label}</strong>
      <p>{!scope.payroll_evaluated && !scope.attendance_evaluated
        ? '工资核算与考勤核算未评估；未评估不代表安全。'
        : !scope.payroll_evaluated ? '工资核算未评估；未评估不代表安全。'
        : !scope.attendance_evaluated ? '考勤核算未评估；未评估不代表安全。' : '工资与考勤项目已纳入本次评估。'}</p>
      <details open={!compact}>
        <summary>评估范围说明</summary>
        {scope.excluded_rule_codes.length ? <p>工资表核对、考勤及相关计算项目不在本次评估范围。</p> : null}
        {limitations.map((reason, index) => <p key={index}>{reason}</p>)}
        <p>离职结算项按{scope.settlement_document_label}。</p>
      </details>
    </section>
  );
}
