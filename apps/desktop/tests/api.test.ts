import { describe, expect, it, vi } from 'vitest';
import { createDesktopApi, readJson, safeErrorCode } from '../src/lib/api';

describe('desktop API client', () => {
  it.each(['WORKSPACE_VERSION_CONFLICT', 'HISTORICAL_FILE_MISSING'])('preserves safe workspace contract code %s', async code => {
    await expect(readJson(Promise.resolve(new Response(JSON.stringify({ detail: { code } }), { status: 409 })))).rejects.toThrow(code);
    expect(safeErrorCode(new Error(code))).toBe(code);
    expect(safeErrorCode(new Error('WORKSPACE_secret/path'))).toBe('DESKTOP_OPERATION_FAILED');
  });
  it('preserves streamed JSON and safe HTTP error codes after buffering the complete response', async () => {
    const bytes = new TextEncoder().encode(JSON.stringify({ detail: { code: 'AI_RATE_LIMIT' }, label: '虚构' }));
    const fetchImpl = vi.fn().mockResolvedValue(new Response(new ReadableStream({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    } }), { status: 429 }));
    const api = createDesktopApi({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' }, fetchImpl as typeof fetch);
    await expect(readJson(api('/api/analyses'))).rejects.toThrow('AI_RATE_LIMIT');
    fetchImpl.mockResolvedValueOnce(new Response(null, { status: 204 }));
    expect((await api('/api/analyses')).status).toBe(204);
  });

  it('rejects oversized responses without returning a truncated result', async () => {
    const cancel = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) { controller.enqueue(new Uint8Array(16 * 1024 * 1024 + 1)); }, cancel,
    })));
    const api = createDesktopApi({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' }, fetchImpl as typeof fetch);
    await expect(readJson(api('/api/analyses'))).rejects.toThrow('DESKTOP_RESPONSE_TOO_LARGE');
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it('settles its deadline even if the fetch implementation does not reject on abort', async () => {
    const fetchImpl = vi.fn(() => new Promise<Response>(() => {}));
    const api = createDesktopApi({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' }, fetchImpl as typeof fetch, 10);
    await expect(api('/api/analyses')).rejects.toThrow('DESKTOP_REQUEST_TIMEOUT');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it.each([200, 429])('bounds a stalled response body after receiving HTTP %s headers', async (status) => {
    const cancel = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel }), { status }));
    const api = createDesktopApi({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' }, fetchImpl as typeof fetch, 10);
    await expect(readJson(api('/api/analyses'))).rejects.toThrow('DESKTOP_REQUEST_TIMEOUT');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it('propagates caller cancellation through response body consumption and releases its reader', async () => {
    const cancel = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel })));
    const controller = new AbortController();
    const api = createDesktopApi({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' }, fetchImpl as typeof fetch);
    const response = readJson(api('/api/analyses', { signal: controller.signal }));
    const rejection = expect(response).rejects.toThrow('DESKTOP_REQUEST_CANCELLED');
    await new Promise(resolve => setTimeout(resolve, 0));
    controller.abort();
    await rejection;
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it('allows the provider its 30 second connection-test deadline', async () => {
    vi.useFakeTimers();
    try {
      let aborted = false;
      const fetchImpl = vi.fn((_url: string | URL | Request, init?: RequestInit) => new Promise<Response>((resolve) => {
        init?.signal?.addEventListener('abort', () => { aborted = true; resolve(new Response('{}')); });
        setTimeout(() => resolve(new Response('{}')), 25_000);
      }));
      const request = createDesktopApi({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' }, fetchImpl as typeof fetch)(
        '/api/provider/connection-test', { method: 'POST' });
      await vi.advanceTimersByTimeAsync(25_000);
      await request;
      expect(aborted).toBe(false);
      expect(fetchImpl).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
  it('preserves actionable provider and matching codes without exposing raw errors', async () => {
    await expect(readJson(Promise.resolve(new Response(JSON.stringify({
      detail: { code: 'MATCH_EMPLOYEE_NUMBER_EXISTS' },
    }), { status: 409 })))).rejects.toThrow('MATCH_EMPLOYEE_NUMBER_EXISTS');
    expect(safeErrorCode(new Error('AI_TIMEOUT'))).toBe('AI_TIMEOUT');
    expect(safeErrorCode(new Error('secret material text'))).toBe('DESKTOP_OPERATION_FAILED');
  });

  it('times out stalled requests without silently resubmitting them', async () => {
    const fetchImpl = vi.fn((_url: string | URL | Request, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    }));
    const api = createDesktopApi({ baseUrl: 'http://127.0.0.1:43123', token: 'synthetic-token' }, fetchImpl as typeof fetch, 10);
    await expect(api('/api/analyses', { method: 'POST' })).rejects.toThrow('DESKTOP_REQUEST_TIMEOUT');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
  it('sends the per-launch desktop token on every business request', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    const api = createDesktopApi(
      { baseUrl: 'http://127.0.0.1:43123', token: 'memory-only-token' },
      fetchImpl as unknown as typeof fetch,
    );

    await api('/api/status', { headers: { Accept: 'application/json' } });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:43123/api/status');
    const headers = new Headers(init.headers);
    expect(headers.get('Accept')).toBe('application/json');
    expect(headers.get('X-Qian-Desktop-Token')).toBe('memory-only-token');
  });
});
