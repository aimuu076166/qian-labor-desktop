import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it } from 'vitest';
import { companyServer, renderCompany, json } from './company-fixture';
import { ContractAdvisoryPanel, type AdvisoryDrafts, type AdvisoryRequests } from '../src/features/workspace/ContractAdvisoryPanel';

const observation = { id: 'clause-one', run_id: 'run-one', file_id: 'file', filename: 'synthetic-contract.docx', employee_id: 'snapshot-one',
  assignment_status: 'assigned', match_candidate_id: null, issue: '核对工资发放日期约定', checks: ['核对双方签署的条款'], next_action: '保留双方确认记录',
  unverified_references: ['模型参考名称（未核验）'], source: { location: { paragraph: 2 }, excerpt: 'SYN-1 工资发放日由双方书面确认。', provenance: 'locally_located' },
  requires_source_review: false, legal_verification: 'unverified', version: 0, handling: null, is_latest: true, read_only: false };

it('explicitly retries only the original clause UUID and CAS after timeout and version drift', async () => {
  const sent: string[] = []; let version = 0;
  const requests: AdvisoryRequests = new Map(), drafts: AdvisoryDrafts = new Map();
  const api = async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { sent.push(String(init.body)); version = 3; throw new Error('DESKTOP_REQUEST_TIMEOUT'); }
    return json({ observations: [{ ...observation, version }], runs: [], total: 1, run_total: 0, page: 1, pages: 1, read_only: false, request_handling: null });
  };
  const panel = () => <ContractAdvisoryPanel companyId="company-a" analysisId="analysis-a" api={api} requests={requests} drafts={drafts} />;
  const view = render(panel());
  fireEvent.change(await screen.findByLabelText('条款处理说明'), { target: { value: '原始合成说明' } });
  fireEvent.click(screen.getByRole('button', { name: '保存条款处理' })); await screen.findByRole('alert');
  view.unmount(); render(panel());
  const retry = await screen.findByRole('button', { name: '重试原条款处理请求' });
  fireEvent.click(retry); await waitFor(() => expect(sent).toHaveLength(2));
  expect(sent[1]).toBe(sent[0]); expect(JSON.parse(sent[1]).expected_version).toBe(0);
  expect(requests.size).toBe(1); expect(screen.getByRole('button', { name: '保存条款处理' })).toBeDisabled();
});

