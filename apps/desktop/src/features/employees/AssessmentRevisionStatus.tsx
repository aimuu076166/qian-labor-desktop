import type { AssessmentRevision } from '../../lib/api';

function shortRevision(value: string | null) {
  return value ? value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value : '尚无';
}

export function AssessmentRevisionStatus({ revision }: { revision?: AssessmentRevision | null }) {
  if (!revision) return null;
  const availability = revision.availability === 'none' ? '无可用事实' : '已有可用事实';
  const completeness = revision.completeness === 'complete' ? '材料处理完成（不代表资料齐全）'
    : revision.completeness === 'partial' ? '部分可用，仍有材料或来源待核对' : '材料仍待补充或确认';
  return <section className={`assessment-revision ${revision.fresh ? 'is-fresh' : 'is-stale'}`} aria-label="当前结果版本">
    <strong>{revision.fresh ? '当前结果与已保存事实一致' : '结果待重新评估'}</strong>
    <span>{availability} · {completeness}</span>
    <span>核查日期：{revision.check_date} · {revision.check_date_explicit ? '已设置核查日期' : '核查日期来自历史创建日期回退'}</span>
    <span>结果版本：{shortRevision(revision.result_revision)} · 输入版本：{shortRevision(revision.input_revision)}</span>
  </section>;
}
