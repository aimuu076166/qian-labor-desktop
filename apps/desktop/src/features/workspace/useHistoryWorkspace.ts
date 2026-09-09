import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { readJson, safeErrorCode, type AssessmentScope } from '../../lib/api';
import type { EmployeeLedgerItem, EmployeeLedgerPayload } from '../employees/EmployeeLedger';
import type { Company, DesktopRequest, EmployeeRecord, SnapshotBinding } from './useCompanyWorkspace';

export type HistoryRelation = 'historical' | 'unbound';
export type HistoricalAnalysis = { id: string; name: string; company_display_name: string; status: string;
  file_count: number; employee_count: number; created_at: string; assessment_scope?: AssessmentScope;
  relation: HistoryRelation; company_id: string | null; can_adopt: boolean; adoption_blocker_code: string | null };
export type HistoryPage = { items: HistoricalAnalysis[]; page: number; pages: number; total: number; page_size: number };
export type AnalysisBinding = { company_id: string; analysis_id: string; bound: boolean; role: 'historical' | 'current' | null;
  company_version: number; analysis_version: number; bindings: SnapshotBinding[]; excluded_merged_snapshot_ids: string[] };
export type AdoptionChoice = { action: '' | 'link' | 'create'; selected?: EmployeeRecord;
  record: { id: string; display_name: string; employee_number?: string; department?: string; job_title?: string } };
export type AdoptionDraft = { companyId: string; analysis: HistoricalAnalysis; binding?: AnalysisBinding;
  snapshots: EmployeeLedgerItem[]; choices: Record<string, AdoptionChoice>; busy: boolean;
  needsReconcile: boolean; ready: boolean; error: string | null; notice: string | null };
type EmployeePage = { items: EmployeeRecord[]; page: number; pages: number; total: number; page_size: number };

