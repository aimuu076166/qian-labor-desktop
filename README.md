# 企安用工 Desktop

## 项目介绍

企安用工 Desktop 是一个本地桌面劳动用工材料分析工具。当前公开仓主要交付源码、自动化测试和可构建工程，不代表已经正式商用、通过真实客户验证或发布了签名安装包。

## 当前架构

桌面工程采用：

```text
Tauri 2
+ React
+ TypeScript
+ Vite
+ SQLite
+ Python 3.12 sidecar
```

面向最终用户的目标是安装桌面应用后即可使用，不要求安装 Docker、PostgreSQL、Redis、Caddy、Node.js、Rust、Python，也不要求准备云服务器或域名。开发者从源码构建时仍需安装相应工具链和平台依赖。

## 当前本地链路

```text
启动 Tauri
→ Tauri 启动本机 Python sidecar
→ 每次启动生成随机 IPC token
→ SQLite 初始化或恢复
→ 原生文件选择器选择材料
→ 文件复制到应用私有目录
→ 本地解析和隐私处理
→ Fake 或受配置控制的 GLM Provider 抽取事实
→ 员工匹配
→ R01—R20 确定性规则
→ Dashboard 和来源详情
→ 删除及持久化清理
```

## 产品和法律边界

本项目坚持：

- **资料不足 ≠ 无风险**；
- AI 只抽取非结构化事实，R01—R20 负责确定性风险判断；
- 高影响事项保留人工复核；
- 每条风险应可追溯到材料来源；
- 自动化、截图和 Demo 只能使用 synthetic 数据。

模型输出不是最终法律风险结论。Provider 输出必须先通过本地 schema 校验；当前 Zhipu Provider 还会拒绝不在规范集合中的 `fact_type`。

## Provider

当前代码包含：

- `FakeAIProvider`；
- `OpenAIResponsesProvider`；
- `ZhipuChatCompletionsProvider`。

未设置 `AI_PROVIDER` 时，代码默认使用 `FakeAIProvider`；它用于无密钥测试，也是安全兜底配置，但外部 Provider 调用失败时不会自动降级为 Fake。Provider 配置必须来自服务端或 Python sidecar 运行环境，不得来自 React：

```text
AI_PROVIDER
AI_API_KEY
AI_BASE_URL
AI_TEXT_MODEL
AI_VISION_MODEL
PII_HASH_PEPPER
```

选择 `zhipu` 且模型配置留空时，当前代码的智谱文本与视觉模型默认配置值是 `glm-5.3-flash`，可由环境变量覆盖。它只是当前代码默认配置值，不是不可变或永久有效的官方型号。

## API Key 安全

API Key 不得：

- 写进 React 或 HTML；
- 写进 SQLite；
- 写进 fixture；
- 写进日志；
- 提交 Git；
- 打入安装包；
- 出现在截图。

当前尚未实现 macOS Keychain、Windows Credential Manager，也没有面向普通用户的安全 Key 设置界面。开发者通过运行环境注入配置只是当前开发和维护方式，不是最终普通用户体验。

`PII_HASH_PEPPER` 同样属于秘密，适用上述禁止提交、记录、写入 fixture、打包和截图的规则；用于外部 Provider 时应至少 32 个字符，并与应用 secret 分开管理。

## 开发环境

开发和构建需要：

- Node.js 22；
- pnpm 9.15.0；
- Python 3.12；
- Rust stable；
- 对应平台的 Tauri 构建依赖。

## 安装和验证

```bash
pnpm install --frozen-lockfile

pnpm --dir apps/desktop test -- --run
pnpm --dir apps/desktop lint
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop build

python -m pip install -e './python[test,build]'
pytest python/tests -q

python scripts/scan_sensitive.py
python scripts/verify_desktop.py
python scripts/real_provider_smoke.py
python scripts/build_sidecar.py

cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml
```

上述 `python` 表示 Python 3.12 解释器；不同平台上命令名可能是 `python`、`python3.12` 或 `py -3.12`，Windows 虚拟环境也应使用等价的 `python\.venv\Scripts\python.exe`。

`scripts/verify_desktop.py` 启动真实 sidecar 子进程，通过本机 HTTP 调用验证 synthetic Fake Provider 全链、来源追溯与重启后的持久化删除，并检查规则目录仍为 R01—R20 共 20 项。它不应输出 IPC token、材料正文、个人标识或模型密钥。

## Real Provider smoke

```bash
python scripts/real_provider_smoke.py
```

没有安全注入 `AI_API_KEY` 时，正常结果是：

```text
REAL_PROVIDER_SMOKE=NOT_RUN
REASON=AI_API_KEY_MISSING
```

无 Key 的 `NOT_RUN` 不是测试失败，也不能用 Fake Provider 的 PASS 冒充真实模型 PASS。公开 CI 只验证无 Key 的安全 `NOT_RUN` 路径，当前没有可据此确认的真实 Provider PASS。当前脚本的真实图片/VLM smoke 尚未完成，状态应继续诚实记录为：

```text
IMAGE_INPUT=NOT_RUN
```

真实 Provider smoke 只允许使用 synthetic 材料，并需要维护者在受控环境中安全注入 Key 与独立的 `PII_HASH_PEPPER`。

## 当前限制

- 尚无操作系统安全凭据存储；
- 尚无 macOS 签名和公证；
- 尚无 Windows 代码签名；
- 尚无自动更新；
- 尚未发布正式签名安装包；
- 真实 Provider smoke 需要维护者安全注入 Key；
- 当前公开仓主要交付源码、测试和可构建工程。

## License

本项目使用 [MIT License](LICENSE)。
