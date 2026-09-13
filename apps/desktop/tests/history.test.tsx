import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HistoryView } from '../src/features/workspace/HistoryView';
import { useHistoryWorkspace, type HistoricalAnalysis } from '../src/features/workspace/useHistoryWorkspace';
import { companyServer, renderCompany, json } from './company-fixture';

function ScopedHistory({ api, onOpen }: { api: (path: string) => Promise<Response>; onOpen: (item: HistoricalAnalysis) => Promise<void> }) {
  const controller = useHistoryWorkspace(api, { id: 'synthetic-company', display_name: '虚构企业', version: 0, created_at: '' }, true);
  return <HistoryView controller={controller} onOpen={onOpen} onMatching={() => {}} onBack={() => {}} />;
}

describe('analysis history', () => {
  it.each(['history', 'new', 'settings', 'materials'].flatMap(action => [true, false].map(success => ({ action, success }))))(
    'ignores late canonical detail reads after $action navigation (success=$success)', async ({ action, success }) => {
    let finish!: (response: Response) => void;
    const server = companyServer({ override: async path => path.endsWith('/employees/record-one')
      ? new Promise<Response>(resolve => { finish = resolve; }) : undefined });
    renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '查看合成员**详情' }));
    await waitFor(() => expect(finish).toBeDefined());
    if (action === 'history') {
      fireEvent.click(screen.getByRole('button', { name: '报告' }));
      fireEvent.click(await screen.findByRole('button', { name: '历史体检' }));
      fireEvent.click(await screen.findByRole('button', { name: '打开合成历史体检' }));
      await screen.findByText(/历史材料只读/);
    } else if (action === 'materials') {
      fireEvent.click(screen.getByRole('button', { name: '材料' }));
      await screen.findByText('synthetic-contract.docx');
    } else if (action === 'new') {
      fireEvent.change(screen.getByLabelText('当前企业'), { target: { value: '' } });
      await screen.findByLabelText('企业名称');
    } else {
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      await screen.findByRole('heading', { name: '连接智谱 GLM' });
    }
    await act(async () => { finish(success ? json({ ...server.record, current_binding: null, historical_bindings: [], bindings: [] })
      : json({}, 500)); });
    expect(action === 'new' ? screen.getByLabelText('企业名称') : action === 'settings'
      ? screen.getByRole('heading', { name: '连接智谱 GLM' }) : screen.getByText('synthetic-contract.docx')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '合成员**' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('paginates local analyses and opens the selected ID without creating a batch', async () => {
    const api = vi.fn(async (path: string) => {
      const page = path.includes('page=2&') ? 2 : 1;
      return new Response(JSON.stringify({ page, pages: 2, total: 26, items: [{
        id: `synthetic-${page}`, name: `虚构体检${page}`, company_display_name: '虚构企业',
        status: 'partial', file_count: 5, employee_count: 10, created_at: '2026-09-07T10:00:00',
      }] }));
    });
    const open = vi.fn().mockResolvedValue(undefined);
    render(<QueryClientProvider client={new QueryClient()}><ScopedHistory api={api} onOpen={open} /></QueryClientProvider>);
    expect(await screen.findByText('虚构体检1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    fireEvent.click(await screen.findByRole('button', { name: '打开虚构体检2' }));
    await waitFor(() => expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: 'synthetic-2' })));
    expect(api).toHaveBeenCalledWith('/api/company-workspaces/synthetic-company/analyses?relation=historical&page=2&page_size=25', expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('keeps the history visible after a selected analysis cannot be opened', async () => {
    const api = vi.fn(async () => new Response(JSON.stringify({ page: 1, pages: 1, total: 1,
      items: [{ id: 'synthetic', name: '虚构体检', company_display_name: '', status: 'failed',
        file_count: 1, employee_count: 0, created_at: '2026-09-07T10:00:00' }] })));
    const open = vi.fn().mockRejectedValue(new Error('DESKTOP_ANALYSIS_NOT_FOUND'));
    render(<QueryClientProvider client={new QueryClient()}><ScopedHistory api={api} onOpen={open} /></QueryClientProvider>);
    fireEvent.click(await screen.findByRole('button', { name: '打开虚构体检' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('DESKTOP_ANALYSIS_NOT_FOUND');
    expect(screen.getByText('虚构体检')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '打开虚构体检' })).toBeEnabled();
  });
});
