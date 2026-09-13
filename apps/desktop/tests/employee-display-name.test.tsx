import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { EmployeeDisplayName } from '../src/features/employees/EmployeeDisplayName';

it('corrects only the local display name with the record version, without model calls', async () => {
  const api = vi.fn().mockResolvedValue(new Response(JSON.stringify({ masked_name: '甲测试', version: 1 })));
  const saved = vi.fn();
  render(<EmployeeDisplayName name="***" version={0} companyId="company" recordId="employee" api={api} disabled={false} onSaved={saved} />);
  fireEvent.change(screen.getByLabelText('本地员工显示名'), { target: { value: '甲测试' } });
  fireEvent.click(screen.getByRole('button', { name: '保存显示名' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
  expect(api).toHaveBeenCalledTimes(1);
  expect(api.mock.calls[0][0]).toBe('/api/company-workspaces/company/employees/employee/display-name');
  expect(JSON.parse(api.mock.calls[0][1].body)).toEqual({ display_name: '甲测试', expected_record_version: 0 });
});

it('retains typed name after failure and blocks blank names', async () => {
  const api = vi.fn().mockRejectedValue(new Error('DESKTOP_CONNECTION_FAILED'));
  render(<EmployeeDisplayName name="***" version={0} companyId="company" recordId="employee" api={api} disabled={false} onSaved={() => {}} />);
  fireEvent.change(screen.getByLabelText('本地员工显示名'), { target: { value: ' ' } });
  expect(screen.getByRole('button', { name: '保存显示名' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('本地员工显示名'), { target: { value: '甲测试' } });
  fireEvent.click(screen.getByRole('button', { name: '保存显示名' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('本地员工显示名')).toHaveValue('甲测试');
});
