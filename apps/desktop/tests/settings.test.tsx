import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SettingsView } from '../src/features/settings/SettingsView';

describe('SettingsView', () => {
  it('submits a write-only Zhipu key with explicit model configuration', async () => {
    const onSave = vi.fn(async () => undefined);
    render(
      <SettingsView
        status={{
          provider: 'zhipu',
          configured: false,
          validated: false,
          textModel: '',
          visionModel: '',
          baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
        }}
        onSave={onSave}
      />,
    );

    const key = screen.getByLabelText('智谱 API Key');
    expect(key).toHaveAttribute('type', 'password');
    expect(key).toHaveValue('');
    fireEvent.change(key, { target: { value: 'synthetic-ui-key-value' } });
    fireEvent.change(screen.getByLabelText('文本模型'), {
      target: { value: 'glm-synthetic-text' },
    });
    fireEvent.change(screen.getByLabelText('视觉模型'), {
      target: { value: 'glm-synthetic-vision' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        apiKey: 'synthetic-ui-key-value',
        textModel: 'glm-synthetic-text',
        visionModel: 'glm-synthetic-vision',
        baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
      }),
    );
    await waitFor(() => expect(key).toHaveValue(''));
  });

  it('shows a stable connection error without ever echoing the key', () => {
    render(
      <SettingsView
        status={{
          provider: 'zhipu',
          configured: true,
          validated: false,
          textModel: 'glm-5.2',
          visionModel: 'glm-4.6v',
          baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
        }}
        errorCode="AI_PROVIDER_ERROR"
        onSave={async () => undefined}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('AI_PROVIDER_ERROR');
    expect(screen.queryByText(/synthetic-ui-key-value/)).not.toBeInTheDocument();
  });
});
