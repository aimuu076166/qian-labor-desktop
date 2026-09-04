import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SettingsView } from '../src/features/settings/SettingsView';

describe('SettingsView', () => {
  it('submits a write-only Zhipu key with the fixed multimodal model', async () => {
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
    expect(screen.getByLabelText('分析模型')).toHaveValue('glm-5.3-flash');
    expect(screen.getByLabelText('分析模型')).toHaveAttribute('readonly');
    expect(screen.queryByLabelText('文本模型')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('视觉模型')).not.toBeInTheDocument();
    expect(
      screen.getByText('API Key 仅保存在本机当前用户的应用私有目录。连接测试不发送企业或员工材料。'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Keychain|钥匙串/)).not.toBeInTheDocument();
    fireEvent.change(key, { target: { value: 'synthetic-ui-key-value' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并测试连接' }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        apiKey: 'synthetic-ui-key-value',
        textModel: 'glm-5.3-flash',
        visionModel: 'glm-5.3-flash',
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
          textModel: 'glm-5.3-flash',
          visionModel: 'glm-5.3-flash',
          baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
        }}
        errorCode="AI_PROVIDER_ERROR"
        onSave={async () => undefined}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('AI_PROVIDER_ERROR');
    expect(screen.queryByText(/synthetic-ui-key-value/)).not.toBeInTheDocument();
  });

  it('explains provider account arrears without exposing the provider response body', () => {
    render(
      <SettingsView
        status={{
          provider: 'zhipu',
          configured: true,
          validated: false,
          textModel: 'glm-5.3-flash',
          visionModel: 'glm-5.3-flash',
          baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
        }}
        errorCode="AI_ACCOUNT_ARREARS"
        onSave={async () => undefined}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('智谱账户欠费');
    expect(screen.getByRole('alert')).toHaveTextContent('AI_ACCOUNT_ARREARS');
    expect(screen.queryByText(/never expose this body/)).not.toBeInTheDocument();
  });
});
