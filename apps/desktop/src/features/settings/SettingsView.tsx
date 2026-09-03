import { type FormEvent, useRef } from 'react';

export type ProviderConfigurationStatus = {
  provider: string;
  configured: boolean;
  validated: boolean;
  textModel: string;
  visionModel: string;
  baseUrl: string;
};

export type ProviderConfigurationInput = {
  apiKey: string;
  textModel: string;
  visionModel: string;
  baseUrl: string;
};

type SettingsViewProps = {
  status: ProviderConfigurationStatus;
  saving?: boolean;
  errorCode?: string | null;
  onSave: (input: ProviderConfigurationInput) => Promise<void>;
};

const ZHIPU_MODEL = 'glm-5.3-flash';

export function SettingsView({
  status,
  saving = false,
  errorCode = null,
  onSave,
}: SettingsViewProps) {
  const keyRef = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const apiKey = keyRef.current?.value.trim() ?? '';
    try {
      await onSave({
        apiKey,
        textModel: ZHIPU_MODEL,
        visionModel: ZHIPU_MODEL,
        baseUrl: status.baseUrl,
      });
    } finally {
      if (keyRef.current) keyRef.current.value = '';
    }
  }

  return (
    <section className="settings-view" aria-labelledby="settings-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">首次使用设置</p>
          <h2 id="settings-title">连接智谱 GLM</h2>
          <p className="muted">
            API Key 只写入本机 macOS Keychain。连接测试不发送企业或员工材料。
          </p>
        </div>
        <span className="status-pill">
          {status.validated ? '连接已验证' : status.configured ? '等待连接验证' : '尚未配置'}
        </span>
      </div>

      {errorCode ? (
        <p className="settings-error" role="alert">
          连接失败，错误代码：{errorCode}
        </p>
      ) : null}

      <form className="settings-form" onSubmit={submit}>
        <label htmlFor="zhipu-api-key">智谱 API Key</label>
        <input
          ref={keyRef}
          id="zhipu-api-key"
          name="zhipu-api-key"
          type="password"
          autoComplete="off"
          required
          minLength={8}
          maxLength={4096}
          placeholder={status.configured ? '重新输入以更新 Key' : '请输入你自己的智谱 API Key'}
        />

        <label htmlFor="zhipu-model">分析模型</label>
        <input
          id="zhipu-model"
          value={ZHIPU_MODEL}
          readOnly
        />

        <label htmlFor="zhipu-base-url">服务地址</label>
        <input id="zhipu-base-url" value={status.baseUrl} readOnly />

        <button type="submit" className="primary-action" disabled={saving}>
          {saving ? '正在安全保存并测试…' : '保存并测试连接'}
        </button>
      </form>
    </section>
  );
}
