import { useState } from 'react';
import { safeErrorCode } from '../../lib/api';
import { HistoryAdoptionEditor } from './HistoryAdoptionEditor';
import type { HistoricalAnalysis, HistoryController } from './useHistoryWorkspace';
import { permittedHistorical, type HistoricalActions } from './useHistoricalActions';
import { HistoryCopyPanel, HistoryDeleteDialog } from './HistoryActionPanels';

const STATUS_LABELS: Record<string, string> = {
  cancelled: '已取消', interrupted: '已中断，待恢复',
  created: '待添加材料', uploading: '待分析', queued: '等待处理', parsing: '解析中',
  extracting: '提取中', evaluating: '分析中', matching_review: '待确认人员',
  completed: '分析完成', partial: '部分完成', failed: '需要处理失败事项',
};

function needsLegacyMatching(item: HistoricalAnalysis) {
  return item.adoption_blocker_code === 'WORKSPACE_MATCHING_UNRESOLVED' ||
    item.status === 'matching_review' && item.adoption_blocker_code === 'WORKSPACE_ANALYSIS_NOT_SETTLED';
}

export function HistoryView({ controller: h, onOpen, onMatching, onBack, actions, currentId = null, onCurrent = () => {} }: {
  controller: HistoryController;
  onOpen: (analysis: HistoricalAnalysis) => Promise<void>;
  onMatching: (analysis: HistoricalAnalysis) => void;
  onBack: () => void;
  actions?: HistoricalActions;
  currentId?: string | null;
  onCurrent?: () => void;
}) {
  const { query, page } = h;
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function open(analysis: HistoricalAnalysis) {
    if (opening) return;
    setOpening(true); setError(null);
    try { await onOpen(analysis); }
    catch (reason) { setError(safeErrorCode(reason)); }
    finally { setOpening(false); }
  }
  return <section className="dashboard-view" aria-labelledby="history-title">
    <div className="section-heading"><h2 id="history-title">历史体检</h2>
      <button type="button" className="secondary-action" disabled={opening} onClick={onBack}>返回工作区</button>
    </div>
    <p>体检和材料保存在本机；打开历史记录不会重新调用模型。</p>
    {actions?.copy ? <HistoryCopyPanel actions={actions} onCurrent={onCurrent} /> : null}
    {actions?.deletion ? <HistoryDeleteDialog actions={actions} /> : null}
    {!h.companyId ? <p>请先选择企业，再查看历史记录。</p> : <>
    <div role="tablist" aria-label="历史归属范围">
      <button role="tab" aria-selected={h.relation === 'historical'} onClick={() => h.setRelation('historical')}>本企业已归属历史</button>
      <button role="tab" aria-selected={h.relation === 'unbound'} onClick={() => h.setRelation('unbound')}>未归属旧记录</button>
    </div>
    {h.draft ? <HistoryAdoptionEditor controller={h} /> : <>
    {query.isError || error ? <p role="alert">无法读取或打开体检：{error ?? safeErrorCode(query.error)}。已有记录不会因此删除。</p> : null}
    {query.isError ? <button type="button" disabled={query.isFetching || opening} onClick={() => query.refetch()}>重试读取</button> : null}
    {query.isPending ? <p>正在读取历史体检…</p> : null}
    {query.data ? <>
      <div className="table-scroll"><table className="employee-table"><thead><tr>
        <th>体检</th><th>状态</th><th>材料数</th><th>员工数</th><th>创建时间</th><th>操作</th>
      </tr></thead><tbody>{query.data.items.map(item => <tr key={item.id}>
        <td>{item.name}<small>{item.company_display_name}</small></td>
        <td>{STATUS_LABELS[item.status] ?? '状态待核对'}</td><td>{item.file_count}</td><td>{item.employee_count}</td>
        <td>{item.created_at.replace('T', ' ').slice(0, 19)}</td>
        <td><button type="button" className="text-action" disabled={opening} aria-label={`打开${item.name}`} onClick={() => open(item)}>打开体检</button>
          {actions && permittedHistorical(item, h.companyId, currentId) ? <>
            {item.relation === 'historical' ? <button type="button" aria-label={`复用${item.name}材料`} onClick={() => actions.openCopy(item)}>复用所选材料</button> : null}
            {['completed', 'partial', 'failed'].includes(item.status) ? <button type="button" aria-label={`删除${item.name}`} onClick={event => { event.currentTarget.focus(); actions.openDelete(item); }}>删除历史体检</button> : null}
          </> : null}
          {item.relation === 'unbound' && item.can_adopt ? <button type="button" aria-label={`归属${item.name}`} onClick={() => h.adopt(item)}>明确归属到本企业</button> : null}
          {item.relation === 'unbound' && !item.can_adopt ? <p>{needsLegacyMatching(item)
            ? '尚有人员身份未确认，请先完成旧记录人员匹配。' : '记录仍在上传、处理或等待处理完成，暂不能归属。'}
            {needsLegacyMatching(item) ? <button type="button" onClick={() => onMatching(item)}>确认旧记录人员匹配</button> : null}</p> : null}
        </td>
      </tr>)}</tbody></table></div>
      {!query.data.items.length ? <p>没有历史体检记录。</p> : null}
      <div className="dashboard-actions">
        <button type="button" disabled={page <= 1 || opening || query.isFetching} onClick={() => h.setPage(page - 1)}>上一页</button>
        <span>第 {page} / {Math.max(1, query.data.pages)} 页 · 共 {query.data.total} 次体检</span>
        <button type="button" disabled={page >= query.data.pages || opening || query.isFetching} onClick={() => h.setPage(page + 1)}>下一页</button>
      </div>
    </> : null}
    </>}</>}
  </section>;
}
