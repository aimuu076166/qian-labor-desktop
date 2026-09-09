import type { AnalysisTask } from './useAnalysisTask';
import { describeOperationError } from '../../lib/errorMessages';

type ProcessingPanelProps = {
  status: string;
  progress: number;
  task?: AnalysisTask;
  files?: Array<{ id: string; filename: string; status: string; progress?: number; error_code?: string | null }>;
  onResume?: () => void;
  onRetry?: () => void;
  onMatching?: () => void;
};

const STATUS_LABELS: Record<string, string> = {
  created: '正在创建体检任务',
  uploading: '正在导入企业材料',
  uploaded: '材料导入完成',
  queued: '已进入本机分析队列',
  parsing: '正在解析文件内容',
  extracting: '正在提取用工事实',
  evaluating: '正在执行用工风险规则',
  matching_review: '员工归属需要人工确认',
  completed: '分析完成',
  partial: '部分材料处理完成',
  failed: '分析未完成',
  cancelled: '任务已取消', interrupted: '任务已中断',
};

export const TASK_LABELS: Record<string, string> = { queued: '任务等待执行', running: '任务正在处理',
  cancel_requested: '已请求取消，正在等待当前调用结束', cancelled: '任务已取消', interrupted: '任务已中断',
  completed: '本次处理已结束', partial: '本次处理部分完成', failed: '本次处理未完成' };

export function TaskControls({ task, files, onResume, onRetry, onMatching }: Pick<ProcessingPanelProps, 'files' | 'onResume' | 'onRetry' | 'onMatching'> & { task: AnalysisTask }) {
  const preview = task.preview;
  const names = new Map((task.previewFiles ?? files)?.map(file => [file.id, file.filename]));
  const ids = preview ? [...preview.resume_preview.reusable_file_ids, ...preview.resume_preview.extraction_file_ids] : [];
  const namesReady = ids.every(id => names.has(id));
  return <section className="task-controls" aria-label="任务控制">
    {task.run ? <p role="status">{TASK_LABELS[task.run.state]}</p> : <p>{task.data ? '尚无已启动任务。' : '任务状态尚未核实。'}</p>}
    {task.run?.in_flight ? <p>当前有已发出的模型调用，正在等待返回。</p> : null}
    {files ? <>
      <p>已完成 {files.filter(file => file.status === 'processed').length} 份，部分完成 {files.filter(file => file.status === 'partial').length} 份，失败 {files.filter(file => file.status === 'failed').length} 份；已有材料与已保存结果保留，未完成内容仍待处理。</p>
      {files.find(file => ['parsing', 'extracting'].includes(file.status)) ? (() => {
        const current = files.find(file => ['parsing', 'extracting'].includes(file.status))!;
        return <p role="status">当前材料：{current.filename} · {current.status === 'parsing' ? '正在解析' : '正在提取'}{typeof current.progress === 'number' ? `（${Math.round(current.progress)}%）` : ''}</p>;
      })() : null}
    </> : <p>材料清单尚待读取，已有材料与已保存结果保留。</p>}
    <p>已经发出的模型请求可能继续消耗额度；取消确认不代表远端请求已停止，也不代表零费用。</p>
    {task.error ? <p role="alert">{describeOperationError(task.error)}</p> : null}
    {task.pending ? <p role="alert">操作结果尚未核实，不会重复提交。<button type="button" disabled={task.busy} onClick={task.reconcile}>核对处理状态</button></p> : null}
    <div className="dashboard-actions">
      <button type="button" disabled={task.busy} onClick={task.read}>{!task.data && task.error ? '重试读取任务' : '刷新任务状态'}</button>
      {task.pending ? <button type="button" disabled={task.busy} onClick={task.prepareRetry}>重试原请求</button> : null}
      {task.canCancel && !task.data?.read_only ? <button type="button" disabled={task.locked} onClick={() => task.command('cancel')}>取消分析</button> : null}
      {task.canResume && !task.data?.read_only ? <button type="button" disabled={task.locked} onClick={task.showPreview}>恢复分析</button> : null}
      {task.data?.business_status === 'matching_review' && !task.active && onMatching ? <button type="button" onClick={onMatching}>确认员工归属</button> : null}
    </div>
    {task.confirmation ? <section aria-label={task.confirmation === 'retry' ? '原请求重试确认' : '恢复分析确认'}>
      <h3>{task.confirmation === 'retry' ? `重试原${{ start: '开始分析', resume: '恢复分析', cancel: '取消分析' }[task.pending!.operation]}请求` : '确认恢复本次分析'}</h3>
      {task.confirmation === 'retry' ? <p>{task.pending?.operation === 'cancel' ? '按原请求编号与原版本重试取消；不会开始新分析。' : '按原请求编号与原版本重试；如果此前未被接受，本次重试可能开始处理并消耗模型额度。已接受的请求会按原编号核对。'}</p> : null}
      {preview ? <><p>可复用 {preview.resume_preview.reusable_file_ids.length} 份：{preview.resume_preview.reusable_file_ids.map(id => names.get(id) ?? '材料名称待读取').join('、') || '无'}</p>
      <p>需重新提取 {preview.resume_preview.extraction_file_ids.length} 份（未完成或缓存已过期）：{preview.resume_preview.extraction_file_ids.map(id => names.get(id) ?? '材料名称待读取').join('、') || '无'}</p>
      <p>摘要基于当前缓存。重新提取可能再次消耗模型额度，并不保证成功；恢复前已完成的材料不会删除。</p>
      {!namesReady ? <p role="alert">请先重试读取材料清单，取得文件名称后才能确认恢复。</p> : null}</> : null}
      <button type="button" disabled={(task.confirmation === 'retry' ? task.busy : task.locked) || !namesReady} onClick={task.confirmation === 'retry' ? onRetry : onResume}>{task.confirmation === 'retry' ? '确认重试原请求' : '确认恢复分析'}</button>
      <button type="button" onClick={task.hidePreview}>{task.confirmation === 'retry' ? '暂不重试' : '暂不恢复'}</button>
    </section> : null}
  </section>;
}

export function ProcessingPanel({ status, progress, task, files, onResume, onRetry, onMatching }: ProcessingPanelProps) {
  const boundedProgress = Math.max(0, Math.min(100, Math.round(progress)));
  return (
    <section className="processing-panel" aria-labelledby="processing-title">
      <p className="eyebrow">本机分析</p>
      <h2 id="processing-title">{task && !task.active ? '材料处理状态' : '正在分析企业材料'}</h2>
      <p className="processing-stage">{STATUS_LABELS[status] ?? '正在处理企业材料'}</p>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={boundedProgress}
        aria-label="分析进度"
      >
        <span className="progress-value" style={{ width: `${boundedProgress}%` }} />
      </div>
      <strong>{boundedProgress}%</strong>
      <p className="muted">原始材料和结构化台账默认保存在本机。</p>
      {task ? <TaskControls task={task} files={files} onResume={onResume} onRetry={onRetry} onMatching={onMatching} /> : null}
    </section>
  );
}
