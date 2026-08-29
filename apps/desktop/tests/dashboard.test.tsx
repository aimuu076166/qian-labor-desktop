import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProcessingPanel } from '../src/features/processing/ProcessingPanel';
import { DashboardView } from '../src/features/dashboard/DashboardView';
import { FindingDetail } from '../src/features/findings/FindingDetail';

describe('desktop analysis views', () => {
  it('renders processing states in Chinese business language', () => {
    render(<ProcessingPanel status="extracting" progress={58} />);
    expect(screen.getByRole('heading', { name: '正在分析企业材料' })).toBeInTheDocument();
    expect(screen.getByText('正在提取用工事实')).toBeInTheDocument();
    expect(screen.getByText('58%')).toBeInTheDocument();
  });

  it('distinguishes suspected risk from insufficient data and never calls missing data safe', () => {
    render(
      <DashboardView
        summary={{
          analysis_id: 'analysis-one',
          status: 'completed',
          employee_count: 3,
          finding_count: 2,
          high_count: 1,
          medium_count: 0,
          insufficient_data_count: 1,
        }}
        findings={[
          {
            id: 'risk-one',
            rule_id: 'CONTRACT_MISSING_ACTIVE',
            title: '在职员工合同材料缺失',
            severity: 'high',
            assessment_status: 'suspected_risk',
            requires_human_review: true,
          },
          {
            id: 'gap-one',
            rule_id: 'MATERIAL_COVERAGE_LOW',
            title: '关键材料覆盖率不足',
            severity: 'info',
            assessment_status: 'insufficient_data',
            requires_human_review: false,
          },
        ]}
        onSelectFinding={vi.fn()}
      />,
    );

    expect(screen.getByText('高风险')).toBeInTheDocument();
    expect(screen.getByText('资料不足')).toBeInTheDocument();
    expect(screen.getByText('疑似风险')).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();
    expect(screen.queryByText('无风险')).not.toBeInTheDocument();
  });

  it('shows traceable source file and locator in finding detail', () => {
    render(
      <FindingDetail
        finding={{
          id: 'risk-one',
          analysis_id: 'analysis-one',
          rule_id: 'CONTRACT_MISSING_ACTIVE',
          title: '在职员工合同材料缺失',
          severity: 'high',
          assessment_status: 'suspected_risk',
          requires_human_review: true,
          summary: '本次材料中未发现书面劳动合同，请人工核对。',
          sources: [
            {
              id: 'source-one',
              file_id: 'file-one',
              file_name: '虚构劳动合同.docx',
              locator_type: 'cell',
              location: { sheet: '员工台账', row: 2, cell: 'B2' },
              excerpt: '完全虚构来源摘录',
            },
          ],
        }}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByText('虚构劳动合同.docx')).toBeInTheDocument();
    expect(screen.getByText(/员工台账/)).toBeInTheDocument();
    expect(screen.getByText(/第 2 行/)).toBeInTheDocument();
    expect(screen.getByText(/B2/)).toBeInTheDocument();
    expect(screen.getByText('完全虚构来源摘录')).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();
  });
});
