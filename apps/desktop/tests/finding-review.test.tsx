import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { FindingDetail, type FindingDetailData } from '../src/features/findings/FindingDetail';
import { companyServer, renderCompany, syntheticFinding, json } from './company-fixture';

const finding: FindingDetailData = {
  id: 'synthetic-finding', analysis_id: 'synthetic-analysis', rule_id: 'R01', title: '虚构风险',
  severity: 'high', assessment_status: 'insufficient_data', requires_human_review: true,
  summary: '仅供合成测试', sources: [], review_status: 'open', version: 2, reviews: [],
  missing_fact_types: ['contract_signed'], recommended_actions: ['补充签署材料'],
};

describe('human finding review', () => {
  it('requires GET reconciliation after an uncertain review and preserves the explanation', async () => {
    const save = vi.fn().mockRejectedValue(new Error('DESKTOP_REQUEST_TIMEOUT'));
    const reload = vi.fn().mockResolvedValue(undefined);
    render(<FindingDetail finding={finding} onBack={vi.fn()} onReview={save} onReload={reload} />);
    fireEvent.change(screen.getByLabelText('复核说明'), { target: { value: '合成核对说明' } });
    fireEvent.click(screen.getByRole('button', { name: '保存复核' }));
    expect(await screen.findByRole('button', { name: '刷新详情' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '保存复核' })).toBeDisabled();
    expect(screen.getByLabelText('复核说明')).toHaveValue('合成核对说明');
    fireEvent.click(screen.getByRole('button', { name: '刷新详情' }));
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    expect(save).toHaveBeenCalledOnce();
  });
  it('keeps retired finding history readable without offering a new review or calling it resolved', () => {
    render(<FindingDetail finding={{ ...finding, is_current: false,
      reviews: [{ status: 'reviewed', note: '此前核对合成材料', created_at: '2026-01-01T10:00:00' }] }}
      onBack={vi.fn()} onReview={vi.fn()} />);
    expect(screen.getByText(/已退出当前结果/)).toBeInTheDocument();
    expect(screen.getByText(/不代表风险已解决/)).toBeInTheDocument();
    expect(screen.getByText('此前核对合成材料')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存复核' })).not.toBeInTheDocument();
  });
  it('locks conflicting company changes during review while navigation remains available and refreshes results', async () => {
    let finish!: (response: Response) => void;
    const server = companyServer({ override: async (path, init) => path.endsWith('/reviews') && init?.method === 'POST'
      ? new Promise<Response>(resolve => { finish = resolve; }) : undefined });
    renderCompany(server);
    fireEvent.click(await screen.findByRole('button', { name: '合成合同事项待核查' }));
    fireEvent.change(await screen.findByLabelText('复核说明'), { target: { value: '合成确认' } });
    fireEvent.click(screen.getByRole('button', { name: '保存复核' }));
    expect(screen.getByLabelText('当前企业')).toBeDisabled();
    expect(screen.getByRole('button', { name: '材料' })).toBeEnabled();
    expect(server.request).toHaveBeenCalledWith('/api/findings/finding-one/reviews', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ expected_version: 2, status: 'open', note: '合成确认' }),
    }));
    await act(async () => { finish(json({ ...syntheticFinding, version: 3 })); });
    expect(await screen.findByText('复核已保存。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /返回风险概览/ }));
    expect(await screen.findByLabelText('搜索员工')).toBeInTheDocument();
    expect(server.request.mock.calls.filter(([path]) => path.endsWith('/dashboard')).length).toBeGreaterThan(1);
  });
  it('requires a reason and sends an explicit versioned decision', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    render(<FindingDetail finding={finding} onBack={vi.fn()} onReview={save} />);
    expect(screen.getByText(/没有可追溯的直接材料依据/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存复核' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('复核决定'), { target: { value: 'needs_material' } });
    fireEvent.change(screen.getByLabelText('复核说明'), { target: { value: '  等待补充合同  ' } });
    fireEvent.click(screen.getByRole('button', { name: '保存复核' }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({
      expected_version: 2, status: 'needs_material', note: '等待补充合同',
    }));
  });

  it('keeps an unsaved explanation when a stale version is rejected', async () => {
    render(<FindingDetail finding={finding} onBack={vi.fn()}
      onReview={vi.fn().mockRejectedValue(new Error('DESKTOP_REVIEW_STALE'))} />);
    fireEvent.change(screen.getByLabelText('复核说明'), { target: { value: '人工核对说明' } });
    fireEvent.click(screen.getByRole('button', { name: '保存复核' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('依据已更新');
    expect(screen.getByLabelText('复核说明')).toHaveValue('人工核对说明');
    expect(screen.getByRole('button', { name: '保存复核' })).toBeDisabled();
  });

  it('shows saved decisions without changing the original assessment', () => {
    render(<FindingDetail finding={{ ...finding, review_status: 'dismissed', version: 3,
      reviews: [{ status: 'dismissed', note: '人工确认属于误报', created_at: '2026-09-07T10:00:00' }],
    }} onBack={vi.fn()} onReview={vi.fn()} />);
    expect(screen.getByText('人工确认属于误报')).toBeInTheDocument();
    expect(screen.getByText('资料不足')).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();
  });
});
