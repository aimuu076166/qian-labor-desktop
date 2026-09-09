import { describeOperationError } from '../../lib/errorMessages';
import type { AssessmentScope } from '../../lib/api';
import type { ReactNode } from 'react';
import { importError, type ImportOutcome } from './useNativeImport';

export type WorkspacePayload = {
  analysis: { id: string; name: string; company_display_name: string; status: string; created_at?: string; assessment_scope?: AssessmentScope };
  files: Array<{ id: string; filename: string; status: string; progress: number;
    detected_kind: string; classified_kind: string; error_code: string | null; size_bytes: number; fact_count?: number;
    warnings?: string[]; needs_reextraction?: boolean; extraction_version?: string | null }>;
};

const FILE_STATUS: Record<string, string> = { uploaded: '待分析', parsing: '正在解析',
  extracting: '正在提取', processed: '已处理', partial: '部分处理', failed: '处理失败', interrupted: '已中断，待恢复' };

export function ImportResults({ results }: { results: ImportOutcome[] }) {
  const imported = results.filter(r => r.status === 'imported').length;
  const duplicate = results.filter(r => r.status === 'duplicate').length;
  const failed = results.filter(r => r.status === 'error').length;
  return <section aria-label="本次导入结果">
    <h3>本次导入结果</h3>
    <p>{failed === results.length ? '未导入新材料，' + failed + ' 项失败。'
      : '新导入 ' + imported + ' 项，重复 ' + duplicate + ' 项，失败 ' + failed + ' 项。'}</p>
    <ol>{results.map(r => <li key={r.index}><span>{r.filename}</span>：{r.status === 'error'
      ? importError(r.error_code) : r.status === 'duplicate' ? '已有相同材料，已保留' : '已导入'}</li>)}</ol>
    <p>已成功导入的材料会保留。需要分析时，请明确点击“开始分析”。</p>
  </section>;
}

export function MaterialWorkspace({ payload, configured, busy, error, onAdd, onProcess, onBack, readOnly = false, importResults, onSelectAdvisory, advisoryPanel, taskPanel, processDisabled = false, processing = false }:
  { payload: WorkspacePayload; configured: boolean; busy: boolean; error?: string | null;
    readOnly?: boolean; importResults?: ImportOutcome[];
    onSelectAdvisory?: (fileId: string) => void; advisoryPanel?: ReactNode;
    taskPanel?: ReactNode; processDisabled?: boolean; processing?: boolean;
    onAdd: () => void; onProcess: () => void; onBack: () => void }) {
  return <section className="dashboard-view" aria-labelledby="materials-title">
    <div className="section-heading"><div><p className="eyebrow">{readOnly ? '历史体检材料' : '当前体检材料'}</p>
      <h2 id="materials-title">{payload.analysis.name}</h2>
      <p>{payload.analysis.company_display_name} · 共 {payload.files.length} 份材料</p></div>
      <button type="button" className="text-action" onClick={onBack}>返回风险概览</button>
    </div>
    {!readOnly ? <><p className="muted">开始分析后，需要模型提取的材料将在本地脱敏后发送至你配置的智谱官方通道；脱敏可能存在遗漏，AI 输出仍需人工复核。</p>
      <p className="muted">仅处理尚未完成提取、没有有效输出或需要更新来源核验及条款观察的材料，可能消耗所选通道额度；已有材料和结果会保留，新提取可能改变判断。</p></> : null}
    {!readOnly ? <p className="muted">模型未执行或无法读取的条款，可明确开始分析重试；会重新发送该文件的脱敏内容（包括已成功部分），可能再次消耗额度。无法读取的内嵌图片会明确显示为部分材料，不会被静默忽略。</p> : null}
    {payload.files.some(file => file.needs_reextraction) ? <p role="status">部分旧版材料尚未完成当前来源核验；明确开始分析后才会重新提取。</p> : null}
    {error ? <p role="alert">{describeOperationError(error)}</p> : null}
    {importResults?.length ? <ImportResults results={importResults} /> : null}
    <div className="table-scroll"><table className="employee-table"><thead><tr>
      <th>材料</th><th>状态</th><th>识别类型</th><th>已提取事实</th><th>处理信息</th>
    </tr></thead><tbody>{payload.files.map(file => <tr key={file.id}>
      <td>{file.filename}</td><td>{FILE_STATUS[file.status] ?? file.status}</td>
      <td>{file.classified_kind === 'unknown' ? '待识别' : file.classified_kind}</td>
      <td>{file.fact_count ?? '—'}</td>
      <td>{file.error_code ? describeOperationError(file.error_code) : '—'}
        {onSelectAdvisory ? <button type="button" onClick={() => onSelectAdvisory(file.id)}>查看 {file.filename} 条款观察</button> : null}
        {file.warnings?.includes('embedded_images_need_vision') ? <p>内嵌图片尚未提取，请将图片单独导入后分析；已提取文字仍保留。</p> : null}
        {file.warnings?.includes('empty_csv') ? <p>表格没有可提取的内容，请核对原材料。</p> : null}
      </td>
    </tr>)}</tbody></table></div>
    {!payload.files.length ? <p>尚未添加材料。</p> : null}
    {advisoryPanel}
    {taskPanel}
    {!readOnly ? <div className="dashboard-actions">
      <button type="button" className="secondary-action" onClick={onAdd} disabled={busy}>添加材料</button>
      <button type="button" className="primary-action" onClick={onProcess} disabled={busy || processDisabled || !payload.files.length}>
        {processing ? '正在处理…' : configured ? '开始分析' : '配置模型后分析'}
      </button>
    </div> : <p>历史材料只读；复用需要明确选择并导入当前材料。</p>}
  </section>;
}
