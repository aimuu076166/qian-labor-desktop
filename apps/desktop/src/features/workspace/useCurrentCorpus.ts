import { useRef, useState } from 'react';
import { readJson } from '../../lib/api';
import type { Company, CurrentAnalysis, DesktopRequest } from './useCompanyWorkspace';

type Pending = { companyId: string; id: string; version: number; uncertain: boolean };
// One caller UUID per unresolved company creation, shared by native import and historical reuse.
export function useCurrentCorpus(api: DesktopRequest | null) {
  const pending = useRef(new Map<string, Pending>());
  const locked = useRef(false);
  const [, render] = useState(0);
  const changed = () => render(value => value + 1);
  async function read(companyId: string) {
    const current = await readJson<CurrentAnalysis | null>(api!(`/api/company-workspaces/${companyId}/current-analysis`));
    if (current && (current.company_id !== companyId || current.role !== 'current')) throw new Error('WORKSPACE_CURRENT_CONFLICT');
    return current;
  }
  async function refreshVersion(target: Pending) {
    target.uncertain = true; changed();
    const company = await readJson<Company>(api!(`/api/company-workspaces/${target.companyId}`));
    if (company.id !== target.companyId || !Number.isInteger(company.version) || company.version < 0) throw new Error('WORKSPACE_CURRENT_CONFLICT');
    target.version = company.version;
    target.uncertain = false; changed();
  }
  async function reconcile(companyId: string) {
    if (!api || locked.current) throw new Error('WORKSPACE_CURRENT_RECONCILIATION_REQUIRED');
    locked.current = true;
    try {
      const target = pending.current.get(companyId);
      const current = await read(companyId);
      if (target && current && current.analysis_id !== target.id) {
        pending.current.delete(companyId); changed(); throw new Error('WORKSPACE_CURRENT_CONFLICT');
      }
      if (current) pending.current.delete(companyId);
      else if (target) await refreshVersion(target);
      changed(); return current;
    } finally { locked.current = false; }
  }
  async function resolve(company: Company) {
    if (!api || locked.current || pending.current.get(company.id)?.uncertain) throw new Error('WORKSPACE_CURRENT_RECONCILIATION_REQUIRED');
    locked.current = true;
    try {
      const existing = await read(company.id);
      const prior = pending.current.get(company.id);
      if (existing) {
        if (prior && existing.analysis_id !== prior.id) { pending.current.delete(company.id); changed(); throw new Error('WORKSPACE_CURRENT_CONFLICT'); }
        pending.current.delete(company.id); changed(); return existing.analysis_id;
      }
      const target = prior ?? { companyId: company.id, id: crypto.randomUUID(), version: company.version, uncertain: false };
      pending.current.set(company.id, target); target.uncertain = true; changed();
      try {
        const created = await readJson<CurrentAnalysis>(api(`/api/company-workspaces/${company.id}/current-analysis`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: target.id, expected_company_version: target.version }),
        }));
        if (created.company_id !== company.id || created.role !== 'current' || created.analysis_id !== target.id) throw new Error('WORKSPACE_CURRENT_CONFLICT');
      } catch (cause) {
        const saved = await read(company.id);
        if (saved?.analysis_id !== target.id) {
          if (saved) { pending.current.delete(company.id); changed(); throw new Error('WORKSPACE_CURRENT_CONFLICT'); }
          // Empty current GET plus authoritative company GET refreshes CAS while retaining the caller UUID.
          // Failure to read either keeps creation locked; success permits only an explicit next POST.
          await refreshVersion(target); throw cause;
        }
      }
      pending.current.delete(company.id); changed(); return target.id;
    } finally { locked.current = false; }
  }
  return { resolve, reconcile, pending: (companyId: string | null) => companyId ? pending.current.get(companyId) : undefined };
}
export type CurrentCorpus = ReturnType<typeof useCurrentCorpus>;
