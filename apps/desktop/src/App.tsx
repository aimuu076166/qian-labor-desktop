import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import {
  DashboardView,
  type DashboardFinding,
  type DashboardOverview,
  type DashboardSummary,
} from './features/dashboard/DashboardView';
import {
  EmployeeDetail,
  EmployeeLedger,
  type EmployeeDetailPayload,
  type EmployeeLedgerPayload,
} from './features/employees/EmployeeLedger';
import { FindingDetail, type FindingDetailData } from './features/findings/FindingDetail';
import { ImportPanel } from './features/import/ImportPanel';
import {
  MatchingReview,
  type MatchCandidate,
  type MatchDecisionPayload,
} from './features/matching/MatchingReview';
import { ProcessingPanel } from './features/processing/ProcessingPanel';
import { ReportView, type ReportPayload } from './features/report/ReportView';
import {
  SettingsView,
  type ProviderConfigurationInput,
  type ProviderConfigurationStatus,
} from './features/settings/SettingsView';
import {
  createDesktopApi,
  getDesktopBackendInfo,
  type DesktopBackendInfo,
} from './lib/api';
import {
  configureZhipuProvider,
  getProviderConfigurationStatus,
  markZhipuProviderValidated,
} from './lib/desktop';

type DesktopRequest = (path: string, init?: RequestInit) => Promise<Response>;
type BackendLoader = () => Promise<DesktopBackendInfo>;
type ApiFactory = (info: DesktopBackendInfo) => DesktopRequest;
type ConfigurationLoader = () => Promise<ProviderConfigurationStatus>;
type ProviderConfigurator = (
  input: ProviderConfigurationInput,
) => Promise<ProviderConfigurationStatus>;
type ProviderValidator = () => Promise<ProviderConfigurationStatus>;

type DesktopView =
  | { kind: 'booting' }
  | { kind: 'settings' }
  | { kind: 'import' }
  | { kind: 'processing'; analysisId: string }
  | { kind: 'matching'; analysisId: string }
  | { kind: 'dashboard'; analysisId: string }
  | { kind: 'employees'; analysisId: string }
  | { kind: 'employee'; analysisId: string; employeeId: string }
  | { kind: 'report'; analysisId: string }
  | { kind: 'finding'; analysisId: string; findingId: string }
  | { kind: 'error'; code: string };

type ProcessingStatus = {
  analysis_id: string;
  status: string;
  progress: number;
  current_stage: string;
};

type DashboardPayload = {
  summary: DashboardSummary;
  findings: DashboardFinding[];
  overview?: DashboardOverview;
};

type MatchingPayload = {
  analysis_id: string;
  candidates: MatchCandidate[];
};

type AppProps = {
  backendLoader?: BackendLoader;
  apiFactory?: ApiFactory;
  configurationLoader?: ConfigurationLoader;
  providerConfigurator?: ProviderConfigurator;
  providerValidator?: ProviderValidator;
};

const TERMINAL_PROCESSING_STATES = new Set([
  'completed',
  'partial',
  'matching_review',
  'failed',
]);

async function readJson<T>(request: Promise<Response>): Promise<T> {
  const response = await request;
  if (!response.ok) {
    let code = '';
    try {
      const payload = (await response.json()) as { detail?: { code?: unknown } };
      if (typeof payload.detail?.code === 'string') code = payload.detail.code;
    } catch {
      // The status code remains the safe fallback for malformed error responses.
    }
    throw new Error(
      /^(?:AI|DESKTOP|MATCH)_[A-Z0-9_]+$/.test(code)
        ? code
        : `DESKTOP_API_${response.status}`,
    );
  }
  return (await response.json()) as T;
}

function safeErrorCode(error: unknown): string {
  return error instanceof Error && /^DESKTOP_[A-Z0-9_]+$/.test(error.message)
    ? error.message
    : 'DESKTOP_OPERATION_FAILED';
}

