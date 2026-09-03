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
→ 首次启动在应用内配置并验证智谱 API Key
→ API Key 与隐私 pepper 仅存入 macOS Keychain
→ 原生文件选择器选择材料
→ 文件复制到应用私有目录
→ 本地解析和隐私处理
→ 智谱 GLM Provider 抽取事实
→ 自动员工匹配，歧义项进入人工匹配复核
→ R01—R20 确定性规则
→ Dashboard、风险详情、员工台账与报告
→ 通过系统打印对话框存储为 PDF
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

底层代码包含：

- `FakeAIProvider`；
- `OpenAIResponsesProvider`；
- `ZhipuChatCompletionsProvider`。

面向普通用户的桌面流程只开放智谱 Provider。首次启动必须在设置页输入并通过连接测试；未配置或未验证时，材料分析会被明确阻止，外部调用失败也不会降级为 Fake。`FakeAIProvider` 仅供自动化测试和显式打包 smoke 使用，不能作为用户流程的无密钥兜底。

桌面主程序从 macOS Keychain 读取秘密后，仅在启动 sidecar 时注入其进程环境；React 不接触 Key 或 pepper。开发和维护脚本仍可使用以下运行环境变量：

```text
AI_PROVIDER
AI_API_KEY
AI_BASE_URL
AI_TEXT_MODEL
AI_VISION_MODEL
PII_HASH_PEPPER
```

桌面设置当前固定使用智谱原生多模态模型 [`glm-5.3-flash`](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)，文本材料和图片材料统一使用该型号。设置页不允许改写模型编码；升级前保存的其他型号必须使用用户自己的 Key 重新保存并通过连接测试后，才可继续真实分析。

## API Key 安全

API Key 不得：

- 写进 React 或 HTML；
- 写进 SQLite；
- 写进 fixture；
- 写进日志；
- 提交 Git；
- 打入安装包；
- 出现在截图。

macOS 版本已经提供面向普通用户的安全设置页。连接测试成功后，API Key 和随机生成的 `PII_HASH_PEPPER` 存放在当前用户的 macOS Keychain；界面只显示“已配置”，不会回显 Key。主程序还会先移除继承环境中可能存在的 Provider 秘密，再把 Keychain 中的值只注入受控 sidecar 进程。

`PII_HASH_PEPPER` 同样属于秘密，适用上述禁止提交、记录、写入 fixture、打包和截图的规则；应用首次配置时生成至少 32 个字符的随机值，并与 API Key 分项保存在 Keychain。

## 开发环境

开发和构建需要：

- Node.js 22；
- pnpm 9.15.0；
- Python 3.12；
- Rust 1.98.0（已由 Linux、macOS ARM64 和 Windows x64 CI 验证）；
- 对应平台的 Tauri 构建依赖。

## 安装和验证

```bash
pnpm install --frozen-lockfile

pnpm --dir apps/desktop test -- --run
pnpm --dir apps/desktop lint
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop build

python -m pip install -e './python[test,build]'
python -m compileall -q python/src python/tests scripts
pytest python/tests -q

python scripts/scan_sensitive.py
python scripts/scan_public_history.py --repo .
python scripts/verify_desktop.py
python scripts/real_provider_smoke.py
python scripts/build_sidecar.py

cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all --check
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
```

上述 `python` 表示 Python 3.12 解释器；不同平台上命令名可能是 `python`、`python3.12` 或 `py -3.12`，Windows 虚拟环境也应使用等价的 `python\.venv\Scripts\python.exe`。

`Cargo.lock` 已提交；所有 Cargo 验证中的依赖解析均使用 `--locked`，以固定 Cargo 的依赖解析。Rust 1.98.0 已由 Linux、macOS ARM64 和 Windows x64 CI 验证为当前固定工具链。这里的固定范围不表示完整构建或安装包达到逐位可复现，也不表示已经完成签名生产发布。

`python -m compileall`、`cargo fmt --check` 和上述锁定的 Cargo 验证均为 CI 阻断门禁。公共历史扫描要求在非浅克隆且已完整抓取的本地 Git 历史上执行：它检查本地 HEAD 或任一本地 ref 可达的文本 blob 与提交消息，以及从本地 ref 可达的附注标签消息；二进制 blob、未被 ref/HEAD 引用的悬空对象和未抓取到本地的远端历史不在内容匹配范围内。扫描器会清除继承的 `GIT_*` 仓库定向环境、忽略 replace objects、禁用 commit-graph 加速，并在发现 legacy grafts、浅克隆或不完整对象图时失败关闭。CI 的 `public-history-security` job 使用完整历史检出后运行该扫描。

`scripts/verify_desktop.py` 启动真实 sidecar 子进程，通过本机 HTTP 调用验证 synthetic Fake Provider 自动化全链，包括人工员工匹配、R01—R20、Dashboard、员工台账、报告、来源追溯、重启持久化和删除清理。它不应输出 IPC token、材料正文、个人标识或模型密钥。

## v0.1.0-rc.1 无签名打包验收

`.github/workflows/desktop-rc.yml` 只在 `release/` 分支的 Pull Request 或手动 `workflow_dispatch` 时运行高成本打包。当前收口范围只生成 Apple Silicon macOS 的 `.app` / `.dmg`，固定使用 Node.js 22、pnpm 9.15.0、Python 3.12、Rust 1.98.0 和 Xcode 26.2。候选标签是 `0.1.0-rc.1`；应用内部版本仍为 `0.1.0`。Windows 安装包不属于本阶段交付范围。

