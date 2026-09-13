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
+ Python sidecar（CI 3.12；本地本轮检查 3.13.3）
```

面向最终用户的目标是安装桌面应用后即可使用，不要求安装 Docker、PostgreSQL、Redis、Caddy、Node.js、Rust、Python，也不要求准备云服务器或域名。开发者从源码构建时仍需安装相应工具链和平台依赖。

## 当前本地链路

```text
启动 Tauri
→ Tauri 启动本机 Python sidecar
→ 每次启动生成随机 IPC token
→ SQLite 初始化或恢复
→ 直接浏览企业工作台和员工档案（无需登录、无需先验证模型）
→ 选择企业，原生选择材料并复制到当前企业的私有材料档案
→ 需要处理时，才配置并验证智谱并明确点击“开始分析”
→ 本地解析；明确开始后将材料原文发送至已配置的智谱官方通道，模型抽取事实及单独的合同条款观察
→ 员工归属核对、选择当前合同与核查日期、有效事实人工复核
→ 明确本地重新评估；查看当前范围内的确定性结果
→ 明确生成并保存不可变报告草稿版本，选择版本后系统打印为 PDF
→ 重启恢复、历史只读；按归属删除历史副本，不删除当前材料档案
```

## 产品和法律边界

本项目坚持：

- **资料不足 ≠ 无风险**；
- 模型抽取事实，另提供标明“未经核验”的合同条款观察；条款观察不计入确定性高、中风险数量；
- 通用回归保留完整 R01—R20 目录和原有语义；员工材料工作台使用 `labor_materials_v1` 的当前范围，不等于执行全部通用规则；
- 不做工资/考勤对账、工资/加班费/补偿金计算、登录、云同步或 HR 扩展；合同工资条款审阅仍在范围内；
- 高影响事项保留人工复核；
- 每条风险应可追溯到材料来源；
- 自动化、截图和 Demo 只能使用 synthetic 数据。

模型输出不是最终法律风险结论。Provider 输出必须先通过本地 schema 校验；当前 Zhipu Provider 还会拒绝不在规范集合中的 `fact_type`。

## Provider

底层代码包含：

- `FakeAIProvider`；
- `OpenAIResponsesProvider`；
- `ZhipuChatCompletionsProvider`。

面向普通用户的桌面流程只开放智谱 Provider。浏览、建档、导入和阅读已保存报告不要求先配置模型；明确开始可能消耗额度的分析前，必须在设置页保存并通过连接测试。未配置或未验证时，处理会被明确阻止，外部调用失败也不会降级为 Fake。`FakeAIProvider` 仅供自动化测试和显式打包 smoke 使用，不能作为真实模型验收证据。

## 员工优先工作流与不确定状态

企业持有长期员工档案；当前材料是可继续补充的工作副本，历史体检保留原事实、结果和日期。未绑定旧批次只能在明确核对归属后收养；复用历史材料会创建当前档案下的新私有副本，不改原文件或历史结果，也不会自动开始处理。

每次事实修订都受员工归属、版本、来源和上下文校验。更换当前合同、核查日期或事实后，既有评估可变为过期；旧草稿必须重新核对后才采用新依据。局部成功、资料不足、员工待匹配、原文未定位、尚未执行、未知提交结果均不是“安全”或“无风险”。超出单表 10000 行/200 列的 Excel 内容整份拒绝处理，不能以截断内容宣称完整。

处理必须显式启动；取消只停止后续本机工作，已发出的远端请求仍可能收费。中断/重启后显示可复用与待处理材料，明确恢复才继续。请求超时或暂未查到回执时保留原 UUID 和原操作依据，核对只读；明确重试使用原请求，不能通过另建请求推定上次没有发生。

生成报告只冻结当前复核草稿，不调用模型、不重新体检。每个版本保留保存时间、体检依据、来源与人工记录；当前输入变化只增加过期提示，不更新保存内容或哈希。版本列表、详情及打印显示选中版本的数据；当前来源损坏时，安全的旧版本仍可阅读，但停止生成新版本。删除仅针对获授权的历史分析及其私有副本/报告/回执；当前材料和长期员工档案受保护。

桌面主程序从当前用户的应用私有目录读取秘密后，仅在启动 sidecar 时注入其进程环境；React 不接触 Key 或 pepper。开发和维护脚本仍可使用以下运行环境变量：

```text
AI_PROVIDER
AI_API_KEY
AI_BASE_URL
AI_TEXT_MODEL
AI_VISION_MODEL
PII_HASH_PEPPER
```

桌面设置当前固定使用智谱原生多模态模型 [`glm-5.3-flash`](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)，文本材料和图片材料统一使用该型号。设置页不允许改写模型编码，但可在智谱官方的“标准 API”与“Coding Plan”两个计费通道之间选择；应用只接受对应的两个官方 HTTPS 地址，不允许把 API Key 发送到任意自定义地址。升级前保存的其他型号必须使用用户自己的 Key 重新保存并通过连接测试后，才可继续真实分析。

## API Key 安全

API Key 不得：

- 写进 React 或 HTML；
- 写进 SQLite；
- 写进 fixture；
- 写进日志；
- 提交 Git；
- 打入安装包；
- 出现在截图。

macOS 版本已经提供面向普通用户的设置页。连接测试成功后，API Key 和随机生成的 `PII_HASH_PEPPER` 分项存放在当前用户的应用私有目录，Unix/macOS 文件权限限制为 `0600`；界面只显示“已配置”，不会回显 Key。主程序还会先移除继承环境中可能存在的 Provider 秘密，再把本地私有文件中的值只注入受控 sidecar 进程。该方案避免无 Developer ID 签名 RC 反复触发 Keychain 授权，但同一用户权限下的其他进程理论上可读取这些文件，静态保护弱于 macOS Keychain。

`PII_HASH_PEPPER` 同样属于秘密，适用上述禁止提交、记录、写入 fixture、打包和截图的规则；应用首次配置时生成至少 32 个字符的随机值，并与 API Key 分项保存在应用私有目录。

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

候选文件从该 Pull Request 的 GitHub Actions `desktop-rc` run 下载；优先下载最终的 `qian-labor-desktop-0.1.0-rc.1-unsigned` 汇总 artifact，而不是把平台 job 的中间 artifact 当作正式候选。普通用户打开工作台无需登录或先验证模型；明确开始材料分析前，必须在应用内配置并验证自己的智谱 API Key，未完成验证时会阻止分析，不会静默使用 Fake Provider。无签名候选仍应先使用 synthetic 材料完成安装验收，再由数据责任人决定是否导入真实材料。

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

Fake Provider 仅用于自动化测试和显式打包 smoke；普通用户的显式处理只接受经连接测试验证的智谱 Provider。API Key 与本地隐私 pepper 存放在权限为 `0600` 的应用私有文件中，不写入 React、SQLite、日志或安装包。synthetic 验收后只通过应用内明确归属的历史分析删除清理测试副本，并确认当前材料、长期员工档案和其他企业未受影响。不要把删除整个应用数据目录当成正常测试清理步骤；其中可能已有其他企业或真实材料。

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

该脚本仅保留维护者的旧命令行诊断入口，不能代替当前强制验收。最终必须在正常应用设置中安全配置用户自己的模型通道，并使用中文十名虚构员工/八份混合材料完成真实 GLM 语义、图片和扫描件检查；记录原始 ID、日期、条款及真实位置，不准备模型标准答案。两条路径都不得在聊天、日志、fixture 或 CI 中传递 Key。

构建清单中的 `real_provider_smoke=NOT_RUN`、`image_input=NOT_RUN` 继续如实表示 CI 没运行；另附注明最终 commit、实际下载 artifact SHA-256 的安装验收报告，不能把手工结果伪装为 CI PASS。最终 Mac 原生启动、导入、处理/取消/恢复、匹配、事实核对、版本打印、重启、归属删除和退出清理都是 Ready 前门禁，不是可选补充。此 README 不声明这些待执行项目已经通过。

## 当前限制

- 候选仅有 ad-hoc bundle 签名，尚无 Developer ID 签名和 macOS 公证；
- 当前候选仅支持 Apple Silicon macOS，不提供 Windows 安装包；
- 尚无自动更新；
- 尚未发布正式签名安装包；
- `v0.1.0-rc.1` 工作流只产生临时候选 artifact，不创建标签或 GitHub Release；
- 图片/扫描材料的真实模型与安装验收是本 RC 强制门禁，当前结果须查最终 exact-head 验收记录；Word 报告和多 Provider 用户配置不在范围；
- 底层 OpenAI 适配尚不支持完整实际提取 schema 的 tuple `prefixItems` 和字符串长度约束；最小 schema 传输测试不代表完整 OpenAI 可用，GLM-only RC 不扩展该适配器；
- CI Python 3.12 与本地 3.13.3 结果分别记录；已知 Starlette/httpx、AnyIO、SWIG 弃用警告保留，不作隐藏或临时依赖升级；
- macOS 最低 11.0 是构建元数据，不等于已在 macOS 11 实机测试。本轮开发主机 macOS 15.7.3；最终报告须单列实际安装测试主机及最低版本未验证限制；
- PR 转 Ready 前仍必须由用户在应用内完成真实智谱 Provider 的 synthetic 验收。

## License

本项目使用 [MIT License](LICENSE)。
