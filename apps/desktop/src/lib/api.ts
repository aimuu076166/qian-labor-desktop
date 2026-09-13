import { invoke } from '@tauri-apps/api/core';

export type DesktopBackendInfo = {
  baseUrl: string;
  token: string;
};

export type AssessmentScope = {
  identifier: string;
  display_label: string;
  excluded_rule_codes: string[];
  excluded_rule_ids: string[];
  not_evaluated_reasons: Record<string, string>;
  payroll_evaluated: boolean;
  attendance_evaluated: boolean;
  settlement_document_label: string;
};

export type AssessmentRevision = {
  input_revision: string;
  result_revision: string | null;
  evaluated_input_revision: string | null;
  fresh: boolean;
  check_date: string;
  check_date_explicit: boolean;
  evaluated_at: string | null;
  read_only: boolean;
  report_review_revision: string;
  availability: 'none' | 'available';
  completeness: 'pending' | 'partial' | 'complete';
};

export async function getDesktopBackendInfo(): Promise<DesktopBackendInfo> {
  return invoke<DesktopBackendInfo>('desktop_backend_info');
}

export function createDesktopApi(
  info: DesktopBackendInfo,
  fetchImpl: typeof fetch = fetch,
  timeoutMs = 15_000,
) {
  return async function desktopApi(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set('X-Qian-Desktop-Token', info.token);
    const controller = new AbortController();
    let timedOut = false;
    const abortError = () => new Error(timedOut ? 'DESKTOP_REQUEST_TIMEOUT' : 'DESKTOP_REQUEST_CANCELLED');
    let rejectAbort: (error: Error) => void = () => {};
    const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject; });
    const onAbort = () => rejectAbort(abortError());
    controller.signal.addEventListener('abort', onAbort, { once: true });
    const cancel = () => controller.abort();
    if (init.signal?.aborted) throw new Error('DESKTOP_REQUEST_CANCELLED');
    init.signal?.addEventListener('abort', cancel, { once: true });
    // Connection testing waits for the provider's own 30-second deadline.
    const deadline = path === '/api/provider/connection-test' ? Math.max(timeoutMs, 40_000) : timeoutMs;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, deadline);
    try {
      return await Promise.race([aborted, (async () => {
        const response = await fetchImpl(`${info.baseUrl}${path}`, { ...init, headers, signal: controller.signal });
        if (controller.signal.aborted) {
          void response.body?.cancel().catch(() => {});
          throw abortError();
        }
        if (!response.body) return response;
        // The deadline includes the entire JSON body, not just the response headers.
        const reader = response.body.getReader();
        const cancelBody = () => { void reader.cancel().catch(() => {}); };
        controller.signal.addEventListener('abort', cancelBody, { once: true });
        try {
          const chunks: Uint8Array[] = [];
          let bytes = 0;
          while (true) {
            const item = await reader.read();
            if (controller.signal.aborted) throw abortError();
            if (item.done) break;
            bytes += item.value.byteLength;
            if (bytes > 16 * 1024 * 1024) {
              cancelBody();
              throw new Error('DESKTOP_RESPONSE_TOO_LARGE');
            }
            chunks.push(item.value);
          }
          const body = new Uint8Array(bytes);
          let offset = 0;
          for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
          return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
        } finally {
          controller.signal.removeEventListener('abort', cancelBody);
          reader.releaseLock();
        }
      })()]);
    } catch (error) {
      if (error instanceof Error && error.message === 'DESKTOP_RESPONSE_TOO_LARGE') throw error;
      throw new Error(timedOut ? 'DESKTOP_REQUEST_TIMEOUT'
        : controller.signal.aborted ? 'DESKTOP_REQUEST_CANCELLED' : 'DESKTOP_CONNECTION_FAILED');
    } finally {
      clearTimeout(timer);
      controller.signal.removeEventListener('abort', onAbort);
      init.signal?.removeEventListener('abort', cancel);
    }
  };
}

export async function readJson<T>(request: Promise<Response>): Promise<T> {
  const response = await request;
  if (!response.ok) {
    let code = '';
    try {
      const payload = await response.json() as { detail?: { code?: unknown } };
      if (typeof payload.detail?.code === 'string') code = payload.detail.code;
    } catch { /* Use the safe HTTP status fallback for non-JSON responses. */ }
    throw new Error(/^(?:AI|DESKTOP|MATCH|WORKSPACE|HISTORICAL|ADVISORY|FACT|ASSESSMENT|TASK|PROCESSING|ANALYSIS)_[A-Z0-9_]+$/.test(code) ? code : `DESKTOP_API_${response.status}`);
  }
  return await response.json() as T;
}

export function safeErrorCode(error: unknown): string {
  return error instanceof Error && /^(?:AI|DESKTOP|MATCH|WORKSPACE|HISTORICAL|ADVISORY|FACT|ASSESSMENT|TASK|PROCESSING|ANALYSIS)_[A-Z0-9_]+$/.test(error.message)
    ? error.message : 'DESKTOP_OPERATION_FAILED';
}