// Mounted once by App: navigating to Settings does not discard an adoption decision or caller UUID.
export function useHistoryWorkspace(api: DesktopRequest | null, company: Company | null | undefined, active: boolean) {
  const client = useQueryClient();
  const companyId = company?.id ?? null;
  const [relation, setRelationState] = useState<HistoryRelation>('historical');
  const [pages, setPages] = useState({ historical: 1, unbound: 1 });
  const [draft, setDraft] = useState<AdoptionDraft | null>(null);
  const [selected, setSelected] = useState<HistoricalAnalysis | null>(null);
  const [search, setSearchState] = useState('');
  const [employeePage, setEmployeePage] = useState(1);
  const epoch = useRef(0);
  const mutation = useRef(false);
  useEffect(() => {
    epoch.current += 1; setDraft(null); setSelected(null); setPages({ historical: 1, unbound: 1 });
    setRelationState('historical'); setSearchState(''); setEmployeePage(1);
    return () => { epoch.current += 1; };
  }, [companyId]);
  const page = pages[relation];
  const query = useQuery<HistoryPage>({ queryKey: ['company-history', companyId, relation, page],
    enabled: Boolean(api && companyId && active), retry: false,
    queryFn: ({ signal }) => readJson<HistoryPage>(api!(`/api/company-workspaces/${companyId}/analyses?relation=${relation}&page=${page}&page_size=25`, { signal })) });
  useEffect(() => {
    if (query.data && page > Math.max(1, query.data.pages)) setPages(old => ({ ...old, [relation]: Math.max(1, query.data!.pages) }));
  }, [query.data, page, relation]);
  const shownDraft = draft?.companyId === companyId ? draft : null;
  const options = useQuery<EmployeePage>({ queryKey: ['history-employee-options', companyId, search, employeePage],
    enabled: Boolean(api && companyId && active && shownDraft && !shownDraft.binding?.bound), retry: false,
    queryFn: ({ signal }) => readJson<EmployeePage>(api!(`/api/company-workspaces/${companyId}/employees?${new URLSearchParams({ search, page: String(employeePage), page_size: '25' })}`, { signal })) });
  async function refresh(id: string) {
    await Promise.all([client.invalidateQueries({ queryKey: ['company-history', id] }),
      client.invalidateQueries({ queryKey: ['company-current', id] }), client.invalidateQueries({ queryKey: ['current-dashboard'] }),
      client.invalidateQueries({ queryKey: ['history-employee-options', id] })]);
  }
  async function load(target: AdoptionDraft, reconciliation: boolean) {
    if (!api || mutation.current) return;
    const ticket = ++epoch.current;
    setDraft({ ...target, busy: true, ready: false, error: null });
    try {
      const binding = await readJson<AnalysisBinding>(api(`/api/company-workspaces/${target.companyId}/analyses/${target.analysis.id}/binding`));
      if (ticket !== epoch.current) return;
      if (binding.company_id !== target.companyId || binding.analysis_id !== target.analysis.id || binding.role === 'current') throw new Error('WORKSPACE_ANALYSIS_ALREADY_BOUND');
      if (binding.bound) {
        setDraft({ ...target, binding, busy: false, ready: true, needsReconcile: false, error: null, notice: null });
        void refresh(target.companyId); return;
      }
      const snapshots: EmployeeLedgerItem[] = [];
      let expectedTotal: number | undefined;
      let totalPages = 1;
      for (let next = 1; next <= totalPages; next++) {
        const payload = await readJson<EmployeeLedgerPayload>(api(`/api/analyses/${target.analysis.id}/employees?page=${next}&page_size=100`));
        if (ticket !== epoch.current) return;
        if (payload.page !== next || !Number.isInteger(payload.pages) || payload.pages < 0 ||
          (expectedTotal !== undefined && (expectedTotal !== payload.total || payload.pages !== totalPages))) throw new Error('WORKSPACE_SNAPSHOT_SET_CHANGED');
        expectedTotal = payload.total; totalPages = payload.pages;
        snapshots.push(...payload.items);
      }
      if (snapshots.length !== expectedTotal || new Set(snapshots.map(item => item.id)).size !== snapshots.length) throw new Error('WORKSPACE_SNAPSHOT_SET_CHANGED');
      const included = snapshots.filter(item => !binding.excluded_merged_snapshot_ids.includes(item.id));
      const choices: Record<string, AdoptionChoice> = {};
      for (const snapshot of included) {
        const prior = target.choices[snapshot.id];
        choices[snapshot.id] = prior ?? { action: '', record: { id: crypto.randomUUID(), display_name: snapshot.masked_name } };
        if (reconciliation && prior?.selected) {
          choices[snapshot.id] = { ...prior, selected: await readJson<EmployeeRecord>(api(`/api/company-workspaces/${target.companyId}/employees/${prior.selected.id}`)) };
        }
      }
      if (ticket !== epoch.current) return;
      setDraft({ ...target, binding, snapshots: included, choices, busy: false, ready: true, needsReconcile: false,
        error: null, notice: reconciliation ? '版本已重新读取；选择和新员工编号已保留，请核对后明确重试。' : null });
      if (reconciliation) void client.invalidateQueries({ queryKey: ['history-employee-options', target.companyId] });
    } catch (reason) {
      if (ticket === epoch.current) setDraft({ ...target, ready: false, busy: false, needsReconcile: true, error: safeErrorCode(reason) });
    }
  }
  function adopt(analysis: HistoricalAnalysis) {
    if (!companyId || !analysis.can_adopt || analysis.relation !== 'unbound' || mutation.current) return;
    void load({ companyId, analysis, snapshots: [], choices: {}, busy: false, ready: false, needsReconcile: false, error: null, notice: null }, false);
  }
  function update(snapshotId: string, choice: AdoptionChoice) {
    setDraft(old => old && !old.busy ? { ...old, choices: { ...old.choices, [snapshotId]: choice } } : old);
  }
  const valid = Boolean(shownDraft?.ready && !shownDraft.needsReconcile && !shownDraft.busy && !shownDraft.binding?.bound &&
    shownDraft.snapshots.every(item => { const c = shownDraft.choices[item.id]; return c && (c.action === 'link' ? Boolean(c.selected) : c.action === 'create' && Boolean(c.record.display_name.trim())); }) &&
    new Set(shownDraft.snapshots.map(item => { const c = shownDraft.choices[item.id]; return c.action === 'link' ? c.selected?.id : c.record.id; })).size === shownDraft.snapshots.length);
  async function submit() {
    if (!api || !shownDraft?.binding || !valid || mutation.current) return;
    const target = shownDraft; const ticket = epoch.current;
    mutation.current = true; setDraft({ ...target, busy: true, error: null });
    const decisions = target.snapshots.map(item => {
      const c = target.choices[item.id];
      return c.action === 'link' ? { action: 'link', snapshot_id: item.id, employee_record_id: c.selected!.id, expected_record_version: c.selected!.version }
        : { action: 'create', snapshot_id: item.id, record: { ...c.record, display_name: c.record.display_name.trim() } };
    });
    try {
      const binding = await readJson<AnalysisBinding>(api(`/api/company-workspaces/${target.companyId}/analyses/${target.analysis.id}/binding`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_company_version: target.binding!.company_version,
          expected_analysis_version: target.binding!.analysis_version, decisions }) }));
      if (!binding.bound || binding.company_id !== target.companyId || binding.analysis_id !== target.analysis.id || binding.role !== 'historical') throw new Error('WORKSPACE_BINDING_UNKNOWN');
      if (ticket === epoch.current) setDraft({ ...target, binding, busy: false, needsReconcile: false, error: null });
      void refresh(target.companyId);
    } catch (reason) {
      if (ticket === epoch.current) setDraft({ ...target, busy: false, needsReconcile: true, error: safeErrorCode(reason), notice: '归属结果尚未核实。先读取已保存状态，再决定是否重试；不会自动重复提交。' });
    } finally { mutation.current = false; }
  }
  return { company, companyId, relation, page, query, draft: shownDraft, selected, setSelected, options, search, employeePage, valid,
    setRelation: (next: HistoryRelation) => setRelationState(next), setPage: (next: number) => setPages(old => ({ ...old, [relation]: next })),
    setSearch: (next: string) => { setSearchState(next); setEmployeePage(1); }, setEmployeePage,
    adopt, update, submit, refresh, reconcile: () => shownDraft && load(shownDraft, true),
    cancel: () => { if (!mutation.current && !shownDraft?.busy) { epoch.current += 1; setDraft(null); } } };
}
export type HistoryController = ReturnType<typeof useHistoryWorkspace>;
