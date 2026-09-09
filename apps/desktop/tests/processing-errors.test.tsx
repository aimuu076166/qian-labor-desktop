import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { describeOperationDiagnostic, describeOperationError } from '../src/lib/errorMessages';
import { MaterialWorkspace } from '../src/features/workspace/MaterialWorkspace';

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

it('shows zero extracted facts as an actionable empty result rather than evidence of no risk', () => {
  render(<MaterialWorkspace configured busy={false} onAdd={() => {}} onProcess={() => {}} onBack={() => {}}
    payload={{ analysis: { id: 'a', name: '虚构旧体检', company_display_name: '虚构企业', status: 'completed' },
      files: [{ id: 'f', filename: 'synthetic.csv', status: 'processed', progress: 100, detected_kind: 'table',
        classified_kind: 'unknown', error_code: 'AI_NO_SUPPORTED_FACTS', size_bytes: 20, fact_count: 0 }] }} />);
  expect(screen.getByRole('columnheader', { name: '已提取事实' })).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: '0' })).toBeInTheDocument();
  expect(screen.getByText(/不能据此判断没有风险/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开始分析' })).toBeEnabled();
});