it('keeps timed-out clause identity through repeated absent receipts and Settings until the original late commit', async () => {
  let body: Record<string, unknown> | null = null; let committed = false;
  const server = companyServer({ override: async (path, init) => {
    if (!path.includes('/contract-advisories')) return undefined;
    if (init?.method === 'POST') { body = JSON.parse(String(init.body)); throw new Error('DESKTOP_REQUEST_TIMEOUT'); }
    const saved = committed ? { ...body, observation_id: observation.id, version: 1 } : null;
    return json({ observations: [{ ...observation, version: committed ? 1 : 0, handling: saved }], runs: [],
      total: 1, run_total: 0, page: 1, pages: 1, read_only: false, request_handling: path.includes('request_id=') ? saved : null });
  } });
  renderCompany(server); await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  fireEvent.change(await screen.findByLabelText('条款处理说明'), { target: { value: '原合成处理意见' } });
  fireEvent.click(screen.getByRole('button', { name: '保存条款处理' }));
  for (let i = 0; i < 2; i++) {
    const check = await screen.findByRole('button', { name: '核对条款处理结果' });
    await waitFor(() => expect(check).toBeEnabled()); fireEvent.click(check);
    await waitFor(() => expect(check).toBeEnabled());
    expect(screen.getByRole('button', { name: '保存条款处理' })).toBeDisabled();
  }
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  expect(await screen.findByLabelText('条款处理说明')).toHaveValue('原合成处理意见');
  committed = true; fireEvent.click(screen.getByRole('button', { name: '核对条款处理结果' }));
  await screen.findByText('条款处理已核实保存。');
  expect(server.request.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});

it('handles actual employee clause, retains uncertain request across Settings and material navigation, reconciles by GET without duplicate POST', async () => {
  let saved: { id: string; observation_id: string; version: number; decision: string; reason: string } | null = null;
  const server = companyServer({ override: async (path, init) => {
    if (!path.includes('/contract-advisories')) return undefined;
    if (init?.method === 'POST') { const body = JSON.parse(String(init.body)); saved = { ...body, observation_id: observation.id, version: 1 }; throw new Error('DESKTOP_CONNECTION_FAILED'); }
    return json({ observations: [{ ...observation, version: saved ? 1 : 0, handling: saved }],
      runs: [{ id: 'run-one', file_id: 'file', filename: 'synthetic-contract.docx', execution_status: 'completed', is_latest: true }],
      total: 1, run_total: 1, page: 1, pages: 1, page_size: 20, read_only: false,
      request_handling: path.includes('request_id=') ? saved : null });
  } });
  renderCompany(server);
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  expect(await screen.findByText('核对工资发放日期约定')).toBeInTheDocument();
  expect(screen.getByText(/不计入.*高.*中/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('条款处理说明'), { target: { value: '正在核对合成签署文本' } });
  fireEvent.click(screen.getByRole('button', { name: '保存条款处理' }));
  await screen.findByText(/提交结果尚未核实/);
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  expect(await screen.findByRole('button', { name: '核对条款处理结果' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '返回员工台账' }));
  fireEvent.click(screen.getByRole('button', { name: '材料' }));
  fireEvent.click(await screen.findByRole('button', { name: /查看.*条款观察/ }));
  const panel = await screen.findByRole('region', { name: '合同条款观察' });
  expect(within(panel).getByRole('button', { name: '保存条款处理' })).toBeDisabled();
  fireEvent.click(within(panel).getByRole('button', { name: '核对条款处理结果' }));
  await screen.findByText('条款处理已核实保存。');
  await waitFor(() => expect(server.request.mock.calls.some(([path]) => path.includes(`request_id=${saved!.id}`))).toBe(true));
  expect(server.request.mock.calls.filter(([path, init]) => path.includes('/contract-advisories') && init?.method === 'POST')).toHaveLength(1);
  expect(server.request.mock.calls.some(([path]) => path.endsWith('/process'))).toBe(false);
});

it('material history is read-only and unreadable or absent review never presents all clear', async () => {
  const server = companyServer({ override: async path => path.includes('/contract-advisories') ? json({
    observations: [], runs: [{ id: 'run-one', file_id: 'file', filename: 'synthetic.docx', execution_status: 'unreadable', is_latest: true }],
    total: 0, run_total: 1, page: 1, pages: 1, page_size: 20, read_only: true, request_handling: null }) : undefined });
  renderCompany(server);
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '材料' }));
  fireEvent.click(await screen.findByRole('button', { name: /查看.*条款观察/ }));
  expect(await screen.findByText(/无法读取条款内容/)).toBeInTheDocument();
  expect(screen.getByText(/未返回条款观察.*不代表/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '保存条款处理' })).not.toBeInTheDocument();
});

it('can reconcile a request that settles after its original employee panel unmounts', async () => {
  let finish!: () => void;
  let saved: { id: string; observation_id: string; version: number; decision: string; reason: string } | null = null;
  const server = companyServer({ override: async (path, init) => {
    if (!path.includes('/contract-advisories')) return undefined;
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body));
      await new Promise<void>(resolve => { finish = resolve; });
      saved = { ...body, observation_id: observation.id, version: 1 }; throw new Error('DESKTOP_CONNECTION_FAILED');
    }
    return json({ observations: [{ ...observation, version: saved ? 1 : 0, handling: saved }],
      runs: [], total: 1, run_total: 0, page: 1, pages: 1, read_only: false,
      request_handling: path.includes('request_id=') ? saved : null });
  } });
  renderCompany(server);
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  fireEvent.change(await screen.findByLabelText('条款处理说明'), { target: { value: '合成核对' } });
  fireEvent.click(screen.getByRole('button', { name: '保存条款处理' }));
  await waitFor(() => expect(finish).toBeTypeOf('function'));
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  await screen.findByRole('button', { name: '核对条款处理结果' });
  await act(async () => { finish(); });
  expect(screen.getByRole('button', { name: '核对条款处理结果' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: '核对条款处理结果' }));
  await screen.findByText('条款处理已核实保存。');
});

it('preserves unsaved reason and decision across employee, Settings and material views without submitting', async () => {
  const server = companyServer({ override: async path => path.includes('/contract-advisories') ? json({
    observations: [observation], runs: [], total: 1, run_total: 0, page: 1, pages: 1,
    read_only: false, request_handling: null }) : undefined });
  renderCompany(server);
  await screen.findByText('已建档员工 1 人');
  fireEvent.click(screen.getByRole('button', { name: '查看合成员**详情' }));
  fireEvent.change(await screen.findByLabelText('条款处理说明'), { target: { value: '尚未提交的合成核对说明' } });
  fireEvent.change(screen.getByLabelText('条款处理决定'), { target: { value: 'dismissed' } });
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  expect(await screen.findByLabelText('条款处理说明')).toHaveValue('尚未提交的合成核对说明');
  expect(screen.getByLabelText('条款处理决定')).toHaveValue('dismissed');
  fireEvent.click(screen.getByRole('button', { name: '材料' }));
  fireEvent.click(await screen.findByRole('button', { name: /查看.*条款观察/ }));
  expect(await screen.findByLabelText('条款处理说明')).toHaveValue('尚未提交的合成核对说明');
  expect(screen.getByLabelText('条款处理决定')).toHaveValue('dismissed');
  expect(server.request.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false);
  expect(screen.queryByRole('button', { name: '核对条款处理结果' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '放弃未保存修改' }));
  expect(screen.getByLabelText('条款处理说明')).toHaveValue('');
  expect(screen.getByLabelText('条款处理决定')).toHaveValue('checking');
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '返回工作区' }));
  expect(await screen.findByLabelText('条款处理说明')).toHaveValue('');
});

