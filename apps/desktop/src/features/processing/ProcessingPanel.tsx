type ProcessingPanelProps = {
  status: string;
  progress: number;
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
};

export function ProcessingPanel({ status, progress }: ProcessingPanelProps) {
  const boundedProgress = Math.max(0, Math.min(100, Math.round(progress)));
  return (
    <section className="processing-panel" aria-labelledby="processing-title">
      <p className="eyebrow">本机分析</p>
      <h2 id="processing-title">正在分析企业材料</h2>
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
    </section>
  );
}