候选文件从该 Pull Request 的 GitHub Actions `desktop-rc` run 下载；优先下载最终的 `qian-labor-desktop-0.1.0-rc.1-unsigned` 汇总 artifact，而不是把平台 job 的中间 artifact 当作正式候选。普通用户首次启动必须在应用内配置并验证自己的智谱 API Key；未完成验证时，应用会阻止材料分析，不会静默使用 Fake Provider。无签名候选仍应先使用 synthetic 材料完成安装验收，再由数据责任人决定是否导入真实材料。

验收分为四层，不能互相替代：

1. `scripts/verify_desktop.py` 验证源码入口；
2. `scripts/verify_built_sidecar.py` 验证 PyInstaller 真实二进制、环回绑定、token 鉴权、SQLite、synthetic Fake Provider 全链、R01—R20、来源追溯、删除及退出残留；
3. `scripts/verify_rc_bundle.py` 检查应用载荷的版本、标识符、CPU 架构、sidecar 数量、禁止文件、敏感内容和构建机路径；
4. `scripts/smoke_packaged_app.py` 启动 DMG 中的主程序，要求它启动随包 sidecar、在隔离的临时目录建立 SQLite，在主程序退出前完成受鉴权关闭和 owned-process 清理，并独立确认诊断 PID 不再存活；`--abnormal-lifecycle` 还会强制终止测试主程序，验证异常退出时进程树仍被清理。

打包应用 smoke 仅在显式设置 `QIAN_RC_SMOKE=1` 时生效，并且 `QIAN_RC_SMOKE_DIR` 必须是操作系统临时目录下已存在、名称以 `qian-rc-smoke-` 开头的真实目录；符号链接和越界路径会被拒绝。该模式不暴露 IPC token，不改变普通用户启动路径，也不会把临时数据库写入安装包。

正常退出时，桌面主程序先调用只绑定 loopback、受随机启动 token 保护的内部 shutdown API，让 FastAPI lifespan 关闭队列和数据库；超时或请求失败后才使用启动时建立的进程树所有权对象。Unix/macOS 使用 exec 前建立且由看门狗锚进程持续证明的专用进程组，Windows 使用 suspended creation 后先纳入 kill-on-close Job Object 再恢复进程。READY payload 中的 PID 只用于诊断和 smoke 证据，不是终止权限。

macOS ARM64 本地候选构建示例：

```bash
export RUSTFLAGS="--remap-path-prefix=$HOME=BUILD_HOME -C link-arg=-Wl,-S -C link-arg=-Wl,-x"
python scripts/build_sidecar.py
python scripts/verify_built_sidecar.py \
  --binary apps/desktop/src-tauri/binaries/qian-sidecar-aarch64-apple-darwin
pnpm --dir apps/desktop tauri build --bundles app,dmg --ci -- --locked
```

最终组合 artifact 保留 14 天，并包含：

```text
qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.app.tar.gz
qian-labor-desktop-0.1.0-rc.1-macos-arm64-unsigned.dmg
SHA256SUMS.txt
BUILD-MANIFEST.json
```

这些文件没有 Developer ID 签名，也没有 macOS 公证，操作系统可能显示来源或安全警告。macOS 配置 `signingIdentity: "-"` 生成完整的 ad-hoc 应用包签名，以满足 Apple Silicon 本机执行和完整性校验要求；该签名不等于 Developer ID 签名。清单必须记录 `signed=false`、`notarized=false`、CI 真实 Provider `NOT_RUN` 和图片输入 `NOT_RUN`。

下载后应先在同一目录核对清单。macOS/Linux 可运行：

```bash
shasum -a 256 -c SHA256SUMS.txt
```

校验失败时不要安装或绕过警告。不要要求测试者全局关闭 Gatekeeper；如需继续，只能对已核对哈希的单个内部候选按本机安全策略处理。

Fake Provider 仅用于自动化测试和显式打包 smoke；普通用户流程只接受经连接测试验证的智谱 Provider。API Key 与本地隐私 pepper 存放在 macOS Keychain，不写入 React、SQLite、日志或安装包。完成 synthetic 验收并退出应用后，如需清除本地测试数据，应先确认主程序和 sidecar 已退出，再删除当前用户下的应用数据目录：macOS 为 `~/Library/Application Support/cn.qianlabor.desktop`。清理前应确认目录标识符完全一致，避免删除其他应用数据。

只有 macOS ARM64 的 built-sidecar、正常 packaged-app、Launch Services 启动与异常生命周期清理 smoke 都真实通过，最终下载产物经独立重算 SHA-256 后一致，并且由用户自有 Key 完成 exact-head 真实 Provider synthetic 验收，RC Pull Request 才能从 Draft 转为 Ready。动态 commit、run ID、大小和 SHA-256 以 PR 的 exact-head 证据、`BUILD-MANIFEST.json` 与 `SHA256SUMS.txt` 为准，不回填到源码模板形成 provenance 循环。缺少真实 Provider 验收时必须保持 Draft，不能用 Fake、源码测试、bundle 检查或推测替代 `PASS`。详细规则见 `docs/release/v0.1.0-rc.1-checklist.md`。

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

该脚本是维护者使用的命令行门禁；安装包的普通用户验收应在应用设置页输入用户自己的 Key，并只使用 synthetic 材料。两条路径都不得在聊天、日志、fixture 或 CI 中传递 Key。

## 当前限制

- 候选仅有 ad-hoc bundle 签名，尚无 Developer ID 签名和 macOS 公证；
- 当前候选仅支持 Apple Silicon macOS，不提供 Windows 安装包；
- 尚无自动更新；
- 尚未发布正式签名安装包；
- `v0.1.0-rc.1` 工作流只产生临时候选 artifact，不创建标签或 GitHub Release；
- 图片/VLM 验收、Word 报告和多 Provider 用户配置不在本 RC 范围；
- PR 转 Ready 前仍必须由用户在应用内完成真实智谱 Provider 的 synthetic 验收。

## License

本项目使用 [MIT License](LICENSE)。
