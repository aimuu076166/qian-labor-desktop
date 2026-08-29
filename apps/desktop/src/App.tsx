import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import {
  DashboardView,
  type DashboardFinding,
  type DashboardSummary,
} from './features/dashboard/DashboardView';
import { FindingDetail, type FindingDetailData } from './features/findings/FindingDetail';
import { ImportPanel } from './features/import/ImportPanel';
import { ProcessingPanel } from './features/processing/ProcessingPanel';
import {
  createDesktopApi,
  getDesktopBackendInfo,
  type DesktopBackendInfo,
} from './lib/api';

type DesktopRequest = (path: string, init?: RequestInit) => Promise<Response>;
type BackendLoader = () => Promise<DesktopBackendInfo>;
type ApiFactory = (info: DesktopBackendInfo) => DesktopRequest;

type DesktopView =
  | { kind: 'booting' }
  | { kind: 'import' }
  | { kind: 'processing'; analysisId: string }
  | { kind: 'dashboard'; analysisId: string }
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
};

type AppProps = {
  backendLoader?: BackendLoader;
  apiFactory?: ApiFactory;
};

const TERMINAL_PROCESSING_STATES = new Set([
  'completed',
  'partial',
  'matching_review',
  'failed',
]);

async function readJson<T>(request: Promise<Response>): Promise<T> {
  const response = await request;
  if (!response.ok) throw new Error(`DESKTOP_API_${response.status}`);
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
}: AppProps = {}) {
  const [view, setView] = useState<DesktopView>({ kind: 'booting' });
  const [api, setApi] = useState<DesktopRequest | null>(null);
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [finding, setFinding] = useState<FindingDetailData | null>(null);
  const dashboardLoadRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    backendLoader()
      .then((info) => {
        if (cancelled) return;
        setApi(() => apiFactory(info));
        setView({ kind: 'import' });
      })
      .catch((error: unknown) => {
        if (!cancelled) setView({ kind: 'error', code: safeErrorCode(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [apiFactory, backendLoader]);

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

  useEffect(() => {
    if (!api || view.kind !== 'processing') return;
    const processingStatus = processingQuery.data?.status;
    if (!processingStatus || !TERMINAL_PROCESSING_STATES.has(processingStatus)) return;
    if (processingStatus === 'failed') {
      setView({ kind: 'error', code: 'DESKTOP_ANALYSIS_FAILED' });
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
    if (!api) {
      setView({ kind: 'error', code: 'DESKTOP_BACKEND_NOT_READY' });
      return;
    }
    try {
      setDashboard(null);
      setFinding(null);
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
    if (!api || view.kind !== 'dashboard') return;
    try {
      const payload = await readJson<FindingDetailData>(api(`/api/findings/${findingId}`));
      setFinding(payload);
      setView({ kind: 'finding', analysisId: view.analysisId, findingId });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
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
      dashboardLoadRef.current = null;
      setView({ kind: 'import' });
    } catch (error) {
      setView({ kind: 'error', code: safeErrorCode(error) });
    }
  }

  function returnToImport() {
    setDashboard(null);
    setFinding(null);
    dashboardLoadRef.current = null;
    setView(api ? { kind: 'import' } : { kind: 'booting' });
  }

  let content;
  if (view.kind === 'booting') {
    content = (
      <section className="status-card" aria-label="desktop-status">
        <span className="status-dot" aria-hidden="true" />
        <p>正在准备本机分析服务…</p>
      </section>
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
  } else if (view.kind === 'dashboard' && dashboard) {
    content = (
      <DashboardView
        summary={dashboard.summary}
        findings={dashboard.findings}
        onSelectFinding={handleSelectFinding}
        onDeleteAnalysis={handleDeleteAnalysis}
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
      </header>
      {content}
    </main>
  );
}
