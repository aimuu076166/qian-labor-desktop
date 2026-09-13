import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { describeOperationDiagnostic, describeOperationError } from '../src/lib/errorMessages';
import { MaterialWorkspace } from '../src/features/workspace/MaterialWorkspace';

it.each([0, 19])('shows rejected output alongside %s accepted facts and only retries on an explicit click', (count) => {
  const onProcess = vi.fn();
  render(<MaterialWorkspace configured busy={false} onAdd={() => {}} onProcess={onProcess} onBack={() => {}}
    payload={{ analysis: { id: 'a', name: '虚构体检', company_display_name: '虚构企业', status: 'partial' },
      files: [{ id: 'f', filename: 'synthetic.docx', status: 'partial', progress: 100, detected_kind: 'docx',
        classified_kind: 'contract', error_code: 'PROCESSING_INCOMPLETE', size_bytes: 20, fact_count: count,
        needs_reextraction: true, unreceived_count: 2, unreceived: [
          { reason: 'unsupported_fact_type', index: '19' },
          { reason: 'invalid_value', index: '20' },
        ] }] }} />);
  expect(screen.getByText(/2 项模型输出未接收/)).toBeInTheDocument();
  expect(screen.getByText(/不支持的事实类型/)).toBeInTheDocument();
  expect(screen.getByText(/事实值不符合要求/)).toBeInTheDocument();
  expect(screen.getByText(/不能据此判断没有风险/)).toBeInTheDocument();
  expect(screen.queryByText(/部分旧版材料/)).toBeNull();
  expect(onProcess).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '开始分析' }));
  expect(onProcess).toHaveBeenCalledTimes(1);
});

it('keeps historical unreceived warnings visible without offering a retry or echoing unknown reasons', () => {
  render(<MaterialWorkspace readOnly configured busy={false} onAdd={() => {}} onProcess={() => {}} onBack={() => {}}
    payload={{ analysis: { id: 'a', name: '虚构历史', company_display_name: '虚构企业', status: 'partial' },
      files: [{ id: 'f', filename: 'synthetic.docx', status: 'partial', progress: 100, detected_kind: 'docx',
        classified_kind: 'contract', error_code: null, size_bytes: 20, unreceived_count: 1,
        unreceived: [{ reason: 'synthetic-private-marker', index: '0' }] }] }} />);
  expect(screen.getByText(/1 项模型输出未接收/)).toBeInTheDocument();
  expect(screen.getByText(/输出未通过校验，需人工核对/)).toBeInTheDocument();
  expect(screen.queryByText(/synthetic-private-marker/)).toBeNull();
  expect(screen.queryByRole('button', { name: '开始分析' })).toBeNull();
  expect(screen.queryByText(/可点击.*重试该文件/)).toBeNull();
});

it('explains a local redaction failure without asking the user to change their API key', () => {
  render(<MaterialWorkspace configured busy={false} onAdd={() => {}} onProcess={() => {}} onBack={() => {}}
    payload={{ analysis: { id: 'a', name: '虚构体检', company_display_name: '虚构企业', status: 'partial' },
      files: [{ id: 'f', filename: 'synthetic.png', status: 'failed', progress: 0, detected_kind: 'image',
        classified_kind: 'unknown', error_code: 'AI_LOCAL_REDACTION_FAILED', size_bytes: 20 }] }} />);
  expect(screen.getByText(/本地识别或脱敏未完成/)).toBeInTheDocument();
  expect(screen.getByText(/这份材料未发送给模型/)).toBeInTheDocument();
});

it('distinguishes request timeouts and never renders unknown raw error content', () => {
  expect(describeOperationError('AI_TIMEOUT')).toContain('模型响应超时');
  expect(describeOperationError('AI_TIMEOUT')).not.toContain('欠费');
  expect(describeOperationError('DESKTOP_REQUEST_TIMEOUT')).toContain('操作可能仍在后台进行');
  expect(describeOperationError('synthetic-private-path-and-secret')).not.toContain('synthetic-private');
});

it('renders only the safe provider diagnostic category and bounded fields', () => {
  expect(describeOperationDiagnostic({ category: 'json' })).toContain('有效 JSON');
  expect(describeOperationDiagnostic({ category: 'http', status_code: 429, attempt: 2 })).toContain('HTTP 429');
  expect(describeOperationDiagnostic({ category: 'synthetic-secret-marker' })).toBeNull();
});

it('separates field type errors from conflicts and unsupported types', () => {
  expect(describeOperationDiagnostic({ category: 'schema', validation_type: 'invalid_type' })).toContain('字段类型不符合约定');
  expect(describeOperationDiagnostic({ category: 'schema', validation_type: 'conflict' })).toContain('内容矛盾');
  expect(describeOperationDiagnostic({ category: 'semantic', validation_type: 'unsupported' })).toContain('不可识别');
  expect(describeOperationDiagnostic({ category: 'schema', validation_type: 'missing_field' })).toContain('缺少必需字段');
});

it('states the zhipu channel copy without redaction wording', () => {
  render(<MaterialWorkspace configured busy={false} onAdd={() => {}} onProcess={() => {}} onBack={() => {}}
    payload={{ analysis: { id: 'a', name: '虚构体检', company_display_name: '虚构企业', status: 'created' },
      files: [] }} />);
  expect(screen.getByText(/材料内容将发送至你配置的智谱官方通道/)).toBeInTheDocument();
  expect(screen.queryByText(/脱敏/)).toBeNull();
});

it('shows zero extracted facts as an actionable empty result rather than evidence of no risk', () => {
  render(<MaterialWorkspace configured busy={false} onAdd={() => {}} onProcess={() => {}} onBack={() => {}}
    payload={{ analysis: { id: 'a', name: '虚构旧体检', company_display_name: '虚构企业', status: 'completed' },
      files: [{ id: 'f', filename: 'synthetic.csv', status: 'processed', progress: 100, detected_kind: 'table',
        classified_kind: 'unknown', error_code: 'AI_NO_SUPPORTED_FACTS', size_bytes: 20, fact_count: 0 }] }} />);
  expect(screen.getByRole('columnheader', { name: '当前提取事实' })).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: '0' })).toBeInTheDocument();
  expect(screen.getByText(/不能据此判断没有风险/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开始分析' })).toBeEnabled();
});
