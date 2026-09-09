import { useEffect, useRef } from 'react';
import type { HistoricalActions } from './useHistoricalActions';

const COPY_ERRORS: Record<string, string> = {
  HISTORICAL_STORAGE_UNSAFE: '原材料存储状态不安全，无法复制', HISTORICAL_STORAGE_UNAVAILABLE: '原材料存储暂不可用',
  HISTORICAL_SIZE_INVALID: '原材料大小不符合要求', HISTORICAL_FILE_MISSING: '原材料文件缺失',
  HISTORICAL_CHECKSUM_MISMATCH: '原材料完整性校验失败', HISTORICAL_UPLOAD_INVALID: '材料格式或内容不符合导入要求',
  HISTORICAL_BATCH_SIZE_LIMIT: '当前材料总大小已达上限', HISTORICAL_BATCH_FILE_LIMIT: '当前材料数量已达上限', HISTORICAL_COPY_FAILED: '该材料复制失败',
};
export function HistoryCopyPanel({ actions: a, onCurrent }: { actions: HistoricalActions; onCurrent: () => void }) {
  const copy = a.copy;
  if (!copy) return null;
  return <section aria-label="复用历史材料">
    <h3>从{copy.analysis.name}选择材料</h3>
    <p>仅复制明确选择的原始文件，不复制旧结论，不会自动分析。手动开始重新分析可能消耗模型服务额度。</p>
    {copy.busy ? <p>正在处理材料请求…</p> : null}
    {copy.error ? <p role="alert">{copy.error === 'WORKSPACE_CURRENT_CONFLICT' ? '当前材料已被其他操作建立或改变；本次没有继续复制，请核对当前材料后明确重试。' : '材料操作未完成。'}（{copy.error}）</p> : null}
    {copy.notice ? <p role="status">{copy.notice}</p> : null}
    {copy.files.map(file => <label key={file.id}><input type="checkbox" aria-label={`选择${file.filename}`} checked={copy.selection.includes(file.id)}
      disabled={copy.busy || copy.uncertain} onChange={() => a.select(file.id)} />{file.filename} · {file.status}</label>)}
    {copy.loaded && !copy.files.length ? <p>该历史记录没有可复用的材料。</p> : null}
    {copy.selection.length > 100 ? <p role="alert">每次最多选择100份材料。</p> : null}
    {copy.response ? <><table aria-label="材料复用结果"><thead><tr><th>原材料</th><th>结果</th><th>说明</th></tr></thead>
      <tbody>{copy.response.results.map(result => <tr key={result.source_file_id}>
        <td>{copy.files.find(file => file.id === result.source_file_id)?.filename ?? '所选材料'}</td>
        <td>{{ imported: '已复制', duplicate: '已存在', error: '复制失败' }[result.status]}</td>
        <td>{result.error_code ? `${COPY_ERRORS[result.error_code] ?? '材料复制未完成'}（${result.error_code}）` : '—'}</td>
      </tr>)}</tbody></table>
      <p>材料复用已返回结果；如需重新分析，请到当前材料手动开始，可能消耗模型服务额度。</p>
      <button type="button" onClick={onCurrent}>前往当前材料</button></> : null}
    {!copy.loaded && !copy.busy ? <button type="button" onClick={() => a.openCopy(copy.analysis)}>重试读取历史材料</button> : null}
    {copy.uncertain ? <button type="button" disabled={copy.busy} onClick={a.reconcileCopy}>核对复用结果</button> : null}
    <button type="button" disabled={copy.busy || copy.uncertain || !copy.loaded || !copy.selection.length || copy.selection.length > 100} onClick={a.submitCopy}>确认复用所选材料</button>
    <button type="button" disabled={copy.busy || copy.uncertain} onClick={a.cancelCopy}>取消复用</button>
  </section>;
}
export function HistoryDeleteDialog({ actions: a }: { actions: HistoricalActions }) {
  const target = a.deletion;
  const cancel = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const origin = document.activeElement as HTMLElement | null;
    const element = panel.current;
    cancel.current?.focus();
    return () => { if (origin?.isConnected && (element?.contains(document.activeElement) || document.activeElement === document.body)) origin.focus(); };
  }, []);
  if (!target) return null;
  return <div ref={panel} role="dialog" aria-modal="true" aria-labelledby="delete-history-title" onKeyDown={event => {
    if (event.key === 'Escape' && !target.busy) a.cancelDelete();
    if (event.key === 'Tab') {
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }}>
    <h3 id="delete-history-title">确认删除本次体检</h3>
    <p>{target.analysis.name} · {target.analysis.created_at.replace('T', ' ').slice(0, 19)}</p>
    <p>将删除这次历史分析、材料及结果；企业员工档案保留，已经复用到当前材料的独立副本保留。此操作不可撤销。</p>
    {target.error ? <p role="alert">删除结果待核对（{target.error}）。</p> : null}
    {target.notice ? <p role="status">{target.notice}</p> : null}
    <button ref={cancel} type="button" disabled={target.busy || target.uncertain} onClick={a.cancelDelete}>取消删除</button>
    {target.uncertain ? <button type="button" disabled={target.busy} onClick={a.reconcileDelete}>核对删除结果</button> : null}
    <button type="button" disabled={target.busy || target.uncertain} onClick={a.confirmDelete}>确认删除</button>
  </div>;
}
