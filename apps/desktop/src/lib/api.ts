import { invoke } from '@tauri-apps/api/core';

export type DesktopBackendInfo = {
  baseUrl: string;
  token: string;
};

export async function getDesktopBackendInfo(): Promise<DesktopBackendInfo> {
  return invoke<DesktopBackendInfo>('desktop_backend_info');
}

export function createDesktopApi(
  info: DesktopBackendInfo,
  fetchImpl: typeof fetch = fetch,
) {
  return async function desktopApi(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set('X-Qian-Desktop-Token', info.token);
    return fetchImpl(`${info.baseUrl}${path}`, {
      ...init,
      headers,
    });
  };
}
