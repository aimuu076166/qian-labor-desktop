import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { MaterialWorkspace, type WorkspacePayload } from '../src/features/workspace/MaterialWorkspace';
import { CompanyWorkbench } from '../src/features/workspace/CompanyWorkbench';
import { FindingDetail } from '../src/features/findings/FindingDetail';

it('shows channel, human review, quota and incomplete warnings before explicit start', () => {
  const process = vi.fn();
  const payload = { analysis: { id: 'synthetic', name: '合成材料', company_display_name: '虚构企业', status: 'partial' },
    files: [{ id: 'file', filename: 'synthetic.docx', status: 'partial', progress: 100, detected_kind: 'docx', classified_kind: 'contract',
      error_code: 'PROCESSING_INCOMPLETE', size_bytes: 10, fact_count: 2, needs_reextraction: true, warnings: ['embedded_images_need_vision'] }] } satisfies WorkspacePayload;
  render(<MaterialWorkspace payload={payload} configured busy={false} onAdd={vi.fn()} onBack={vi.fn()} onProcess={process} />);
  expect(screen.getByText(/智谱官方通道/)).toBeInTheDocument();
  expect(screen.getByText(/AI 输出仍需人工复核/)).toBeInTheDocument();
  expect(screen.getAllByText(/消耗.*额度/)).toHaveLength(2);
  expect(screen.getByText(/包括已成功部分/)).toBeInTheDocument();
  expect(screen.getByText(/内嵌图片尚未提取/)).toBeInTheDocument();
  expect(screen.getByText('部分处理')).toBeInTheDocument();
  expect(screen.getByText('2')).toBeInTheDocument();
  expect(process).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
  expect(process).toHaveBeenCalledOnce();
});

it('blocks material mutations while a task is active and explains the wait', () => {
  const onAdd = vi.fn();
  const onProcess = vi.fn();
  const payload = { analysis: { id: 'synthetic', name: '合成材料', company_display_name: '虚构企业', status: 'extracting' },
    files: [{ id: 'file', filename: 'synthetic.docx', status: 'extracting', progress: 42, detected_kind: 'docx', classified_kind: 'contract',
      error_code: null, size_bytes: 10, fact_count: 1 }] } satisfies WorkspacePayload;
  render(<MaterialWorkspace payload={payload} configured busy={false} processDisabled processing onAdd={onAdd} onBack={vi.fn()} onProcess={onProcess} />);
  expect(screen.getByRole('status')).toHaveTextContent('当前任务正在处理');
  expect(screen.getByRole('button', { name: '添加材料' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '正在处理…' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '添加材料' }));
  expect(onAdd).not.toHaveBeenCalled();
});

it('blocks the home matching entry point while the task is active', () => {
  const onMatching = vi.fn();
  render(<CompanyWorkbench payload={{ company: { id: 'company', display_name: '虚构企业', version: 0, created_at: '2026-09-01' },
    current_analysis: { analysis_id: 'analysis', company_id: 'company', role: 'current', assessment_profile: 'labor_materials_v1', status: 'extracting', current_stage: 'extracting', progress: 40,
      analysis_version: 1, company_version: 0, stale: true, pending_identity_count: 1 }, employees: [], enrolled_employee_count: 0,
    pending_identity_count: 1, total: 0, page: 1, page_size: 25, pages: 1 }}
    filters={{ search: '', severity: '', assessment_state: '', page: 1, page_size: 25 }} onFilters={vi.fn()}
    onSelectEmployee={vi.fn()} onImport={vi.fn()} busy onMatching={onMatching} onOverview={vi.fn()} />);
  expect(screen.getByRole('button', { name: '确认员工匹配' })).toBeDisabled();
  expect(screen.getByRole('status')).toHaveTextContent('任务正在处理');
  fireEvent.click(screen.getByRole('button', { name: '确认员工匹配' }));
  expect(onMatching).not.toHaveBeenCalled();
});

it('source detail labels all provenance states without claiming model text as original', () => {
  render(<FindingDetail onBack={vi.fn()} finding={{ id: 'synthetic', analysis_id: 'synthetic', rule_id: 'R01', title: '合成事项',
    severity: 'low', assessment_status: 'insufficient_data', requires_human_review: true, summary: '需核对材料',
    sources: ['locally_located', 'unlocated_needs_review', 'legacy_unverified'].map((provenance, i) => ({
      id: String(i), file_id: String(i), file_name: `synthetic-${i}.docx`, locator_type: 'document', location: {}, excerpt: '', provenance,
    })) }} />);
  expect(screen.getByText(/本地已定位/)).toBeInTheDocument();
  expect(screen.getByText(/未定位，需核对原材料/)).toBeInTheDocument();
  expect(screen.getByText(/旧版来源未经本地核验/)).toBeInTheDocument();
  expect(screen.getByText(/定位仅证明文字所在位置/)).toBeInTheDocument();
});

it('labels fact counts as current or historical extraction output', () => {
  const payload = { analysis: { id: 'synthetic', name: '合成材料', company_display_name: '虚构企业', status: 'processed' },
    files: [{ id: 'file', filename: 'synthetic.xlsx', status: 'processed', progress: 100, detected_kind: 'xlsx', classified_kind: 'assessment',
      error_code: null, size_bytes: 10, fact_count: 30 }] } satisfies WorkspacePayload;
  const { rerender } = render(<MaterialWorkspace payload={payload} configured busy={false} onAdd={vi.fn()} onBack={vi.fn()} onProcess={vi.fn()} />);
  expect(screen.getByText('当前提取事实')).toBeInTheDocument();
  rerender(<MaterialWorkspace payload={payload} configured busy={false} readOnly onAdd={vi.fn()} onBack={vi.fn()} onProcess={vi.fn()} />);
  expect(screen.getByText('该批次提取事实')).toBeInTheDocument();
});
