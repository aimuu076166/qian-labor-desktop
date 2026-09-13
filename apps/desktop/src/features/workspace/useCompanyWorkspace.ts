import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { readJson, safeErrorCode, type AssessmentRevision } from '../../lib/api';

export type DesktopRequest = (path: string, init?: RequestInit) => Promise<Response>;
export type Company = { id: string; display_name: string; version: number; created_at: string };
export type SnapshotBinding = { snapshot_id: string; employee_record_id: string; analysis_id: string; company_id: string; created_at: string };
export type EmployeeRecord = { id: string; company_id: string; masked_name: string; employee_number: string | null;
  department: string | null; job_title: string | null; lifecycle_status: string; version: number; created_at: string };
export type CurrentAnalysis = { analysis_id: string; company_id: string; role: 'current'; assessment_profile: string;
  status: string; current_stage: string; progress: number; analysis_version: number; company_version: number;
  stale: boolean; assessment_revision?: AssessmentRevision; pending_identity_count: number };
export type CurrentEmployee = EmployeeRecord & { current_binding: SnapshotBinding | null; snapshot_employee_id: string | null;
  employment_status: string | null; assessment_state: 'pending_evidence' | 'pending_analysis' | 'evaluated';
  assessment: null | { risk_counts: { high: number; medium: number }; insufficient_data_count: number;
    requires_human_review_count: number; material_coverage: number } };
export type RecordDetail = EmployeeRecord & { bindings: SnapshotBinding[]; current_binding: SnapshotBinding | null; historical_bindings: SnapshotBinding[] };
export type CurrentWorkspace = { company: Company; enrolled_employee_count: number; current_analysis: CurrentAnalysis | null;
  employees: CurrentEmployee[]; pending_identity_count: number; total: number; page: number; page_size: number; pages: number };
