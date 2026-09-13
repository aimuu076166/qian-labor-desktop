import type { HistoryController } from './useHistoryWorkspace';

export function HistoryAdoptionEditor({ controller: h }: { controller: HistoryController }) {
  const draft = h.draft;
  if (!draft) return null;
  if (draft.binding?.bound) return <section><h3>已归属到{h.company?.display_name}</h3>
    <p>{draft.analysis.name}仍保留原体检日期、范围和结论，不会作为当前事实。</p>
    <button type="button" onClick={h.cancel}>返回历史列表</button></section>;
  return <section aria-label="历史记录归属"><h3>将{draft.analysis.name}归属到{h.company?.display_name}</h3>
    <p>每名历史员工均需明确选择已有档案或新建档案；同名不表示同一人。</p>
    {draft.error ? <p role="alert">归属读取或保存未完成：{draft.error}。已填选择仍保留。</p> : null}
    {draft.notice ? <p role="status">{draft.notice}</p> : null}
    {draft.needsReconcile ? <button type="button" disabled={draft.busy} onClick={() => h.reconcile()}>核对已保存的归属</button> : null}
    {draft.busy ? <p>正在读取或保存归属…</p> : null}
    <label>搜索企业员工<input aria-label="搜索企业员工" value={h.search} disabled={draft.busy} onChange={e => h.setSearch(e.target.value)} /></label>
    {h.options.isError ? <p role="alert">员工选项读取失败。<button type="button" onClick={() => h.options.refetch()}>重试读取员工选项</button></p> : null}
    {h.options.data ? <p><button type="button" disabled={h.employeePage <= 1 || h.options.isFetching || draft.busy} onClick={() => h.setEmployeePage(h.employeePage - 1)}>员工上一页</button>
      员工第 {h.employeePage} / {Math.max(1, h.options.data.pages)} 页 · 共 {h.options.data.total} 人
      <button type="button" disabled={h.employeePage >= h.options.data.pages || h.options.isFetching || draft.busy} onClick={() => h.setEmployeePage(h.employeePage + 1)}>员工下一页</button></p> : null}
    {draft.snapshots.map(snapshot => {
      const choice = draft.choices[snapshot.id];
      const records = new Map((h.options.data?.items ?? []).map(item => [item.id, item]));
      if (choice.selected && !records.has(choice.selected.id)) records.set(choice.selected.id, choice.selected);
      return <fieldset key={snapshot.id} disabled={draft.busy}><legend>{snapshot.masked_name} · {snapshot.employee_number ?? '未填写工号'} · 历史快照</legend>
        <label>归属方式<select aria-label={`${snapshot.masked_name}的归属方式`} value={choice.action}
          onChange={e => h.update(snapshot.id, { ...choice, action: e.target.value as typeof choice.action })}>
          <option value="">请明确选择</option><option value="link">关联已有员工</option><option value="create">新建员工档案</option></select></label>
        {choice.action === 'link' ? <label>关联员工<select aria-label={`${snapshot.masked_name}关联员工`} value={choice.selected?.id ?? ''}
          onChange={e => h.update(snapshot.id, { ...choice, selected: records.get(e.target.value) })}>
          <option value="">请选择具体档案</option>{[...records.values()].map(record => <option key={record.id} value={record.id}>
            {record.masked_name} · {record.employee_number ?? '无工号'} · {record.department ?? '无部门'} · {record.id}</option>)}</select></label> : null}
        {choice.action === 'create' ? (['display_name', 'employee_number', 'department', 'job_title'] as const).map((key, index) => <label key={key}>
          {['新员工姓名', '工号（可选）', '部门（可选）', '岗位（可选）'][index]}<input aria-label={`${snapshot.masked_name}${['新员工姓名', '工号', '部门', '岗位'][index]}`}
            maxLength={200} value={choice.record[key] ?? ''} onChange={e => h.update(snapshot.id, { ...choice, record: { ...choice.record, [key]: e.target.value } })} /></label>) : null}
      </fieldset>;
    })}
    {!h.valid && draft.ready && !draft.needsReconcile ? <p>请完成每名员工的明确选择；不能将两个快照关联到同一个员工档案。</p> : null}
    <button type="button" disabled={!h.valid} onClick={h.submit}>确认归属到企业</button>
    <button type="button" disabled={draft.busy} onClick={h.cancel}>取消归属</button>
  </section>;
}
