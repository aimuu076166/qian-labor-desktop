import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { EmployeeWorkspace } from '../src/features/employees/EmployeeWorkspace';
import type { EmployeeLedgerPayload } from '../src/features/employees/EmployeeLedger';
import { companyServer, renderCompany, syntheticFinding, json } from './company-fixture';

it('returns from reviewed source detail by canonical identity, refreshes the same page, and never repeats review after failed read', async () => {
  let reviewed = false; let failRead = false;
  const server = companyServer({ override: async (path, init) => {
    if (path.endsWith('/reviews') && init?.method === 'POST') {
      reviewed = true; failRead = true; return json({ ...syntheticFinding, version: 3, review_status: 'dismissed' });
    }
    if (path.endsWith('/employees/snapshot-one') && reviewed) {
      if (failRead) { failRead = false; throw new Error('DESKTOP_CONNECTION_FAILED'); }
      return json({ employee: { ...server.record, id: 'snapshot-one', employment_status: 'active', match_status: 'confirmed' }, findings: [] });
    }
  } }); renderCompany(server);
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '员工' }));
  fireEvent.change(await screen.findByLabelText('搜索员工'), { target: { value: 'SYN-1' } });
  await waitFor(() => expect(server.request.mock.calls.some(([path]) => path.includes('search=SYN-1'))).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(await screen.findByText(/第 2 \/ 2 页/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  await screen.findByRole('heading', { name: '合成员**' });
  fireEvent.click(await screen.findByRole('button', { name: /合成合同事项待核查/ }));
  fireEvent.change(await screen.findByLabelText('复核说明'), { target: { value: '合成确认误报' } });
  fireEvent.change(screen.getByLabelText('复核决定'), { target: { value: 'dismissed' } });
  fireEvent.click(screen.getByRole('button', { name: '保存复核' }));
  expect(await screen.findByText('复核已保存。')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /返回员工详情/ }));
  expect(await screen.findByRole('alert')).toHaveTextContent('读取失败');
  expect(screen.getByText('复核已保存。')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /返回员工详情/ }));
  expect(await screen.findByText('该员工暂无可列示事项。')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '返回员工台账' }));
  expect(await screen.findByLabelText('搜索员工')).toHaveValue('SYN-1');
  expect(await screen.findByText(/第 2 \/ 2 页/)).toBeInTheDocument();
  expect(server.request.mock.calls.filter(([path, init]) => path.endsWith('/reviews') && init?.method === 'POST')).toHaveLength(1);
  expect(server.request.mock.calls.filter(([path]) => path.endsWith('/employees/record-one')).length).toBeGreaterThan(1);
});

const initial: EmployeeLedgerPayload = { total: 30, page: 1, pages: 2, page_size: 25, department_options: [],
  items: [{ id: 'synthetic', employee_number: 'SYN-001', masked_name: '虚构员工', department: '未分组',
    job_title: null, employment_status: 'active', match_status: 'confirmed', risk_counts: { high: 0, medium: 0 },
    insufficient_data_count: 0, requires_human_review_count: 0, material_coverage: 0.5 }] };

it('paginates by GET and preserves the applied filter and snapshot for returning from detail', async () => {
  const api = vi.fn<(path: string, init?: RequestInit) => Promise<Response>>()
    .mockResolvedValue(new Response(JSON.stringify({ ...initial, page: 2 })));
  const saved = vi.fn();
  render(<EmployeeWorkspace analysisId="batch" api={api} initialPayload={initial} initialQuery="SYN"
    onSnapshot={saved} onBack={vi.fn()} onSelectEmployee={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }), 'SYN'));
  expect(api.mock.calls[0][0]).toBe('/api/analyses/batch/employees?page=2&page_size=25&query=SYN');
  expect(screen.getByText('第 2 / 2 页')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
});

it('resets to page one on explicit search and retains current data and input after read failure', async () => {
  const api = vi.fn().mockRejectedValueOnce(new Error('DESKTOP_CONNECTION_FAILED'))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...initial, total: 1, pages: 1 })));
  render(<EmployeeWorkspace analysisId="batch" api={api} initialPayload={{ ...initial, page: 2 }} initialQuery=""
    onSnapshot={vi.fn()} onBack={vi.fn()} onSelectEmployee={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('搜索员工'), { target: { value: 'SYN-001' } });
  expect(api).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '搜索' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('仍显示上次成功读取的列表');
  expect(screen.getByText('虚构员工')).toBeInTheDocument();
  expect(screen.getByLabelText('搜索员工')).toHaveValue('SYN-001');
  fireEvent.click(screen.getByRole('button', { name: '搜索' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(api.mock.calls.every(([path]) => path === '/api/analyses/batch/employees?page=1&page_size=25&query=SYN-001')).toBe(true);
});

it('does not publish a late employee response after leaving the workspace', async () => {
  let finish!: (response: Response) => void;
  const api = vi.fn(() => new Promise<Response>(resolve => { finish = resolve; }));
  const saved = vi.fn();
  const { unmount } = render(<EmployeeWorkspace analysisId="batch" api={api} initialPayload={initial} initialQuery=""
    onSnapshot={saved} onBack={vi.fn()} onSelectEmployee={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(screen.getByRole('button', { name: '查看虚构员工详情' })).toBeDisabled();
  unmount();
  await act(async () => { finish(new Response(JSON.stringify({ ...initial, page: 2 }))); });
  expect(saved).not.toHaveBeenCalled();
});

it('keeps canonical page and server filter when returning from detail and settings', async () => {
  const server = companyServer(); renderCompany(server);
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '员工' }));
  fireEvent.change(await screen.findByLabelText('搜索员工'), { target: { value: 'SYN-1' } });
  await waitFor(() => expect(server.request.mock.calls.some(([path]) => path.includes('search=SYN-1'))).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(await screen.findByText(/第 2 \/ 2 页/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回员工台账' }));
  expect(await screen.findByLabelText('搜索员工')).toHaveValue('SYN-1');
  expect(await screen.findByText(/第 2 \/ 2 页/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  expect(await screen.findByLabelText('搜索员工')).toHaveValue('SYN-1');
  expect(await screen.findByText(/第 2 \/ 2 页/)).toBeInTheDocument();
});
