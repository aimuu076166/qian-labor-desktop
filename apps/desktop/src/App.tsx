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
  type EmployeeDetailPayload,
  type EmployeeLedgerPayload,
} from './features/employees/EmployeeLedger';
import { EmployeeWorkspace } from './features/employees/EmployeeWorkspace';
import { EmployeeFactWorkbench, type AssessmentDecisionDrafts, type AssessmentDecisionRequests,
  type FactRevisionDrafts, type FactRevisionRequests, type ReevaluationRequests } from './features/employees/EmployeeFactWorkbench';
import { FindingDetail, type FindingDetailData, type FindingReviewInput } from './features/findings/FindingDetail';
import {
  MatchingReview,
  type MatchCandidate,
  type MatchDecisionPayload,
  type MatchingDraft,
} from './features/matching/MatchingReview';
import { ProcessingPanel, TaskControls, TASK_LABELS } from './features/processing/ProcessingPanel';
import { useAnalysisTask, type TaskRun } from './features/processing/useAnalysisTask';
import { describeOperationError } from './lib/errorMessages';
import { ReportView, type ReportPayload } from './features/report/ReportView';
import { ReportVersions } from './features/report/ReportVersions';
import { MaterialWorkspace, type WorkspacePayload } from './features/workspace/MaterialWorkspace';
import { ContractAdvisoryPanel, type AdvisoryRequests, type AdvisoryDrafts } from './features/workspace/ContractAdvisoryPanel';
import { HistoryView } from './features/workspace/HistoryView';
import { useHistoryWorkspace, type HistoricalAnalysis, type AnalysisBinding } from './features/workspace/useHistoryWorkspace';
import { useCurrentCorpus } from './features/workspace/useCurrentCorpus';
import { useNativeImport } from './features/workspace/useNativeImport';
import { useHistoricalActions } from './features/workspace/useHistoricalActions';
import { CompanyWorkbench } from './features/workspace/CompanyWorkbench';
import { AssessmentScopeNotice } from './features/scope/AssessmentScopeNotice';
import { useCompanyWorkspace, type RecordDetail, type EmployeeRecord } from './features/workspace/useCompanyWorkspace';
import {
  SettingsView,
  type ProviderConfigurationInput,
  type ProviderConfigurationStatus,
} from './features/settings/SettingsView';
import {
  createDesktopApi,
  getDesktopBackendInfo,
  readJson,
  safeErrorCode,
  type AssessmentRevision,
  type DesktopBackendInfo,
} from './lib/api';
import {
  configureZhipuProvider,
  getProviderConfigurationStatus,
  markZhipuProviderValidated,
  selectEmploymentFiles,
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
  | { kind: 'home' }
  | { kind: 'company-employees' }
  | { kind: 'company-employee'; recordId: string }
  | { kind: 'reports' }
  | { kind: 'history' }
  | { kind: 'materials'; analysisId: string }
  | { kind: 'processing'; analysisId: string }
  | { kind: 'matching'; analysisId: string }
  | { kind: 'dashboard'; analysisId: string }
  | { kind: 'employees'; analysisId: string }
  | { kind: 'employee'; analysisId: string; employeeId: string }
  | { kind: 'report'; analysisId: string }
  | { kind: 'finding'; analysisId: string; findingId: string }
  | { kind: 'error'; code: string; analysisId?: string };

type ProcessingStatus = {
  analysis_id: string;
  status: string;
  progress: number;
  current_stage: string;
  files?: Array<{ id: string; filename: string; status: string; progress?: number; error_code?: string | null }>;
  task_run?: TaskRun | null;
};

type DashboardPayload = {
  summary: DashboardSummary;
  findings: DashboardFinding[];
  overview?: DashboardOverview;
};

type MatchingPayload = {
  analysis_id: string;
  candidates: MatchCandidate[];
  current_company_id?: string;
  employee_record_options?: EmployeeRecord[];
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
  const backendGeneration = useRef({ api, value: 0 });
  if (backendGeneration.current.api !== api) backendGeneration.current = { api, value: backendGeneration.current.value + 1 };
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [finding, setFinding] = useState<FindingDetailData | null>(null);
  const [matchingError, setMatchingError] = useState<string | null>(null);
  const matchingDrafts = useRef(new Map<string, MatchingDraft>());
  const advisoryRequests = useRef<AdvisoryRequests>(new Map());
  const advisoryDrafts = useRef<AdvisoryDrafts>(new Map());
  const factRevisionRequests = useRef<FactRevisionRequests>(new Map());
  const factRevisionDrafts = useRef<FactRevisionDrafts>(new Map());
  const assessmentDecisionRequests = useRef<AssessmentDecisionRequests>(new Map());
  const assessmentDecisionDrafts = useRef<AssessmentDecisionDrafts>(new Map());
  const reevaluationRequests = useRef<ReevaluationRequests>(new Map());
  const [advisoryFile, setAdvisoryFile] = useState<{ analysisId: string; fileId: string } | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [resultReadBusy, setResultReadBusy] = useState(false);
  const findingOrigin = useRef<Extract<DesktopView, { kind: 'dashboard' | 'employee' }> | null>(null);
  const staleLedger = useRef<string | null>(null);
  const assessmentRefreshEpoch = useRef(0);
  const [openingHistory, setOpeningHistory] = useState(false);
  const historyOpeningTarget = useRef<{ id: string; epoch: number } | null>(null);
  const historyReturn = useRef<DesktopView>({ kind: 'home' });
  const historicalOrigin = useRef<DesktopView>({ kind: 'history' });
  const [employees, setEmployees] = useState<EmployeeLedgerPayload | null>(null);
  const [employeeQuery, setEmployeeQuery] = useState('');
  const [employee, setEmployee] = useState<EmployeeDetailPayload | null>(null);
  const [report, setReport] = useState<ReportPayload | null>(null);
  const reportSelections = useRef(new Map<string, string>());
  const [submittingMatch, setSubmittingMatch] = useState(false);
  const [providerStatus, setProviderStatus] = useState<ProviderConfigurationStatus | null>(null);
  const [savingProvider, setSavingProvider] = useState(false);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [selectingMaterials, setSelectingMaterials] = useState(false);
  const dashboardLoadRef = useRef<string | null>(null);
  const [currentAnalysisId, setCurrentAnalysisId] = useState<string | null>(null);
  const [companyName, setCompanyName] = useState('');
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const settingsReturn = useRef<DesktopView>({ kind: 'home' });
  const navigationEpoch = useRef(0);
  const retryRead = useRef<(() => Promise<void>) | null>(null);
  const company = useCompanyWorkspace(api, view.kind === 'company-employees' ? 'employees' : 'home');
  const history = useHistoryWorkspace(api, company.company, view.kind === 'history');
  const [record, setRecord] = useState<RecordDetail | null>(null);
  const recordRef = useRef<RecordDetail | null>(null);
  recordRef.current = record;
  const recordOrigin = useRef<DesktopView>({ kind: 'home' });
  const canonicalFindingOrigin = useRef<string | null>(null);
  const homeFindingOrigin = useRef<DesktopView | null>(null);
  const [historical, setHistorical] = useState(false);
  const [resumePaths, setResumePaths] = useState<string[] | null>(null);
  const corpus = useCurrentCorpus(api);
  const nativeImport = useNativeImport(api);
  const nativeState = nativeImport.state(company.companyId);
  const selectedCompanyRef = useRef(company.companyId);
  selectedCompanyRef.current = company.companyId;
  const nativePendingPaths = useRef<{ companyId: string; paths: string[] } | null>(null);
  const pendingCorpus = corpus.pending(company.companyId);
  const activeCurrentId = company.current.data?.current_analysis?.analysis_id ?? null;
  const historyActions = useHistoricalActions(api, company.company, activeCurrentId, corpus, (companyId, id) => {
    if (companyId !== company.companyId) return;
    if (historyOpeningTarget.current?.id === id && historyOpeningTarget.current.epoch === navigationEpoch.current) navigationEpoch.current++;
    if (history.selected?.id === id) { history.setSelected(null); setHistorical(false); }
    if (history.draft?.analysis.id === id) history.cancel();
    setRecord(old => old?.company_id === companyId ? { ...old,
      bindings: old.bindings.filter(binding => binding.analysis_id !== id),
      historical_bindings: old.historical_bindings.filter(binding => binding.analysis_id !== id) } : old);
    if ('analysisId' in settingsReturn.current && settingsReturn.current.analysisId === id) settingsReturn.current = { kind: 'history' };
    if ('analysisId' in view && view.analysisId === id) navigate({ kind: 'history' });
  });
  const historicalView = historical && Boolean(history.selected && 'analysisId' in view && history.selected.id === view.analysisId);
  const taskHistory = historical ? history.selected : null;
  const task = useAnalysisTask(api, company.companyId && (taskHistory || activeCurrentId) ? {
    analysisId: taskHistory?.id ?? activeCurrentId!, uiCompanyId: company.companyId,
    ownerId: taskHistory?.relation === 'unbound' ? null : taskHistory?.company_id ?? company.companyId,
    legacy: taskHistory?.relation === 'unbound',
  } : null);
  const legacyEligible = taskHistory?.relation === 'unbound' && ['created', 'uploading', 'failed'].includes(taskHistory.status);
  const analysisRunning = !historical && (task.active || Boolean(task.busy || task.pending));
  const currentDashboard = useQuery<DashboardPayload>({ queryKey: ['current-dashboard', activeCurrentId, company.current.data?.current_analysis?.analysis_version],
    enabled: Boolean(api && activeCurrentId && !company.current.data?.current_analysis?.stale), retry: false,
    queryFn: ({ signal }) => readJson<DashboardPayload>(api!(`/api/analyses/${activeCurrentId}/dashboard`, { signal })) });

  useEffect(() => {
    setCurrentAnalysisId(activeCurrentId);
  }, [activeCurrentId]);

  function navigate(next: DesktopView) {
    navigationEpoch.current += 1;
    retryRead.current = null;
    setResultReadBusy(false);
    setWorkspaceError(null);
    setView(next);
  }

  function enterCurrentProcessing(analysisId: string) {
    setHistorical(false);
    history.setSelected(null);
    historicalOrigin.current = { kind: 'history' };
    navigate({ kind: 'processing', analysisId });
  }

  function openSettings() {
    if (view.kind === 'settings') return;
    settingsReturn.current = view;
    navigate({ kind: 'settings' });
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([backendLoader(), configurationLoader()])
      .then(async ([info, configuration]) => {
        if (cancelled) return;
        const nextApi = apiFactory(info);
        setApi(() => nextApi);
        setProviderStatus(configuration);
        setView({ kind: 'home' });
      })
      .catch((error: unknown) => {
        if (!cancelled) setView({ kind: 'error', code: safeErrorCode(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [apiFactory, backendLoader, configurationLoader]);

  const materialsId = view.kind === 'materials' || view.kind === 'processing' ? view.analysisId : null;
  const workspaceQuery = useQuery<WorkspacePayload>({
    queryKey: ['desktop-workspace', materialsId, backendGeneration.current.value],
    enabled: api !== null && materialsId !== null,
    queryFn: () => readJson<WorkspacePayload>(api!(`/api/analyses/${materialsId}/workspace`)),
    refetchInterval: (query) => (view.kind === 'processing' || view.kind === 'materials') && task.active && !query.state.error ? 1000 : false,
    retry: false,
  });

  const processingAnalysisId = view.kind === 'processing' ? view.analysisId : analysisRunning ? activeCurrentId : null;
  const processingQuery = useQuery<ProcessingStatus>({
    queryKey: ['desktop-processing', processingAnalysisId, backendGeneration.current.value],
    enabled: api !== null && processingAnalysisId !== null,
    queryFn: async () => {
      if (!api || !processingAnalysisId) throw new Error('DESKTOP_BACKEND_NOT_READY');
      return readJson<ProcessingStatus>(
        api(`/api/analyses/${processingAnalysisId}/processing`),
      );
    },
    refetchInterval: (query) => query.state.error || !task.active ? false : 1000,
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
    if (!task.data || task.pending || task.active || task.run && ['cancelled', 'interrupted', 'failed', 'partial'].includes(task.run.state)) return;
    const processingStatus = processingQuery.data?.status;
    if (!processingStatus || !TERMINAL_PROCESSING_STATES.has(processingStatus)) return;
    if (processingStatus === 'failed') {
      const errorCode = processingQuery.data?.files?.find((item) => item.error_code)?.error_code;
      if (historicalView) {
        setWorkspaceError(errorCode ?? 'DESKTOP_ANALYSIS_FAILED');
        void queryClient.invalidateQueries({ queryKey: ['desktop-workspace', view.analysisId] });
        setView({ kind: 'materials', analysisId: view.analysisId });
        return;
      }
      setView({
        kind: 'error',
        code: errorCode ?? 'DESKTOP_ANALYSIS_FAILED',
        analysisId: view.analysisId,
      });
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
  }, [api, processingQuery.data?.status, view, task.data, task.pending, task.active, task.run]);

  useEffect(() => {
    if (!api || !task.target || !task.data) return;
    void queryClient.invalidateQueries({ queryKey: ['desktop-processing', task.target.analysisId] });
    void queryClient.invalidateQueries({ queryKey: ['desktop-workspace', task.target.analysisId] });
    if (!historical) void company.refresh();
    // Refresh business projections only when a task transition or backend change is observed.
  }, [api, task.target?.analysisId, task.run?.id, task.run?.version]);

  async function handleSelected(paths: string[], epoch = navigationEpoch.current) {
    if (!api || !company.companyId || !company.company || !paths.length || historical) {
      setView({ kind: 'error', code: 'DESKTOP_BACKEND_NOT_READY' });
      return;
    }
    const selectedCompanyId = company.companyId;
    let analysisId = activeCurrentId;
    try {
      setWorkspaceError(null);
      dashboardLoadRef.current = null;
      nativePendingPaths.current = { companyId: selectedCompanyId, paths };
      analysisId = await corpus.resolve(company.company);
      nativePendingPaths.current = null;
      if (selectedCompanyRef.current === selectedCompanyId) setCurrentAnalysisId(analysisId);
      await nativeImport.submit(selectedCompanyId, analysisId, paths);
      if (selectedCompanyRef.current === selectedCompanyId && epoch === navigationEpoch.current) setView({ kind: 'materials', analysisId });
    } catch (error) {
      if (epoch === navigationEpoch.current) {
        setWorkspaceError(safeErrorCode(error));
        if (analysisId) setView({ kind: 'materials', analysisId });
      }
    }
  }

  async function startMaterialAnalysis() {
    const targetId = currentAnalysisId;
    if (!api || !targetId || targetId !== activeCurrentId || historical || analysisRunning || task.locked
      || nativeState?.phase === 'unknown' || nativeImport.busy(company.companyId)) return;
    const epoch = ++navigationEpoch.current;
    if (!providerStatus?.validated) {
      settingsReturn.current = view;
      setView({ kind: 'settings' });
      return;
    }
    setWorkspaceError(null);
    const accepted = await task.command('start');
    if (accepted && epoch === navigationEpoch.current && selectedCompanyRef.current === task.target?.uiCompanyId) {
      queryClient.removeQueries({ queryKey: ['desktop-processing', targetId] });
      dashboardLoadRef.current = null;
      void company.refresh();
      enterCurrentProcessing(targetId);
    }
  }

  async function handleSelectMaterials() {
    if (selectingMaterials || analysisRunning || historical || pendingCorpus?.uncertain
      || nativeState?.phase === 'unknown' || nativeImport.busy(company.companyId)) return;
    const epoch = ++navigationEpoch.current;
    setSelectingMaterials(true);
    try {
      const paths = await selectEmploymentFiles();
      if (paths.length) await handleSelected(paths, epoch);
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    } finally {
      setSelectingMaterials(false);
    }
  }

  async function handleRetryAnalysis() { await startMaterialAnalysis(); }

  async function handleSelectFinding(findingId: string) {
    if (!api || !['dashboard', 'employee', 'company-employee', 'home', 'company-employees'].includes(view.kind)) return;
    const epoch = ++navigationEpoch.current;
    retryRead.current = () => handleSelectFinding(findingId);
    try {
      const payload = await readJson<FindingDetailData>(api(`/api/findings/${findingId}`));
      if (epoch !== navigationEpoch.current) return;
      setFinding(payload);
      canonicalFindingOrigin.current = view.kind === 'company-employee' ? view.recordId : null;
      homeFindingOrigin.current = view.kind === 'home' || view.kind === 'company-employees' ? view : null;
      findingOrigin.current = view.kind === 'dashboard' || view.kind === 'employee' ? view : null;
      setView({ kind: 'finding', analysisId: payload.analysis_id, findingId });
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    }
  }

  async function handleOpenEmployees() {
    if (!api || view.kind !== 'dashboard') return;
    if (!historical) { navigate({ kind: 'company-employees' }); return; }
    const epoch = ++navigationEpoch.current;
    retryRead.current = handleOpenEmployees;
    try {
      const payload = await readJson<EmployeeLedgerPayload>(
        api(`/api/analyses/${view.analysisId}/employees`),
      );
      if (epoch !== navigationEpoch.current) return;
      setEmployees(payload);
      staleLedger.current = null;
      setEmployeeQuery('');
      setEmployee(null);
      setView({ kind: 'employees', analysisId: view.analysisId });
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    }
  }

  async function handleFindingReview(input: FindingReviewInput) {
    if (!api || view.kind !== 'finding' || !finding || finding.id !== view.findingId || reviewBusy) throw new Error('DESKTOP_REVIEW_UNAVAILABLE');
    const epoch = navigationEpoch.current;
    setReviewBusy(true);
    try {
      const updated = await readJson<FindingDetailData>(api(`/api/findings/${finding.id}/reviews`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
      }));
      if (epoch === navigationEpoch.current) setFinding(updated);
      // Refresh summaries when leaving this detail; never re-submit a successful review on a read failure.
      setReport(null);
      staleLedger.current = view.analysisId;
      void company.refresh();
      void queryClient.invalidateQueries({ queryKey: ['current-dashboard', view.analysisId] });
    } finally { setReviewBusy(false); }
  }

  async function reloadFinding() {
    if (!api || view.kind !== 'finding' || !finding || finding.id !== view.findingId || reviewBusy) throw new Error('DESKTOP_REVIEW_UNAVAILABLE');
    setReviewBusy(true);
    const epoch = navigationEpoch.current;
    try {
      const updated = await readJson<FindingDetailData>(api(`/api/findings/${finding.id}`));
      if (epoch !== navigationEpoch.current) return;
      setFinding(updated);
      staleLedger.current = view.analysisId;
      setReport(null);
    }
    finally { setReviewBusy(false); }
  }

  async function returnFromFinding() {
    if (!api || view.kind !== 'finding' || resultReadBusy) return;
    const origin = findingOrigin.current;
    if (canonicalFindingOrigin.current) { await openRecord(canonicalFindingOrigin.current, false); return; }
    if (homeFindingOrigin.current) {
      const epoch = navigationEpoch.current;
      try { const refreshed = await currentDashboard.refetch(); if (refreshed.error) throw refreshed.error;
        await company.refresh(); if (epoch === navigationEpoch.current) navigate(homeFindingOrigin.current);
      } catch (cause) { if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(cause)); }
      return;
    }
    if (origin?.kind !== 'employee' || origin.analysisId !== view.analysisId) {
      await refreshDashboard(view.analysisId);
      return;
    }
    const epoch = ++navigationEpoch.current;
    setWorkspaceError(null); setResultReadBusy(true);
    try {
      const payload = await readJson<EmployeeDetailPayload>(
        api(`/api/analyses/${origin.analysisId}/employees/${origin.employeeId}`));
      if (epoch !== navigationEpoch.current) return;
      setEmployee(payload); setView(origin);
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    } finally { if (epoch === navigationEpoch.current) setResultReadBusy(false); }
  }

  async function returnToLedger(analysisId: string) {
    if (!api || !employees || resultReadBusy) return;
    if (staleLedger.current !== analysisId) {
      navigate({ kind: 'employees', analysisId });
      return;
    }
    const epoch = ++navigationEpoch.current;
    setWorkspaceError(null); setResultReadBusy(true);
    try {
      const params = new URLSearchParams({ page: String(employees.page),
        page_size: String(employees.page_size), query: employeeQuery });
      const payload = await readJson<EmployeeLedgerPayload>(api(`/api/analyses/${analysisId}/employees?${params}`));
      if (epoch !== navigationEpoch.current) return;
      setEmployees(payload); staleLedger.current = null;
      setView({ kind: 'employees', analysisId });
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    } finally { if (epoch === navigationEpoch.current) setResultReadBusy(false); }
  }

  async function refreshDashboard(analysisId: string) {
    if (!api) return;
    const epoch = ++navigationEpoch.current;
    retryRead.current = () => refreshDashboard(analysisId);
    setWorkspaceError(null); setResultReadBusy(true);
    try {
      const data = await readJson<DashboardPayload>(api(`/api/analyses/${analysisId}/dashboard`));
      if (epoch !== navigationEpoch.current) return;
      setDashboard(data); setView({ kind: 'dashboard', analysisId });
    } catch (error) { if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error)); }
    finally { if (epoch === navigationEpoch.current) setResultReadBusy(false); }
  }

  async function openHistoricalAnalysis(analysis: HistoricalAnalysis) {
    if (!api || openingHistory) throw new Error('DESKTOP_OPERATION_BUSY');
    const analysisId = analysis.id;
    const epoch = ++navigationEpoch.current;
    historyOpeningTarget.current = { id: analysisId, epoch };
    setOpeningHistory(true);
    try {
      const workspace = await readJson<WorkspacePayload>(api(`/api/analyses/${analysisId}/workspace`));
      if (workspace.analysis.id !== analysisId) throw new Error('DESKTOP_ANALYSIS_MISMATCH');
      const status = workspace.analysis.status;
      const result = ['completed', 'partial'].includes(status)
        ? await readJson<DashboardPayload>(api(`/api/analyses/${analysisId}/dashboard`)) : null;
      if (epoch !== navigationEpoch.current) return;
      historicalOrigin.current = { kind: 'history' };
      history.setSelected({ ...analysis, assessment_scope: workspace.analysis.assessment_scope ?? analysis.assessment_scope });
      setHistorical(true); setWorkspaceError(null); setMatchingError(null);
      setDashboard(result); setFinding(null); setEmployees(null); setEmployee(null); setReport(null);
      dashboardLoadRef.current = null;
      queryClient.setQueryData(['desktop-workspace', analysisId, backendGeneration.current.value], workspace);
      queryClient.removeQueries({ queryKey: ['desktop-processing', analysisId] });
      queryClient.removeQueries({ queryKey: ['desktop-matching', analysisId] });
      if (result) setView({ kind: 'dashboard', analysisId });
      else setView({ kind: 'materials', analysisId });
    } catch (cause) { if (epoch === navigationEpoch.current) throw cause; }
    finally { if (historyOpeningTarget.current?.epoch === epoch) historyOpeningTarget.current = null; setOpeningHistory(false); }
  }

  async function handleSelectEmployee(employeeId: string) {
    if (!api || view.kind !== 'employees') return;
    const epoch = ++navigationEpoch.current;
    retryRead.current = () => handleSelectEmployee(employeeId);
    try {
      const payload = await readJson<EmployeeDetailPayload>(
        api(`/api/analyses/${view.analysisId}/employees/${employeeId}`),
      );
      if (epoch !== navigationEpoch.current) return;
      setEmployee(payload);
      setView({ kind: 'employee', analysisId: view.analysisId, employeeId });
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    }
  }

  async function openHistoricalSnapshot(analysisId: string, snapshotId: string) {
    if (!api || !company.companyId || view.kind !== 'company-employee') return;
    const origin = view;
    const epoch = ++navigationEpoch.current;
    retryRead.current = () => openHistoricalSnapshot(analysisId, snapshotId);
    setWorkspaceError(null);
    try {
      const binding = await readJson<AnalysisBinding>(api(`/api/company-workspaces/${company.companyId}/analyses/${analysisId}/binding`));
      if (!binding.bound || binding.role !== 'historical') throw new Error('WORKSPACE_ANALYSIS_ALREADY_BOUND');
      const workspace = await readJson<WorkspacePayload>(api(`/api/analyses/${analysisId}/workspace`));
      const snapshot = await readJson<EmployeeDetailPayload>(api(`/api/analyses/${analysisId}/employees/${snapshotId}`));
      if (epoch !== navigationEpoch.current) return;
      historicalOrigin.current = origin;
      history.setSelected({ ...workspace.analysis, created_at: workspace.analysis.created_at ?? '', assessment_scope: workspace.analysis.assessment_scope ?? snapshot.assessment_scope,
        relation: 'historical', company_id: company.companyId, can_adopt: false, adoption_blocker_code: null, file_count: workspace.files.length, employee_count: 0 });
      setHistorical(true); setEmployee(snapshot); setView({ kind: 'employee', analysisId, employeeId: snapshotId });
    } catch (cause) { if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(cause)); }
  }

  function returnFromHistory() {
    const origin = historicalOrigin.current;
    if (origin.kind === 'company-employee') { void openRecord(origin.recordId, false); }
    else { setHistorical(false); navigate(origin); }
  }

  async function processLegacy() {
    if (!historicalView || history.selected?.relation !== 'unbound') return;
    if (!providerStatus?.validated) { openSettings(); return; }
    const epoch = navigationEpoch.current;
    const accepted = await task.command('start', () => epoch === navigationEpoch.current);
    if (epoch !== navigationEpoch.current || !accepted) return;
    dashboardLoadRef.current = null;
    queryClient.removeQueries({ queryKey: ['desktop-processing', task.target!.analysisId] });
    navigate({ kind: 'processing', analysisId: task.target!.analysisId });
  }

  async function resumeTask() {
    if (!providerStatus?.validated) { openSettings(); return; }
    const epoch = navigationEpoch.current;
    const accepted = await task.command('resume', () => epoch === navigationEpoch.current);
    if (accepted && epoch === navigationEpoch.current && task.target) navigate({ kind: 'processing', analysisId: task.target.analysisId });
  }

  async function retryOriginalTask() {
    if (!task.pending) return;
    if (task.pending.operation !== 'cancel' && !providerStatus?.validated) { openSettings(); return; }
    const epoch = navigationEpoch.current;
    const accepted = await task.retryOriginal(() => epoch === navigationEpoch.current);
    if (accepted && epoch === navigationEpoch.current && task.target) navigate({ kind: 'processing', analysisId: task.target.analysisId });
  }

  async function openRecord(recordId: string, captureOrigin = true) {
    if (!api || !company.companyId) return;
    const epoch = ++navigationEpoch.current;
    retryRead.current = () => openRecord(recordId, captureOrigin);
    if (captureOrigin) recordOrigin.current = view;
    setResultReadBusy(true); setWorkspaceError(null);
    try {
      const detail = await readJson<RecordDetail>(api(`/api/company-workspaces/${company.companyId}/employees/${recordId}`));
      const binding = detail.current_binding;
      const snapshot = binding ? await readJson<EmployeeDetailPayload>(api(`/api/analyses/${binding.analysis_id}/employees/${binding.snapshot_id}`)) : null;
      if (epoch !== navigationEpoch.current) return;
      setHistorical(false); setRecord(detail); setEmployee(snapshot); setView({ kind: 'company-employee', recordId });
      void company.refresh();
    } catch (cause) { if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(cause)); }
    finally { if (epoch === navigationEpoch.current) setResultReadBusy(false); }
  }

  function handleAssessmentRevision(companyId: string, analysisId: string, recordId: string, revision: AssessmentRevision) {
    const matchesLatestTarget = () => selectedCompanyRef.current === companyId && recordRef.current?.id === recordId
      && recordRef.current.current_binding?.analysis_id === analysisId;
    if (!matchesLatestTarget()) return;
    const navigationGeneration = navigationEpoch.current;
    const snapshotId = recordRef.current!.current_binding!.snapshot_id;
    const refreshEpoch = ++assessmentRefreshEpoch.current;
    setReport(null); staleLedger.current = analysisId;
    setEmployee(current => current && matchesLatestTarget() ? { ...current, assessment_revision: revision } : current);
    void queryClient.invalidateQueries({ queryKey: ['current-dashboard', analysisId] });
    void company.refresh();
    if (!api) return;
    void readJson<EmployeeDetailPayload>(api(`/api/analyses/${analysisId}/employees/${snapshotId}`))
      .then(refreshed => setEmployee(current => refreshEpoch === assessmentRefreshEpoch.current
        && navigationGeneration === navigationEpoch.current && matchesLatestTarget()
        ? { ...refreshed, assessment_revision: refreshed.assessment_revision ?? revision } : current))
      .catch(() => undefined);
  }

  async function openCurrentReport() {
    if (!api || !activeCurrentId) return;
    const epoch = ++navigationEpoch.current;
    retryRead.current = openCurrentReport;
    // Stored versions are readable independently of today's optional live draft.
    if (company.companyId) { setReport(null); setView({ kind: 'report', analysisId: activeCurrentId }); }
    try {
      const payload = await readJson<ReportPayload>(api(`/api/analyses/${activeCurrentId}/report`));
      if (epoch !== navigationEpoch.current) return;
      setReport(payload); setView({ kind: 'report', analysisId: activeCurrentId });
    } catch (cause) { if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(cause)); }
  }

  async function reconcileCurrentMaterials() {
    if (!api || !company.companyId) return;
    const selectedCompanyId = company.companyId;
    const epoch = navigationEpoch.current;
    try {
      const current = await corpus.reconcile(selectedCompanyId);
      if (epoch !== navigationEpoch.current) return;
      if (current) setCurrentAnalysisId(current.analysis_id);
      if (nativePendingPaths.current?.companyId === selectedCompanyId) setResumePaths(nativePendingPaths.current.paths);
      setWorkspaceError(null); await company.refresh();
    } catch (cause) { if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(cause)); }
  }

  async function handleOpenReport() {
    if (!api || view.kind !== 'dashboard') return;
    const epoch = ++navigationEpoch.current;
    retryRead.current = handleOpenReport;
    const reportOwner = historical ? history.selected?.company_id : company.companyId;
    if (reportOwner) { setReport(null); setView({ kind: 'report', analysisId: view.analysisId }); }
    try {
      const payload = await readJson<ReportPayload>(
        api(`/api/analyses/${view.analysisId}/report`),
      );
      if (epoch !== navigationEpoch.current) return;
      setReport(payload);
      setView({ kind: 'report', analysisId: view.analysisId });
    } catch (error) {
      if (epoch === navigationEpoch.current) setWorkspaceError(safeErrorCode(error));
    }
  }

  async function handleProviderSave(input: ProviderConfigurationInput) {
    const epoch = navigationEpoch.current;
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
      if (epoch === navigationEpoch.current) setView(settingsReturn.current);
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
      if (epoch === navigationEpoch.current) setView({ kind: 'settings' });
    } finally {
      setSavingProvider(false);
    }
  }

  async function handleMatchDecision(payload: MatchDecisionPayload) {
    if (!api || view.kind !== 'matching' || historical || task.active || task.locked) return;
    const epoch = navigationEpoch.current;
    setSubmittingMatch(true);
    setMatchingError(null);
    try {
      const result = await readJson<{ analysis_status: string }>(
        api(`/api/analyses/${view.analysisId}/matching-decisions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }),
      );
      void company.refresh();
      if (epoch !== navigationEpoch.current) return;
      if (result.analysis_status === 'completed' || result.analysis_status === 'partial') {
        dashboardLoadRef.current = null;
        queryClient.removeQueries({
          queryKey: ['desktop-processing', view.analysisId],
        });
        if (view.analysisId === activeCurrentId) enterCurrentProcessing(view.analysisId);
        else setView({ kind: 'processing', analysisId: view.analysisId });
      } else {
        await matchingQuery.refetch();
      }
    } catch (error) {
      if (epoch === navigationEpoch.current) setMatchingError(safeErrorCode(error));
    } finally {
      setSubmittingMatch(false);
    }
  }

  async function reconcileMatching() {
    if (!api || view.kind !== 'matching' || submittingMatch) return;
    const epoch = navigationEpoch.current;
    setSubmittingMatch(true);
    try {
      // A timed-out POST may already have committed. Read state; never blindly replay it.
      const current = await readJson<ProcessingStatus>(api(`/api/analyses/${view.analysisId}/processing`));
      if (epoch !== navigationEpoch.current) return;
      void company.refresh();
      if (current.status === 'matching_review') {
        const refreshed = await matchingQuery.refetch();
        if (epoch !== navigationEpoch.current) return;
        if (refreshed.error) throw refreshed.error;
        if (!refreshed.data?.candidates.length) throw new Error('MATCH_STATE_INCONSISTENT');
        setMatchingError(null);
      } else if (TERMINAL_PROCESSING_STATES.has(current.status) || ['queued', 'parsing', 'extracting', 'evaluating'].includes(current.status)) {
        setMatchingError(null);
        dashboardLoadRef.current = null;
        queryClient.setQueryData(['desktop-processing', view.analysisId, backendGeneration.current.value], current);
        if (view.analysisId === activeCurrentId) enterCurrentProcessing(view.analysisId);
        else setView({ kind: 'processing', analysisId: view.analysisId });
      } else {
        setMatchingError(null);
        setView({ kind: 'materials', analysisId: view.analysisId });
      }
    } catch (error) { if (epoch === navigationEpoch.current) setMatchingError(safeErrorCode(error)); }
    finally { setSubmittingMatch(false); }
  }

  function returnHome() {
    navigationEpoch.current += 1;
    setResultReadBusy(false);
    staleLedger.current = null;
    setHistorical(false);
    setDashboard(null);
    setFinding(null);
    setEmployees(null);
    setEmployee(null);
    setReport(null);
    dashboardLoadRef.current = null;
    setView(
      api ? { kind: 'home' } : { kind: 'booting' },
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
      <><button type="button" className="text-action" disabled={savingProvider}
        onClick={() => navigate(settingsReturn.current)}>返回工作区</button>
      <SettingsView
        status={providerStatus}
        saving={savingProvider}
        errorCode={providerError}
        onSave={handleProviderSave}
      />
      </>
    );
  } else if (view.kind === 'home' || view.kind === 'company-employees') {
    content = !company.loaded ? <p>正在读取企业档案…</p> : !company.companyId ? <section className="settings-form">
      <h2>建立企业本地档案</h2><p>选择已有企业或建立档案，再导入材料确认员工归属。浏览和导入无需模型连接。</p>
      <label htmlFor="workspace-company">企业名称</label><input id="workspace-company" value={companyName} maxLength={200} onChange={e => setCompanyName(e.target.value)} />
      <button type="button" disabled={company.busy || !companyName.trim() || company.uncertainCreation} onClick={() => company.create(companyName)}>建立本地档案</button>
    </section> : company.current.data ? <CompanyWorkbench payload={company.current.data} filters={company.filters}
      priorities={currentDashboard.data && !company.current.data.current_analysis?.stale ? <details className="workbench-priorities">
        <summary>当前重点：高风险 {currentDashboard.data.summary.high_count} · 中风险 {currentDashboard.data.summary.medium_count} · 资料不足 {currentDashboard.data.summary.insufficient_data_count}</summary>
        <AssessmentScopeNotice scope={currentDashboard.data.overview?.assessment_scope} compact />
        <div className="workbench-priority-actions">{currentDashboard.data.findings.filter(item => item.severity === 'high' || item.requires_human_review).slice(0, 3).map(item =>
          <button type="button" className="finding-row" key={item.id} onClick={() => handleSelectFinding(item.id)}>{item.title}</button>)}</div>
      </details> : null}
      onFilters={company.setFilters} onSelectEmployee={id => { void openRecord(id); }} onImport={handleSelectMaterials}
      busy={selectingMaterials || analysisRunning || Boolean(pendingCorpus?.uncertain)}
      onMatching={() => activeCurrentId && navigate({ kind: 'matching', analysisId: activeCurrentId })}
      onOverview={() => activeCurrentId && void refreshDashboard(activeCurrentId)} />
      : <p>正在读取员工档案…</p>;
  } else if (view.kind === 'company-employee' && record) {
    content = <section>
      <p>员工档案 · {record.lifecycle_status === 'archived' ? '已归档' : '使用中'}</p>
      {!employee ? <h2>{record.masked_name}</h2> : null}
      <p>当前材料关联：{record.current_binding ? record.current_binding.analysis_id : '待补材料'}</p>
      {company.current.data?.current_analysis?.stale && record.current_binding ? <p>当前材料尚待完成分析；下列已保存事项尚未按最新材料更新。</p> : null}
      {record.historical_bindings.length ? <section aria-label="历史材料关联"><h3>历史材料关联（不计入当前评估）</h3>
        {record.historical_bindings.map(binding => <p key={binding.snapshot_id}><button type="button" onClick={() => openHistoricalSnapshot(binding.analysis_id, binding.snapshot_id)}>
          查看历史员工快照 · {binding.created_at.replace('T', ' ').slice(0, 19)}</button></p>)}</section> : null}
      {employee ? <EmployeeDetail payload={employee} onSelectFinding={handleSelectFinding}
        onBack={() => { void company.refresh(); navigate(recordOrigin.current); }} /> : <>
        <p>当前没有可读取的评估结果，请补充材料并开始分析。</p>
        <button type="button" onClick={() => navigate(recordOrigin.current)}>返回员工台账</button></>}
      {api && company.companyId && record.current_binding ? <EmployeeFactWorkbench
        key={`${company.companyId}/${record.current_binding.analysis_id}/${record.id}`}
        companyId={company.companyId} analysisId={record.current_binding.analysis_id} recordId={record.id} api={api}
        factDrafts={factRevisionDrafts.current} factRequests={factRevisionRequests.current}
        decisionDrafts={assessmentDecisionDrafts.current} decisionRequests={assessmentDecisionRequests.current}
        reevaluationRequests={reevaluationRequests.current}
        onInputsCommitted={revision => handleAssessmentRevision(company.companyId!, record.current_binding!.analysis_id, record.id, revision)}
        onReevaluated={revision => handleAssessmentRevision(company.companyId!, record.current_binding!.analysis_id, record.id, revision)} /> : null}
      {api && company.companyId && record.current_binding ? <ContractAdvisoryPanel
        key={`${company.companyId}/${record.id}`} api={api} companyId={company.companyId}
        analysisId={record.current_binding.analysis_id} recordId={record.id} requests={advisoryRequests.current} drafts={advisoryDrafts.current} /> : null}
    </section>;
  } else if (view.kind === 'reports') {
    content = <section><div className="print-hidden"><h2>报告</h2><p>当前报告为实时草稿；下方可以明确保存不可变的版本。</p></div>
      {api && company.companyId && activeCurrentId ? <ReportVersions key={`${company.companyId}/${activeCurrentId}`}
        api={api} companyId={company.companyId} analysisId={activeCurrentId} selections={reportSelections.current}
        onBack={() => navigate({ kind: 'reports' })} /> : null}
      {activeCurrentId ? <button type="button" className="print-hidden" onClick={() => { void openCurrentReport(); }}>查看当前报告草稿</button> : <p>当前尚无材料报告。</p>}
      <button type="button" className="print-hidden" onClick={() => { historyReturn.current = view; navigate({ kind: 'history' }); }}>历史体检</button>
    </section>;
  } else if (view.kind === 'history' && api) {
    content = <HistoryView controller={history} onOpen={openHistoricalAnalysis} actions={historyActions} currentId={activeCurrentId}
      onCurrent={() => { if (activeCurrentId) { setHistorical(false); history.setSelected(null); historicalOrigin.current = { kind: 'history' }; setCurrentAnalysisId(activeCurrentId); navigate({ kind: 'materials', analysisId: activeCurrentId }); } }}
      onMatching={analysis => { historicalOrigin.current = { kind: 'history' }; history.setSelected(analysis); setHistorical(true); navigate({ kind: 'matching', analysisId: analysis.id }); }}
      onBack={() => navigate(historyReturn.current)} />;
  } else if (view.kind === 'materials') {
    content = workspaceQuery.data ? <MaterialWorkspace payload={workspaceQuery.data}
      configured={providerStatus?.validated ?? false} busy={selectingMaterials || analysisRunning || historical || nativeState?.phase === 'unknown' || nativeImport.busy(company.companyId)}
      processDisabled={task.locked || task.canResume || task.active} processing={task.active}
      taskPanel={<TaskControls task={task} files={workspaceQuery.data.files} onResume={resumeTask} onRetry={retryOriginalTask}
        onMatching={() => navigate({ kind: 'matching', analysisId: view.analysisId })} />}
      readOnly={historical}
      onSelectAdvisory={company.companyId ? fileId => setAdvisoryFile({ analysisId: view.analysisId, fileId }) : undefined}
      advisoryPanel={api && company.companyId && advisoryFile?.analysisId === view.analysisId ? <ContractAdvisoryPanel
        key={`${company.companyId}/${view.analysisId}/${advisoryFile.fileId}`} api={api} companyId={company.companyId}
        analysisId={view.analysisId} fileId={advisoryFile.fileId} requests={advisoryRequests.current} drafts={advisoryDrafts.current} readOnly={historical} /> : null}
      importResults={!historical && nativeState?.analysisId === view.analysisId ? nativeState.results : undefined}
      error={workspaceError} onAdd={handleSelectMaterials} onProcess={startMaterialAnalysis}
      onBack={() => historical ? returnFromHistory() : navigate({ kind: 'home' })} /> : <section className="status-card">
        {workspaceQuery.isError ? <><p role="alert">无法读取材料清单，已导入的材料不会因此删除。</p>
          <button type="button" onClick={() => workspaceQuery.refetch()}>重试读取</button></>
          : <p>正在读取材料清单…</p>}
      </section>;
  } else if (view.kind === 'processing') {
    content = processingQuery.isError ? <section className="status-card">
      <p role="alert">进度读取失败：{safeErrorCode(processingQuery.error)}。任务可能仍在后台运行，请先重试读取，不要重复提交。</p>
      <button type="button" onClick={() => processingQuery.refetch()}>重试读取</button>
      <TaskControls task={task} files={workspaceQuery.data?.files} onResume={resumeTask} onRetry={retryOriginalTask} />
    </section> : (
      <ProcessingPanel
        status={task.data?.business_status ?? processingQuery.data?.status ?? 'uploaded'}
        progress={processingQuery.data?.progress ?? 0}
        task={task} files={workspaceQuery.data?.files} onResume={resumeTask} onRetry={retryOriginalTask}
        onMatching={() => navigate({ kind: 'matching', analysisId: view.analysisId })}
      />
    );
  } else if (view.kind === 'matching') {
    const readError = matchingQuery.isError ? <section className="status-card">
      <p role="alert">匹配事项读取失败：{safeErrorCode(matchingQuery.error)}。已保存的材料和决定仍在。</p>
      <button type="button" onClick={() => matchingQuery.refetch()}>重试读取</button>
    </section> : null;
    content = matchingQuery.data ? <>
      {readError}
      {matchingError || matchingQuery.data.candidates.length === 0 ? <section className="status-card">
        <p>提交结果尚未核实，请先读取已保存的状态；不会重复提交刚才的决定。</p>
        <button type="button" disabled={submittingMatch} onClick={reconcileMatching}>核对已保存的结果</button>
        {matchingError === 'MATCH_STATE_INCONSISTENT' ? <p role="alert">没有待确认人员，但分析状态尚未更新。请稍后再次核对。</p> : null}
      </section> : null}
      <MatchingReview
        candidates={matchingQuery.data.candidates}
        currentCompanyId={matchingQuery.data.current_company_id}
        employeeRecordOptions={matchingQuery.data.employee_record_options}
        draftCache={matchingDrafts.current}
        submitting={submittingMatch || matchingQuery.isError || Boolean(matchingError)}
        taskActive={task.active}
        error={matchingError}
        onDecision={handleMatchDecision}
      />
    </> : readError ?? (
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
        onSelectMaterials={historical ? undefined : handleSelectMaterials}
        selectingMaterials={selectingMaterials || analysisRunning}
      />
    );
  } else if (view.kind === 'employees' && employees && api) {
    content = (
      <EmployeeWorkspace
        key={view.analysisId}
        analysisId={view.analysisId}
        api={api}
        initialPayload={employees}
        initialQuery={employeeQuery}
        onSnapshot={(payload, query) => { setEmployees(payload); setEmployeeQuery(query); }}
        onSelectEmployee={handleSelectEmployee}
        onBack={() => { void refreshDashboard(view.analysisId); }}
      />
    );
  } else if (view.kind === 'employee' && employee) {
    content = (
      <><EmployeeDetail
        payload={employee}
        onSelectFinding={handleSelectFinding}
        onBack={() => { if (historicalOrigin.current.kind === 'company-employee' && historical) returnFromHistory(); else void returnToLedger(view.analysisId); }}
      />
      {historical && api && company.companyId && record ? <EmployeeFactWorkbench
        key={`${company.companyId}/${view.analysisId}/${record.id}/history`}
        companyId={company.companyId} analysisId={view.analysisId} recordId={record.id} api={api}
        factDrafts={factRevisionDrafts.current} factRequests={factRevisionRequests.current}
        decisionDrafts={assessmentDecisionDrafts.current} decisionRequests={assessmentDecisionRequests.current}
        reevaluationRequests={reevaluationRequests.current} readOnly onReevaluated={() => undefined} /> : null}</>
    );
  } else if (view.kind === 'report') {
    const reportCompanyId = historical ? history.selected?.company_id : company.companyId;
    content = api && reportCompanyId ? <ReportVersions key={`${reportCompanyId}/${view.analysisId}`} api={api}
      companyId={reportCompanyId} analysisId={view.analysisId} selections={reportSelections.current} livePayload={report ?? undefined}
      onBack={() => historical ? void refreshDashboard(view.analysisId) : navigate({ kind: 'reports' })} /> : report ? (
      <><p>报告草稿（读取时生成，尚未锁定版本）</p><ReportView
        payload={report}
        onBack={() => historical ? void refreshDashboard(view.analysisId) : navigate({ kind: 'reports' })}
      /></>
    ) : <p>正在读取旧记录实时草稿，尚未生成保存版本。</p>;
  } else if (view.kind === 'finding' && finding) {
    content = (
      <FindingDetail
        key={finding.id}
        finding={finding}
        onReview={historical ? undefined : handleFindingReview}
        onReload={reloadFinding}
        backLabel={canonicalFindingOrigin.current || findingOrigin.current?.kind === 'employee' ? '返回员工详情' : '返回风险概览'}
        onBack={() => { void returnFromFinding(); }}
      />
    );
  } else if (view.kind === 'error') {
    content = (
      <section className="error-panel" role="alert">
        <p className="eyebrow">本机分析未继续</p>
        <h2>无法继续本次分析</h2>
        <p>{describeOperationError(view.code)}</p>
        <div className="dashboard-actions">
          {view.analysisId ? (
            <button type="button" className="primary-action" onClick={handleRetryAnalysis}>
              重新分析
            </button>
          ) : null}
          <button type="button" className="secondary-action" onClick={handleSelectMaterials}>
            选择其他材料
          </button>
          <button type="button" className="text-action" onClick={returnHome}>
            返回风险概览
          </button>
        </div>
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
    <main className={`app-shell${view.kind === 'home' || view.kind === 'company-employees' ? ' workbench-shell' : ''}`}>
      <header className="hero">
        <p className="eyebrow">QIAN LABOR DESKTOP</p>
        <h1>企安用工</h1>
        <p className="subtitle">本地优先劳动用工风险体检</p>
        {providerStatus ? <button type="button" className="text-action hero-settings-action"
          onClick={openSettings}>模型设置</button> : null}
      </header>
      {api ? <><nav aria-label="企业工作区" className="company-nav print-hidden">
        {(['home', 'company-employees', 'materials', 'reports', 'settings'] as const).map((kind, index) =>
          <button type="button" key={kind} aria-current={view.kind === kind || kind === 'company-employees' && view.kind === 'company-employee' ? 'page' : undefined} onClick={() => {
            if (kind === 'settings') { openSettings(); return; }
            setHistorical(false);
            if (kind === 'materials') { navigate(activeCurrentId ? { kind, analysisId: activeCurrentId } : { kind: 'home' }); return; }
            navigate({ kind });
          }}>{['工作台', '员工', '材料', '报告', '设置'][index]}</button>)}
      </nav><label className="company-picker print-hidden">当前企业<select aria-label="当前企业" value={company.companyId ?? ''} disabled={selectingMaterials || company.busy || submittingMatch || reviewBusy}
        onChange={e => { setHistorical(false); setRecord(null); setEmployee(null); setFinding(null); setDashboard(null); setResumePaths(null); setMatchingError(null); settingsReturn.current = { kind: 'home' };
          navigate({ kind: 'home' }); void company.select(e.target.value || null); }}>
        <option value="">选择企业 / 新建档案</option>{company.companies.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}
      </select></label></> : null}
      {company.error ? <section role="alert"><p>企业档案读取或保存未完成：{describeOperationError(company.error)}</p>
        <button type="button" disabled={company.busy} onClick={() => company.uncertainCreation ? company.reconcileCreation() : company.reloadSetup()}>核对企业档案</button>
        {company.uncertainCreation ? <button type="button" disabled={company.busy} onClick={() => company.retryCreation()}>重试原企业建档请求</button> : null}</section> : null}
      {company.current.isError ? <section role="alert"><p>员工档案读取失败，仍显示上次成功读取的列表，尚未应用新筛选。</p><button type="button" onClick={() => company.current.refetch()}>重试读取员工档案</button></section> : null}
      {company.current.isPlaceholderData ? <p role="status">正在按筛选更新，暂时显示上次成功读取的列表。</p> : null}
      {currentDashboard.isError ? <p role="alert">当前风险读取失败。<button type="button" onClick={() => currentDashboard.refetch()}>重试读取当前风险</button></p> : null}
      {pendingCorpus ? <p role="alert">当前材料建立结果尚未核实。<button type="button" onClick={reconcileCurrentMaterials}>核对当前材料</button></p> : null}
      {resumePaths ? <button type="button" onClick={() => { const paths = resumePaths; setResumePaths(null); void handleSelected(paths); }}>继续导入已选材料</button> : null}
      {nativeState?.phase === 'unknown' ? <section role="alert"><p>导入结果尚未核实，部分材料可能已经保存；请先核对材料清单。</p>
        {nativeState.error ? <p>核对未完成，请再次读取。</p> : null}
        <button type="button" disabled={nativeImport.busy(company.companyId)} onClick={() => company.companyId && void nativeImport.reconcile(company.companyId)}>核对导入结果</button>
      </section> : null}
      {nativeState?.phase === 'retry' ? <section><p>已核对当前材料清单，无法按文件名判断每一项是否成功。可以明确重试本次选择，相同内容会去重。</p>
        <button type="button" disabled={nativeImport.busy(company.companyId) || analysisRunning} onClick={() => company.companyId && void nativeImport.submit(company.companyId, nativeState.analysisId, nativeState.paths)}>重试本次导入</button>
      </section> : null}
      {nativeState && view.kind !== 'materials' && view.kind !== 'settings' ? <button type="button" onClick={() => { if (activeCurrentId === nativeState.analysisId) { setHistorical(false); navigate({ kind: 'materials', analysisId: activeCurrentId }); } }}>查看导入材料</button> : null}
      {!['processing', 'materials', 'settings'].includes(view.kind) && !historical && task.target ? <>
        {task.error && !task.pending ? <p role="alert">任务状态读取失败。<button type="button" onClick={task.read}>重试读取任务</button></p> : null}
        {task.pending || task.run ? <p className="task-notice" role="status"><span>{task.pending ? '操作结果尚未核实，不会重复提交。' : TASK_LABELS[task.run!.state]}</span>
          <button type="button" onClick={() => activeCurrentId && enterCurrentProcessing(activeCurrentId)}>查看处理进度</button>
          {task.pending ? <button type="button" disabled={task.busy} onClick={task.reconcile}>核对处理状态</button> : null}</p> : null}
      </> : null}
      {historicalView && history.selected ? <section aria-label="历史浏览上下文">
        <p>{view.kind === 'matching' && history.selected.relation === 'unbound' ? '旧记录人员匹配（尚未归属企业）' : '历史体检（只读浏览，不计入当前统计）'}</p>
        <p>{history.selected.name} · 历史日期：{history.selected.created_at ? history.selected.created_at.replace('T', ' ').slice(0, 19) : '尚未读取到原始日期'}</p>
        <AssessmentScopeNotice scope={history.selected.assessment_scope} />
        {legacyEligible && view.kind === 'materials' ? <section aria-label="继续旧记录分析">
          <p>仅处理此旧记录已有材料，可能使用模型额度；不会导入当前企业材料，也不会改变当前统计。</p>
          <button type="button" disabled={task.locked || task.active || task.canResume} onClick={processLegacy}>继续分析此旧记录</button>
        </section> : null}
        <button type="button" onClick={returnFromHistory}>{historicalOrigin.current.kind === 'company-employee' ? '返回员工档案' : '返回历史列表'}</button>
      </section> : null}
      {!historical && company.current.data?.current_analysis?.stale && ['dashboard', 'report', 'employee'].includes(view.kind)
        ? <p role="status">当前材料尚待完成分析，下列已保存结果尚未按最新材料更新。</p> : null}
      {workspaceError && view.kind === 'home' ? <section role="alert" className="error-panel">
        <p>{describeOperationError(workspaceError)}</p>
        {retryRead.current ? <button type="button" onClick={() => retryRead.current?.()}>重试读取结果</button> : null}
      </section> : null}
      {workspaceError && ['finding', 'employee', 'employees', 'dashboard', 'company-employee', 'company-employees', 'reports'].includes(view.kind) ? <p role="alert">结果读取失败：{describeOperationError(workspaceError)}已保存的操作不会重做，可以再次点击返回重试。</p> : null}
      {workspaceError && view.kind !== 'home' && retryRead.current ? <button type="button" onClick={() => retryRead.current?.()}>重试读取结果</button> : null}
      {resultReadBusy ? <p role="status">正在读取最新结果…</p> : null}
      <div className="workspace-content">{content}</div>
    </main>
  );
}
