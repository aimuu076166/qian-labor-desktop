import { describe, expect, it, vi } from 'vitest';
import { createDesktopApi } from '../src/lib/api';

describe('desktop API client', () => {
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
