import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ImportPanel } from '../src/features/import/ImportPanel';
import { selectEmploymentFiles } from '../src/lib/desktop';

vi.mock('../src/lib/desktop', () => ({
  selectEmploymentFiles: vi.fn(),
}));

const mockedSelect = vi.mocked(selectEmploymentFiles);

describe('ImportPanel', () => {
  beforeEach(() => mockedSelect.mockReset());

  it('shows one primary material-import action and the supported formats', () => {
    render(<ImportPanel onSelected={() => undefined} />);
    expect(screen.getByRole('button', { name: '选择企业材料' })).toBeInTheDocument();
    expect(screen.getByText(/Excel.*Word.*PDF.*图片/)).toBeInTheDocument();
  });

  it('opens the native picker only after an explicit click and forwards selected paths', async () => {
    const onSelected = vi.fn();
    mockedSelect.mockResolvedValue(['/tmp/a.pdf', '/tmp/b.xlsx']);
    render(<ImportPanel onSelected={onSelected} />);

    expect(mockedSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '选择企业材料' }));

    await waitFor(() => expect(onSelected).toHaveBeenCalledWith(['/tmp/a.pdf', '/tmp/b.xlsx']));
    expect(mockedSelect).toHaveBeenCalledTimes(1);
  });
});