export function App({
  backendLoader = getDesktopBackendInfo,
  apiFactory = createDesktopApi,
  configurationLoader = getProviderConfigurationStatus,
  providerConfigurator = configureZhipuProvider,
  providerValidator = markZhipuProviderValidated,
}: AppProps = {}) {
  const queryClient = useQueryClient();
  const [view, setView] = useState<DesktopView>({ kind: 'booting' });
  const [api, setApi] = useState<DesktopRequest | null>(null);
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [finding, setFinding] = useState<FindingDetailData | null>(null);
  const [employees, setEmployees] = useState<EmployeeLedgerPayload | null>(null);
  const [employee, setEmployee] = useState<EmployeeDetailPayload | null>(null);
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [submittingMatch, setSubmittingMatch] = useState(false);
  const [providerStatus, setProviderStatus] = useState<ProviderConfigurationStatus | null>(null);
  const [savingProvider, setSavingProvider] = useState(false);
  const [providerError, setProviderError] = useState<string | null>(null);
  const dashboardLoadRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([backendLoader(), configurationLoader()])
      .then(([info, configuration]) => {
        if (cancelled) return;
        setApi(() => apiFactory(info));
        setProviderStatus(configuration);
        setView(configuration.validated ? { kind: 'import' } : { kind: 'settings' });
      })
      .catch((error: unknown) => {
        if (!cancelled) setView({ kind: 'error', code: safeErrorCode(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [apiFactory, backendLoader, configurationLoader]);

  const processingAnalysisId = view.kind === 'processing' ? view.analysisId : null;
  const processingQuery = useQuery<ProcessingStatus>({
    queryKey: ['desktop-processing', processingAnalysisId],
    enabled: api !== null && processingAnalysisId !== null,
    queryFn: async () => {
      if (!api || !processingAnalysisId) throw new Error('DESKTOP_BACKEND_NOT_READY');
      return readJson<ProcessingStatus>(
        api(`/api/analyses/${processingAnalysisId}/processing`),
      );
    },
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && TERMINAL_PROCESSING_STATES.has(status) ? false : 250;
    },
    retry: false,
  });

  const matchingAnalysisId = view.kind === 'matching' ? view.analysisId : null;
  const matchingQuery = useQuery<MatchingPayload>({
    queryKey: ['desktop-matching', matchingAnalysisId],
    enabled: api !== null && matchingAnalysisId !== null,
    queryFn: async () => {
      if (!api || !matchingAnalysisId) throw new Error('DESKTOP_BACKEND_NOT_READY');
      return readJson<MatchingPayload>(
        api(`/api/analyses/${matchingAnalysisId}/matching-candidates`),
      );
    },
    retry: false,
  });

  useEffect(() => {
    if (!api || view.kind !== 'processing') return;
    const processingStatus = processingQuery.data?.status;
    if (!processingStatus || !TERMINAL_PROCESSING_STATES.has(processingStatus)) return;
    if (processingStatus === 'failed') {
      setView({ kind: 'error', code: 'DESKTOP_ANALYSIS_FAILED' });
      return;
    }
    if (processingStatus === 'matching_review') {
      setView({ kind: 'matching', analysisId: view.analysisId });
      return;
    }
    if (dashboardLoadRef.current === view.analysisId) return;
    dashboardLoadRef.current = view.analysisId;
    let cancelled = false;
    readJson<DashboardPayload>(api(`/api/analyses/${view.analysisId}/dashboard`))
      .then((payload) => {
        if (cancelled) return;
        setDashboard(payload);
        setFinding(null);
        setView({ kind: 'dashboard', analysisId: view.analysisId });
      })
      .catch((error: unknown) => {
        if (!cancelled) setView({ kind: 'error', code: safeErrorCode(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [api, processingQuery.data?.status, view]);

  async function handleSelected(paths: string[]) {
    if (!providerStatus?.validated) {
      setView({ kind: 'settings' });
      return;
    }
    if (!api) {
      setView({ kind: 'error', code: 'DESKTOP_BACKEND_NOT_READY' });
      return;
    }
    try {
      setDashboard(null);
      setFinding(null);
      setEmployees(null);
      setEmployee(null);
      setReport(null);
      dashboardLoadRef.current = null;
      const created = await readJson<{ id: string }>(
        api('/api/analyses', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: '本地用工体检',
            company_display_name: '本地企业',
          }),
        }),
      );
      await readJson(
        api(`/api/analyses/${created.id}/import-paths`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths }),
        }),
      );
      await readJson(
        api(`/api/analyses/${created.id}/process`, {
          method: 'POST',
        }),
      );
      setView({ kind: 'processing', analysisId: created.id });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  async function handleSelectFinding(findingId: string) {
    if (!api || (view.kind !== 'dashboard' && view.kind !== 'employee')) return;
    try {
      const payload = await readJson<FindingDetailData>(api(`/api/findings/${findingId}`));
      setFinding(payload);
      setView({ kind: 'finding', analysisId: view.analysisId, findingId });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  async function handleOpenEmployees() {
    if (!api || view.kind !== 'dashboard') return;
    try {
      const payload = await readJson<EmployeeLedgerPayload>(
        api(`/api/analyses/${view.analysisId}/employees`),
      );
      setEmployees(payload);
      setEmployee(null);
      setView({ kind: 'employees', analysisId: view.analysisId });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  async function handleSelectEmployee(employeeId: string) {
    if (!api || view.kind !== 'employees') return;
    try {
      const payload = await readJson<EmployeeDetailPayload>(
        api(`/api/analyses/${view.analysisId}/employees/${employeeId}`),
      );
      setEmployee(payload);
      setView({ kind: 'employee', analysisId: view.analysisId, employeeId });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  async function handleOpenReport() {
    if (!api || view.kind !== 'dashboard') return;
    try {
      const payload = await readJson<ReportPayload>(
        api(`/api/analyses/${view.analysisId}/report`),
      );
      setReport(payload);
      setView({ kind: 'report', analysisId: view.analysisId });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  async function handleProviderSave(input: ProviderConfigurationInput) {
    setSavingProvider(true);
    setProviderError(null);
    try {
      await providerConfigurator(input);
      const info = await backendLoader();
      const refreshedApi = apiFactory(info);
      setApi(() => refreshedApi);
      await readJson(
        refreshedApi('/api/provider/connection-test', {
          method: 'POST',
        }),
      );
      const validated = await providerValidator();
      setProviderStatus(validated);
      setView({ kind: 'import' });
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : typeof error === 'string'
            ? error
            : 'DESKTOP_PROVIDER_CONFIGURATION_FAILED';
      setProviderError(
        /^(?:AI|DESKTOP)_[A-Z0-9_]+$/.test(message)
          ? message
          : 'DESKTOP_PROVIDER_CONFIGURATION_FAILED',
      );
      setView({ kind: 'settings' });
    } finally {
      setSavingProvider(false);
    }
  }

  async function handleMatchDecision(payload: MatchDecisionPayload) {
    if (!api || view.kind !== 'matching') return;
    setSubmittingMatch(true);
    try {
      const result = await readJson<{ analysis_status: string }>(
        api(`/api/analyses/${view.analysisId}/matching-decisions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }),
      );
      if (result.analysis_status === 'completed' || result.analysis_status === 'partial') {
        dashboardLoadRef.current = null;
        queryClient.removeQueries({
          queryKey: ['desktop-processing', view.analysisId],
        });
        setView({ kind: 'processing', analysisId: view.analysisId });
      } else {
        await matchingQuery.refetch();
      }
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    } finally {
      setSubmittingMatch(false);
    }
  }

  async function handleDeleteAnalysis() {
    if (!api || view.kind !== 'dashboard') return;
    try {
      await readJson(
        api(`/api/analyses/${view.analysisId}`, {
          method: 'DELETE',
        }),
      );
      setDashboard(null);
      setFinding(null);
      setEmployees(null);
      setEmployee(null);
      setReport(null);
      dashboardLoadRef.current = null;
      setView({ kind: 'import' });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  function returnToImport() {
    setDashboard(null);
    setFinding(null);
    setEmployees(null);
    setEmployee(null);
    setReport(null);
    dashboardLoadRef.current = null;
    setView(
      api
        ? providerStatus?.validated
          ? { kind: 'import' }
          : { kind: 'settings' }
        : { kind: 'booting' },
    );
  }

  let content;
  if (view.kind === 'booting') {
    content = (
      <section className="status-card" aria-label="desktop-status">
        <span className="status-dot" aria-hidden="true" />
        <p>正在准备本机分析服务…</p>
      </section>
    );
  } else if (view.kind === 'settings' && providerStatus) {
    content = (
      <SettingsView
        status={providerStatus}
        saving={savingProvider}
        errorCode={providerError}
        onSave={handleProviderSave}
      />
    );
  } else if (view.kind === 'import') {
    content = <ImportPanel onSelected={handleSelected} />;
  } else if (view.kind === 'processing') {
    content = (
      <ProcessingPanel
        status={processingQuery.data?.status ?? 'queued'}
        progress={processingQuery.data?.progress ?? 1}
      />
    );
  } else if (view.kind === 'matching') {
    content = matchingQuery.data ? (
      <MatchingReview
        candidates={matchingQuery.data.candidates}
        submitting={submittingMatch}
        onDecision={handleMatchDecision}
      />
    ) : (
      <section className="status-card" aria-label="desktop-status">
        <span className="status-dot" aria-hidden="true" />
        <p>正在加载人工匹配事项…</p>
      </section>
    );
  } else if (view.kind === 'dashboard' && dashboard) {
    content = (
      <DashboardView
        summary={dashboard.summary}
        findings={dashboard.findings}
        overview={dashboard.overview}
        onSelectFinding={handleSelectFinding}
        onOpenEmployees={handleOpenEmployees}
        onOpenReport={handleOpenReport}
        onDeleteAnalysis={handleDeleteAnalysis}
      />
    );
  } else if (view.kind === 'employees' && employees) {
    content = (
      <EmployeeLedger
        payload={employees}
        onSelectEmployee={handleSelectEmployee}
        onBack={() => setView({ kind: 'dashboard', analysisId: view.analysisId })}
      />
    );
  } else if (view.kind === 'employee' && employee) {
    content = (
      <EmployeeDetail
        payload={employee}
        onSelectFinding={handleSelectFinding}
        onBack={() => setView({ kind: 'employees', analysisId: view.analysisId })}
      />
    );
  } else if (view.kind === 'report' && report) {
    content = (
      <ReportView
        payload={report}
        onBack={() => setView({ kind: 'dashboard', analysisId: view.analysisId })}
      />
    );
  } else if (view.kind === 'finding' && finding) {
    content = (
      <FindingDetail
        finding={finding}
        onBack={() => setView({ kind: 'dashboard', analysisId: view.analysisId })}
      />
    );
  } else if (view.kind === 'error') {
    content = (
      <section className="error-panel" role="alert">
        <p className="eyebrow">本机分析未继续</p>
        <h2>无法继续本次分析</h2>
        <p className="muted">错误代码：{view.code}</p>
        <button type="button" className="primary-action" onClick={returnToImport}>
          返回材料导入
        </button>
      </section>
    );
  } else {
    content = (
      <section className="status-card" aria-label="desktop-status">
        <span className="status-dot" aria-hidden="true" />
        <p>正在加载本机分析结果…</p>
      </section>
    );
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <p className="eyebrow">QIAN LABOR DESKTOP</p>
        <h1>企安用工</h1>
        <p className="subtitle">本地优先劳动用工风险体检</p>
        {providerStatus && ['import', 'dashboard', 'employees', 'employee', 'report'].includes(view.kind) ? (
          <button
            type="button"
            className="text-action hero-settings-action"
            onClick={() => {
              setProviderError(null);
              setView({ kind: 'settings' });
            }}
          >
            模型设置
          </button>
        ) : null}
      </header>
      {content}
    </main>
  );
}
