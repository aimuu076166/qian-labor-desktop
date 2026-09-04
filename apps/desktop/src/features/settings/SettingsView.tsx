import { type FormEvent, useRef, useState } from 'react';

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
const ZHIPU_STANDARD_BASE_URL = 'https://open.bigmodel.cn/api/paas/v4';
const ZHIPU_CODING_PLAN_BASE_URL = 'https://open.bigmodel.cn/api/coding/paas/v4';

const PROVIDER_ERROR_MESSAGES: Record<string, string> = {
  AI_ACCOUNT_ARREARS:
    '当前 Key 在所选接口没有可用额度。Coding Plan 用户请选择 Coding Plan 通道。',
  AI_RATE_LIMIT: '请求过于频繁，请等待一分钟后重试。',
  AI_PROVIDER_OVERLOADED: '智谱模型当前访问量过大，请稍后重试。',
  AI_QUOTA_EXCEEDED: '智谱额度已用完，请检查账户额度或等待重置。',
  AI_PLAN_EXPIRED: '智谱套餐已到期，请续订后重试。',
  AI_PROVIDER_ERROR: '智谱连接失败，请检查 API Key 与账户状态。',
};

export function SettingsView({
  status,
  saving = false,
  errorCode = null,
  onSave,
}: SettingsViewProps) {
  const keyRef = useRef<HTMLInputElement>(null);
  const [baseUrl, setBaseUrl] = useState(status.baseUrl);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const apiKey = keyRef.current?.value.trim() ?? '';
    try {
      await onSave({
        apiKey,
        textModel: ZHIPU_MODEL,
        visionModel: ZHIPU_MODEL,
        baseUrl,
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
            API Key 仅保存在本机当前用户的应用私有目录。连接测试不发送企业或员工材料。
          </p>
        </div>
        <span className="status-pill">
          {status.validated ? '连接已验证' : status.configured ? '等待连接验证' : '尚未配置'}
        </span>
      </div>

      {errorCode ? (
        <p className="settings-error" role="alert">
          连接失败：{PROVIDER_ERROR_MESSAGES[errorCode] ?? '请稍后重试。'}（{errorCode}）
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

        <label htmlFor="zhipu-base-url">计费通道</label>
        <select
          id="zhipu-base-url"
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
        >
          <option value={ZHIPU_STANDARD_BASE_URL}>
            标准 API — {ZHIPU_STANDARD_BASE_URL}
          </option>
          <option value={ZHIPU_CODING_PLAN_BASE_URL}>
            Coding Plan — {ZHIPU_CODING_PLAN_BASE_URL}
          </option>
        </select>

        <button type="submit" className="primary-action" disabled={saving}>
          {saving ? '正在安全保存并测试…' : '保存并测试连接'}
        </button>
      </form>
    </section>
  );
}
