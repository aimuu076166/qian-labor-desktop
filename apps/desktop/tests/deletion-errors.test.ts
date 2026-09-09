import { expect, it } from 'vitest';
import { describeOperationError } from '../src/lib/errorMessages';

it('explains rejected deletion without claiming success or revealing a private path', () => {
  const message = describeOperationError('DESKTOP_DELETION_STORAGE_UNSAFE');
  expect(message).toMatch(/归属|路径/);
  expect(message).toMatch(/停止|未完成/);
  expect(message).not.toContain('已删除');
  expect(describeOperationError('/synthetic/private/material.csv')).not.toContain('/synthetic');
});