export type EmployeeFilters = { search: string; severity: string; assessment_state: string; page: number; page_size: number };
type Preference = { last_company_id: string | null; version: number };
const DEFAULT_FILTERS: EmployeeFilters = { search: '', severity: '', assessment_state: '', page: 1, page_size: 25 };
type Creation = { id: string; display_name: string };
const CREATION_KEY = 'qian-company-creation-v1';
function restoreCreation(): Creation | null {
  const raw = localStorage.getItem(CREATION_KEY);
  if (!raw) return null;
  const value = JSON.parse(raw);
  if (!value || !/^[0-9a-f-]{36}$/i.test(value.id) || typeof value.display_name !== 'string'
    || !value.display_name.trim() || Object.keys(value).length !== 2) throw new Error('WORKSPACE_CREATION_JOURNAL_INVALID');
  return value;
}
export const RUNNING = new Set(['queued', 'parsing', 'extracting', 'evaluating']);
export function useCompanyWorkspace(api: DesktopRequest | null, tab: 'home' | 'employees') {
  const client = useQueryClient();
  const [companies, setCompanies] = useState<Company[]>([]);
  const [companyId, setCompanyId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filtersByContext, setFilters] = useState<Record<string, EmployeeFilters>>({});
  const preference = useRef<Preference>({ last_company_id: null, version: 0 });
  const initialRestorationComplete = useRef(false);
  const explicitCompanyChoice = useRef(false);
  const epoch = useRef(0);
  const pendingCreation = useRef<Creation | null>(null);
  const creationAttempt = useRef(0);
  const creationSending = useRef(false);
  const [creationLocked, setCreationLocked] = useState(false);
  const creationJournalInvalid = useRef(false);
  const liveApi = useRef(api); liveApi.current = api;
  const mounted = useRef(true);
  const lastPages = useRef<Record<string, CurrentWorkspace>>({});
  const context = `${companyId}:${tab}`;
  const filters = filtersByContext[context] ?? DEFAULT_FILTERS;
  async function reloadSetup() {
    if (!api) return;
    const ticket = ++epoch.current;
    try {
      const [list, saved] = await Promise.all([readJson<Company[]>(api('/api/company-workspaces')),
        readJson<Preference>(api('/api/workspace-preference'))]);
      if (ticket !== epoch.current) return;
      setCompanies(list); preference.current = saved;
      if (!initialRestorationComplete.current && !explicitCompanyChoice.current) {
        setCompanyId(list.some(item => item.id === saved.last_company_id) ? saved.last_company_id : null);
      }
      initialRestorationComplete.current = true;
      setLoaded(true); setError(creationJournalInvalid.current ? 'WORKSPACE_CREATION_JOURNAL_INVALID' : pendingCreation.current ? 'WORKSPACE_CREATION_UNKNOWN' : null);
    } catch (cause) { if (ticket === epoch.current) { setError(safeErrorCode(cause)); setLoaded(true); } }
  }
  useEffect(() => {
    if (!api) return;
    mounted.current = true; creationAttempt.current++; creationSending.current = false; setBusy(false);
    try { pendingCreation.current = restoreCreation(); creationJournalInvalid.current = false; setCreationLocked(Boolean(pendingCreation.current)); }
    catch { creationJournalInvalid.current = true; setCreationLocked(true); setError('WORKSPACE_CREATION_JOURNAL_INVALID'); }
    void reloadSetup();
    return () => { mounted.current = false; epoch.current += 1; creationAttempt.current++; };
    // The authenticated API instance is the boot boundary.
  }, [api]);
  const params = new URLSearchParams({ search: filters.search, page: String(filters.page), page_size: String(filters.page_size) });
  if (filters.severity) params.set('severity', filters.severity);
  if (filters.assessment_state) params.set('assessment_state', filters.assessment_state);
  const current = useQuery<CurrentWorkspace>({ queryKey: ['company-current', companyId, params.toString()], enabled: Boolean(api && companyId),
    placeholderData: previous => previous?.company.id === companyId ? previous : undefined,
    queryFn: ({ signal }) => readJson<CurrentWorkspace>(api!(`/api/company-workspaces/${companyId}/current?${params}`, { signal })),
    retry: false, refetchInterval: query => query.state.error ? false : RUNNING.has(query.state.data?.current_analysis?.status ?? '') ? 1000 : false });
  useEffect(() => { if (current.data && !current.isPlaceholderData) lastPages.current[current.data.company.id] = current.data; }, [current.data, current.isPlaceholderData]);
  const displayed = current.data ?? (companyId ? lastPages.current[companyId] : undefined);
  async function select(id: string | null) {
    if (!api || busy) return;
    explicitCompanyChoice.current = true;
    const ticket = ++epoch.current;
    setCompanyId(id); setError(null); setBusy(true);
    try {
      const saved = await readJson<Preference>(api('/api/workspace-preference', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ last_company_id: id, expected_version: preference.current.version }) }));
      if (ticket === epoch.current) preference.current = saved;
    } catch {
      try {
        const saved = await readJson<Preference>(api('/api/workspace-preference'));
        if (ticket === epoch.current) { preference.current = saved; if (saved.last_company_id !== id) setError('WORKSPACE_PREFERENCE_NOT_SAVED'); }
      } catch (cause) { if (ticket === epoch.current) setError(safeErrorCode(cause)); }
    } finally { if (ticket === epoch.current) setBusy(false); }
  }
  async function creationOperation(send: boolean, fresh?: Creation) {
    if (!api || busy || creationSending.current) return;
    const source = api, ticket = ++creationAttempt.current, selection = ++epoch.current;
    const active = () => mounted.current && liveApi.current === source && creationAttempt.current === ticket;
    let target: Creation | null = null;
    creationSending.current = true; setBusy(true);
    try {
      const stored = restoreCreation();
      if (fresh && stored) throw new Error('WORKSPACE_CREATION_JOURNAL_INVALID');
      target = fresh ?? stored;
      if (!target) throw new Error('WORKSPACE_CREATION_JOURNAL_INVALID');
      if (fresh) localStorage.setItem(CREATION_KEY, JSON.stringify(target));
      pendingCreation.current = target; setCreationLocked(true); setError('WORKSPACE_CREATION_UNKNOWN');
      const owns = () => active() && JSON.stringify(restoreCreation()) === JSON.stringify(target);
      let created: Company | undefined;
      if (send) {
        try {
          const response = await source('/api/company-workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(target) });
          // Only the first original request's schema rejection proves no send was accepted.
          if (fresh && response.status === 422 && owns()) {
            localStorage.removeItem(CREATION_KEY); pendingCreation.current = null; setCreationLocked(false);
            setError('WORKSPACE_CREATION_REJECTED'); return;
          }
          created = await readJson<Company>(Promise.resolve(response));
        } catch { /* Timeout, auth and duplicate-UUID conflict do not retire the original request. */ }
      }
      if (!owns()) return;
      if (!created) {
        const list = await readJson<Company[]>(source('/api/company-workspaces'));
        if (!owns()) return;
        setCompanies(list); created = list.find(item => item.id === target!.id);
      }
      if (!created) return; // A negative read is never an authoritative rejection.
      if (created.id !== target.id || typeof created.display_name !== 'string' || !created.display_name.trim()) throw new Error('WORKSPACE_CREATION_UNKNOWN');
      // The schema masks names. Confirm the server's canonical value by UUID,
      // never compare it to raw retry input or duplicate privacy rules here.
      const confirmed = await readJson<Company>(source(`/api/company-workspaces/${encodeURIComponent(target.id)}`));
      if (!owns()) return;
      if (confirmed.id !== target.id || confirmed.display_name !== created.display_name) throw new Error('WORKSPACE_CREATION_UNKNOWN');
      created = confirmed;
      if (!owns()) return;
      localStorage.removeItem(CREATION_KEY); pendingCreation.current = null; setCreationLocked(false); setError(null);
      setCompanies(old => [...old.filter(item => item.id !== created!.id), created!]);
      if (selection !== epoch.current) return;
      explicitCompanyChoice.current = true; setCompanyId(created.id);
      try {
        const saved = await readJson<Preference>(source('/api/workspace-preference', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ last_company_id: created.id, expected_version: preference.current.version }) }));
        if (active() && selection === epoch.current) preference.current = saved;
      } catch { if (active() && selection === epoch.current) setError('WORKSPACE_PREFERENCE_NOT_SAVED'); }
    } catch (cause) { if (active()) { setCreationLocked(true); setError(target && pendingCreation.current ? safeErrorCode(cause) : 'WORKSPACE_CREATION_JOURNAL_INVALID'); } }
    finally { if (active()) { creationSending.current = false; setBusy(false); } }
  }
  async function create(name: string) {
    if (!name.trim() || creationLocked || pendingCreation.current) return;
    explicitCompanyChoice.current = true;
    await creationOperation(true, { id: crypto.randomUUID(), display_name: name.trim() });
  }
  const reconcileCreation = () => creationOperation(false);
  const retryCreation = () => creationOperation(true);
  return { companies, companyId, company: current.data?.company ?? companies.find(item => item.id === companyId), loaded, error, busy,
    current: { ...current, data: displayed }, filters, select, create, reloadSetup, reconcileCreation, retryCreation, uncertainCreation: creationLocked,
    setFilters: (patch: Partial<EmployeeFilters>) => setFilters(old => ({ ...old, [context]: { ...filters, ...patch } })),
    refresh: () => client.invalidateQueries({ queryKey: ['company-current', companyId] }) };
}
