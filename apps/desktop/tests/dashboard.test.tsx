import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProcessingPanel } from '../src/features/processing/ProcessingPanel';
import { DashboardView } from '../src/features/dashboard/DashboardView';
import { FindingDetail } from '../src/features/findings/FindingDetail';

const MATERIALS_SCOPE = {
  identifier: 'labor_materials_v1' as const,
  display_label: '用工材料体检 v1',
  excluded_rule_codes: ['R09', 'R13', 'R14', 'R15'],
  excluded_rule_ids: ['WAGE_PAYMENT', 'OVERTIME', 'ATTENDANCE', 'SEVERANCE_CALCULATION'],
  not_evaluated_reasons: {
    R16: '现有离职后记录为工资、考勤、社保合并事实，无法核实社保延续来源，本项暂未评估。',
  },
  payroll_evaluated: false,
  attendance_evaluated: false,
  settlement_document_label: '结算文件（final_pay），不要求工资表，不计算工资或补偿',
};

describe('desktop analysis views', () => {
  it('shows the server-provided materials scope and the R16 limitation without exposing rule ids', () => {
    render(<DashboardView summary={{ analysis_id: 'synthetic', status: 'completed', employee_count: 1,
      finding_count: 0, high_count: 0, medium_count: 0, insufficient_data_count: 0 }} findings={[]}
      overview={{ company_name: '虚构企业', assessment_scope: MATERIALS_SCOPE,
        summary: { coverage_rate: 1, affected_employee_count: 0, requires_human_review_count: 0,
          deadline_30_count: 0, classification_pending: false }, categories: [],
        material_coverage: { overall: 1, classification_pending: false, items: [] } }} />);

    expect(screen.getByText(/用工材料体检 v1/)).toBeInTheDocument();
    expect(screen.getByText(/工资核算与考勤核算未评估/)).toBeInTheDocument();
    expect(screen.getByText(/无法核实社保延续来源/)).toBeInTheDocument();
    expect(screen.getByText(/结算文件.*不要求工资表/)).toBeInTheDocument();
    expect(screen.queryByText(/WAGE_PAYMENT|R09|R13|R14|R15/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^无风险$/)).not.toBeInTheDocument();
  });

  it('labels a legacy scope as historical and full', () => {
    render(<DashboardView summary={{ analysis_id: 'legacy', status: 'completed', employee_count: 1,
      finding_count: 0, high_count: 0, medium_count: 0, insufficient_data_count: 0 }} findings={[]}
      overview={{ company_name: '虚构企业', assessment_scope: { ...MATERIALS_SCOPE,
        identifier: 'legacy_full_v1', display_label: '历史完整规则范围 v1', excluded_rule_codes: [],
        excluded_rule_ids: [], not_evaluated_reasons: {}, payroll_evaluated: true, attendance_evaluated: true },
        summary: { coverage_rate: 1, affected_employee_count: 0, requires_human_review_count: 0,
          deadline_30_count: 0, classification_pending: false }, categories: [],
        material_coverage: { overall: 1, classification_pending: false, items: [] } }} />);
    expect(screen.getByText(/历史完整评估范围/)).toBeInTheDocument();
  });

  it('states that assessment scope is unconfirmed when the payload omits it', () => {
    render(<DashboardView summary={{ analysis_id: 'unknown', status: 'completed', employee_count: 1,
      finding_count: 0, high_count: 0, medium_count: 0, insufficient_data_count: 0 }} findings={[]}
      overview={{ company_name: '虚构企业', summary: { coverage_rate: 1, affected_employee_count: 0,
        requires_human_review_count: 0, deadline_30_count: 0, classification_pending: false }, categories: [],
        material_coverage: { overall: 1, classification_pending: false, items: [] } }} />);
    expect(screen.getByText(/评估范围尚未确认/)).toBeInTheDocument();
    expect(screen.getByText(/无法确认工资、考勤项目是否已评估/)).toBeInTheDocument();
  });
  it('does not label an unknown workforce scope as zero coverage or inapplicable', () => {
    render(<DashboardView summary={{ analysis_id: 'synthetic', status: 'partial', employee_count: 1,
      finding_count: 0, high_count: 0, medium_count: 0, insufficient_data_count: 0 }} findings={[]}
      overview={{ company_name: '虚构企业', summary: { coverage_rate: 0, affected_employee_count: 0,
        requires_human_review_count: 0, deadline_30_count: 0, classification_pending: false }, categories: [],
        material_coverage: { overall: 0, classification_pending: false, scope_pending: true,
          items: [{ code: 'contract', label: '劳动合同', covered: 0, applicable: 0, rate: 0,
            not_applicable: false, classification_pending: false, scope_pending: true }] } }} />);
    expect(screen.getByText(/员工范围或在职状态尚未确认/)).toBeInTheDocument();
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
    expect(screen.queryByText('不适用')).not.toBeInTheDocument();
    expect(screen.getByText('适用范围待确认')).toBeInTheDocument();
  });
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
        overview={{
          company_name: '完全虚构企业',
          summary: {
            coverage_rate: 0.6,
            affected_employee_count: 2,
            requires_human_review_count: 1,
            deadline_30_count: 0,
            classification_pending: false,
          },
          categories: [{ code: 'contract', label: '劳动合同', count: 2 }],
          material_coverage: {
            overall: 0.6,
            classification_pending: false,
            items: [
              {
                code: 'contract',
                label: '劳动合同',
                covered: 3,
                applicable: 5,
                rate: 0.6,
                not_applicable: false,
                classification_pending: false,
              },
            ],
          },
        }}
        onSelectFinding={vi.fn()}
        onOpenEmployees={vi.fn()}
        onOpenReport={vi.fn()}
      />,
    );

    expect(screen.getByText('高风险')).toBeInTheDocument();
    expect(screen.getByText('资料不足')).toBeInTheDocument();
    expect(screen.getByText('疑似风险')).toBeInTheDocument();
    expect(screen.getByText('需要人工复核')).toBeInTheDocument();
    expect(screen.getByText('材料覆盖率')).toBeInTheDocument();
    expect(screen.getByText('60%')).toBeInTheDocument();
    expect(screen.getByText('受影响员工')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看员工台账' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '生成体检报告' })).toBeInTheDocument();
    expect(screen.queryByText('无风险')).not.toBeInTheDocument();
  });

  it('labels partial analyses honestly instead of always claiming completion', () => {
    render(
      <DashboardView
        summary={{
          analysis_id: 'analysis-partial',
          status: 'partial',
          employee_count: 1,
          finding_count: 0,
          high_count: 0,
          medium_count: 0,
          insufficient_data_count: 0,
        }}
        findings={[]}
        onSelectFinding={vi.fn()}
      />,
    );

    expect(screen.getByText('部分完成')).toBeInTheDocument();
    expect(screen.queryByText('分析完成')).not.toBeInTheDocument();
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