it('isolates drafts by company, analysis and observation while retaining each original draft', async () => {
  const drafts: AdvisoryDrafts = new Map(); const requests: AdvisoryRequests = new Map();
  let row = observation;
  const api = async () => json({ observations: [row], runs: [], total: 1, run_total: 0, page: 1, pages: 1,
    read_only: false, request_handling: null });
  const panel = (companyId: string, analysisId: string) => <ContractAdvisoryPanel companyId={companyId}
    analysisId={analysisId} api={api} drafts={drafts} requests={requests} />;
  const view = render(panel('company-a', 'analysis-a'));
  fireEvent.change(await screen.findByLabelText('条款处理说明'), { target: { value: '仅属于第一条观察' } });
  fireEvent.change(screen.getByLabelText('条款处理决定'), { target: { value: 'dismissed' } });
  view.rerender(panel('company-b', 'analysis-a'));
  await waitFor(() => expect(screen.getByLabelText('条款处理说明')).toHaveValue(''));
  fireEvent.change(screen.getByLabelText('条款处理说明'), { target: { value: '第二公司草稿' } });
  view.rerender(panel('company-a', 'analysis-b'));
  await waitFor(() => expect(screen.getByLabelText('条款处理说明')).toHaveValue(''));
  row = { ...observation, id: 'clause-two' };
  view.rerender(panel('company-a', 'analysis-a'));
  await waitFor(() => expect(screen.getByLabelText('条款处理说明')).toHaveValue(''));
  view.unmount(); row = observation;
  render(panel('company-a', 'analysis-a'));
  expect(await screen.findByLabelText('条款处理说明')).toHaveValue('仅属于第一条观察');
  expect(screen.getByLabelText('条款处理决定')).toHaveValue('dismissed');
  expect(requests.size).toBe(0);
});

it.each([false, true])('clears drafts only after confirmed save, retaining an unknown unsaved request (unknown=%s)', async unknown => {
  const drafts: AdvisoryDrafts = new Map(); const requests: AdvisoryRequests = new Map();
  let saved: { id: string; observation_id: string; version: number; decision: string; reason: string } | null = null;
  let posts = 0;
  const server = companyServer({ override: async (path, init) => {
    if (!path.includes('/contract-advisories')) return undefined;
    if (init?.method === 'POST') {
      posts += 1;
      if (unknown) throw new Error('DESKTOP_CONNECTION_FAILED');
      saved = { ...JSON.parse(String(init.body)), observation_id: observation.id, version: 1 };
      return json({ ...observation, version: 1, handling: saved });
    }
    return json({ observations: [{ ...observation, handling: saved, version: saved ? 1 : 0 }], runs: [],
      total: 1, run_total: 0, page: 1, pages: 1, read_only: false, request_handling: null });
  } });
  render(<ContractAdvisoryPanel companyId="company-a" analysisId="analysis-a" api={server.request}
    drafts={drafts} requests={requests} />);
  fireEvent.change(await screen.findByLabelText('条款处理说明'), { target: { value: '待人工保存的合成说明' } });
  fireEvent.click(screen.getByRole('button', { name: '保存条款处理' }));
  if (unknown) {
    await screen.findByRole('alert');
    expect(drafts.size).toBe(1); expect(requests.size).toBe(1);
    expect(screen.getByRole('button', { name: '放弃未保存修改' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '核对条款处理结果' }));
    await screen.findByText(/暂未查到记录/);
    expect(drafts.size).toBe(1); expect(requests.size).toBe(1);
    expect(screen.getByRole('button', { name: '保存条款处理' })).toBeDisabled();
    expect(screen.getByLabelText('条款处理说明')).toHaveValue('待人工保存的合成说明');
  } else {
    await screen.findByText('条款处理已保存。');
    expect(drafts.size).toBe(0); expect(requests.size).toBe(0);
  }
  expect(posts).toBe(1);
});
