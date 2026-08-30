# 企安用工 Desktop 架构设计

更新时间：2026-08-27

> 实施入口：`docs/superpowers/plans/2026-08-27-qian-labor-desktop-foundation.md`

## 1. 设计目标

本仓库仅负责「企安用工 Desktop」，不在此仓库修改 Web 产品。

桌面版的目标不是重新发散产品方向，而是保留现有劳动用工业务内核，改变交付形态：

```text
下载安装
→ 双击企安用工
→ 选择本机劳动用工材料
→ 本机解析 / OCR / 员工归属 / 规则计算
→ 必要时仅将脱敏后的最小信息提交给模型
→ Dashboard / 风险详情 / 员工台账 / 报告
```

桌面版第一阶段明确不需要：

- 公网服务器；
- Docker；
- Caddy；
- PostgreSQL；
- Redis；
- RQ Worker；
- 域名；
- ICP 备案；
- Web 访问码和 8 小时 Session。

目标平台第一优先级：

1. macOS Apple Silicon；
2. Windows x64；
3. macOS Intel 作为后续兼容项，不阻断首个桌面 MVP。

## 2. 现有 Web 版中必须继承的产品内核

桌面版不是“重新想一个新产品”。以下内容从 Web 版继承并保持业务语义一致：

- 「企安用工——中小企业劳动用工风险一键体检 Agent」产品定位；
- 单一混合材料入口；
- CSV / XLS / XLSX / DOCX / 文本 PDF / 扫描 PDF / 图片处理；
- EmploymentFact 结构化事实；
- SourceLocator 来源定位；
- 员工材料归属与 matching_review 人工复核；
- “资料不足 ≠ 无风险”；
- 高影响事项必须人工复核；
- R01—R20 确定性规则；
- 风险 Dashboard；
- 风险详情；
- 员工风险台账；
- 报告；
- 删除分析与数据清理；
- synthetic Demo；
- AI 只负责非结构化事实抽取，确定性风险继续由程序和规则引擎产生；
- 模型输出继续经过结构化 Schema 校验；
- 所有风险继续可追溯到原始材料。

Web 版当前 R01—R20 规则目录保持为桌面版的初始规则基线，不在迁移阶段重写法律业务规则。

## 3. 不从 Web 版继承的云端基础设施

以下内容属于 Web 交付方式，不应直接搬到桌面版：

- Next.js 服务端运行时；
- Caddy；
- Docker Compose；
- PostgreSQL 运维；
- Redis；
- RQ；
- Web Session / Cookie 准入；
- 服务器部署脚本；
- 域名和 HTTPS 运维；
- 公网生产 preflight；
- 云服务器日志和备份流程。

## 4. 方案比较

### 方案 A：全部用 Rust 重写

结构：

```text
Tauri + React + Rust + SQLite
```

优点：

- 单一技术栈；
- 桌面打包形态最干净；
- 运行时依赖少。

缺点：

- Web 版已经完成的 PDF、DOCX、Excel、图片、隐私处理、模型适配、员工匹配、R01—R20 等 Python 代码需要大量重写；
- 法律业务规则回归风险最高；
- 首版投入产出比低。

结论：**首版不采用。**

### 方案 B：Tauri + React + Python sidecar + SQLite（推荐）

结构：

```text
Tauri / Rust
   │
React Desktop UI
   │
本机 Python Sidecar
   │
SQLite + LocalStorage
   │
解析 / OCR / 脱敏 / 模型 / 匹配 / R01—R20
```

优点：

- 最大程度复用现有 Python 业务内核；
- 现有数据库层支持 SQLite；
- 当前处理队列已经存在 `processing_inline` 路径，证明 Redis 不是业务内核硬依赖；
- React 页面和组件可以选择性迁移；
- 无需用户安装 Python，sidecar 可在构建阶段打成平台原生可执行文件。

缺点：

- 需要维护 Tauri 主程序和 Python sidecar 两个运行单元；
- sidecar 需要 Windows/macOS 分平台构建；
- OCR / PyMuPDF / Pillow 等原生依赖需要平台化验证。

结论：**采用。**

### 方案 C：Tauri + React + Python JSON-RPC sidecar + SQLite

与方案 B 类似，但完全废弃 FastAPI，本地 UI 通过 stdin/stdout 或自定义 IPC 和 Python 通信。

优点：

- 不开放 localhost HTTP 端口；
- 本地进程边界更纯粹。

缺点：

- 需要重新设计 API、进度、错误协议和生命周期；
- 无法直接利用现有 FastAPI 路由；
- 首版迁移工作更大。

结论：**作为后续收敛方向，不作为首个可运行版本。**

## 5. 推荐总体架构

首版采用：

```text
┌──────────────────────────────────────────┐
│           企安用工 Desktop               │
│                                          │
│  React + TypeScript + Vite               │
│  - 首页 / 导入材料                       │
│  - 处理进度                              │
│  - 人工匹配                              │
│  - Dashboard                             │
│  - 风险详情                              │
│  - 员工台账                              │
│  - 报告 / 设置                           │
│                 │                        │
│                 ▼                        │
│  Tauri 2 / Rust                          │
│  - 窗口生命周期                          │
│  - 原生文件选择                          │
│  - App data 目录                         │
│  - Python sidecar 启停                    │
│  - 模型密钥安全存储接口                  │
│                 │                        │
└─────────────────┼────────────────────────┘
                  ▼
        Python Desktop Sidecar
        - 本机 FastAPI（127.0.0.1）
        - SQLite
        - LocalStorage
        - Parsing / Vision preparation
        - PrivacyBoundary
        - AI Provider
        - Matching
        - RiskEvaluation
        - Deletion
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
   本机文件/SQLite       外部模型 API
                         （只发送必要的
                          脱敏后内容）
```

## 6. React 前端策略

桌面版不继续以 Next.js 作为运行时。

首版采用：

- React 19；
- TypeScript；
- Vite；
- React Router；
- TanStack Query；
- Recharts；
- 延续 Web 版现有视觉语言和核心组件。

迁移原则：

1. 迁移“纯 React 组件”和业务视图；
2. 不迁移 Next.js 服务端路由、SSR、rewrite、Cookie 准入逻辑；
3. 桌面导航改为适合固定窗口的应用布局；
4. 用户依然只有一个主要材料入口；
5. 首个版本不为“桌面感”重做所有 UI，先确保完整业务链路跑通。

## 7. Python Sidecar 设计

### 7.1 为什么保留 FastAPI

现有代码包含可复用的 API 路由、服务层和测试。

首版 sidecar 将 FastAPI 作为**仅本机进程内使用的 API 外壳**，并不代表部署了云服务器。

它只监听：

```text
127.0.0.1:<ephemeral-port>
```

禁止监听：

```text
0.0.0.0
```

### 7.2 启动流程

Tauri 启动时：

1. 获取系统 app data 目录；
2. 创建 `qian-labor-desktop` 数据目录；
3. 生成本次启动随机 IPC token；
4. 启动 Python sidecar；
5. 将数据目录、随机 token、provider 配置通过受控参数/环境传入；
6. sidecar 选择一个本地随机端口；
7. sidecar 输出一行机器可读的 READY 信息；
8. Tauri 完成健康检查后再让 React 进入业务首页。

退出时：

1. 请求 sidecar 优雅停止；
2. 超时后终止子进程；
3. SQLite 和文件目录不删除。

### 7.3 本机 API 鉴权

即使只监听 `127.0.0.1`，也不能认为本地端口天然安全。

首版要求：

- 每次应用启动生成随机 token；
- 每个 sidecar 业务请求携带 token；
- sidecar 拒绝无 token / 错 token 请求；
- token 不落盘、不写日志；
- React 不把 token 写 localStorage；
- 后续可收敛为 Rust 代理或自定义 IPC。

## 8. SQLite 和本地数据目录

现有 Python `Database` 已支持 SQLite，并开启 foreign keys；桌面版直接继承 SQLAlchemy 模型，而不是重新建一套 Rust 数据模型。

推荐目录：

```text
<AppData>/qian-labor-desktop/
  qian-labor.db
  analyses/
    <analysis-id>/
      raw/
      rendered/
      derived/
  logs/
```

要求：

- SQLite 使用本机文件数据库；
- 桌面版首期为单用户、单机；
- 允许多个历史分析，但第一阶段只允许一个分析任务同时执行；
- 开启 SQLite WAL 前必须先做跨平台测试；
- 数据目录位置由 Tauri 取得，不在 Python 中硬编码用户目录；
- 用户删除分析时继续走 DeletionService；
- 提供“打开数据目录”仅作为高级调试入口，不在普通首页暴露。

## 9. 去掉 Redis / RQ 的本地任务设计

Web 版 `ProcessingQueue` 已存在 `processing_inline` 分支，但桌面实际不能让一次 HTTP 请求一直阻塞到整个分析完成。

因此桌面版新增一个本机后台执行器：

```text
DesktopProcessingQueue
```

要求：

- sidecar 内部 `ThreadPoolExecutor(max_workers=1)` 或等价单任务后台执行器；
- `POST /process` 快速返回 202；
- 后台执行 `ProcessingPipeline.process()`；
- UI 继续轮询 processing status；
- 同一分析重复触发必须幂等或明确拒绝；
- 首版只允许一个活动分析任务，避免 SQLite 写并发和重复模型成本；
- 不依赖 Redis；
- 不启动 RQ worker。

## 10. 本地文件导入

桌面版优先使用 Tauri 原生文件选择，不把“网页上传”原样照搬。

流程：

```text
选择材料
→ Tauri 返回用户明确选择的本机路径
→ sidecar 读取文件
→ 复用现有 upload validation
→ 复制到应用私有数据目录
→ 后续处理只使用私有副本
```

首版支持：

- 多文件选择；
- CSV/XLS/XLSX；
- DOCX；
- PDF；
- PNG/JPG/JPEG/WEBP。

拖拽导入放到后续增量，不阻断第一条垂直链路。

安全要求：

- 只处理用户通过文件选择器/拖拽明确选择的文件；
- 继续执行现有类型、大小和内容校验；
- 不递归扫描整个磁盘；
- 原始文件不被修改；
- 应用复制自己的工作副本。

## 11. OCR / 扫描件处理

现有 ParserRegistry 已经能区分：

- 文本 PDF；
- 需要视觉处理的 PDF 页面；
- 图片。

桌面版首版不重新设计解析算法。

需要新增的是平台打包验证：

- PyMuPDF；
- Pillow；
- Office 解析依赖；
- 如果某隐私/视觉步骤依赖本机 OCR 二进制，则必须将对应 OCR 可执行文件和语言数据作为桌面资源打包，不能要求普通用户另装 Homebrew / apt / Chocolatey。

首个垂直切片允许先以现有 synthetic 文本/图片链路验证 sidecar 打包；扫描件原生依赖验证作为独立任务。

## 12. AI Provider 和 GLM

桌面架构不应绑定 OpenAI。

Provider 继续遵循统一接口：

```text
extract(filename, content) -> ExtractionResult
```

首版 Provider 层目标：

- `fake`：默认自动化测试 / 无网演示；
- `zhipu`：受配置控制的真实模型 Provider；
- `openai-responses`：可选保留，不是桌面 MVP 的必需项。

具体 GLM 模型编码不得硬编码。

配置通过：

```text
AI_PROVIDER
AI_TEXT_MODEL
AI_VISION_MODEL
```

等抽象字段传给 sidecar。

### 12.1 API Key

桌面软件不能把开发者自己的模型 API Key 固化进安装包。

首版设计：

- 开发/测试可通过进程环境注入；
- 正式桌面设置页允许用户录入自己的 Key；
- Key 的持久化由 Rust/Tauri 侧接入操作系统安全凭据存储；
- Python sidecar只在启动时获得当前会话所需密钥；
- Key 不写 SQLite；
- Key 不写日志；
- Key 不进入 React localStorage；
- Key 不进入 Git。

如果未来要实现“任何用户安装后无需自己的 Key，统一使用产品方模型额度”，则需要独立的轻量 AI Gateway；这属于后续商业化架构，不进入当前无服务器桌面 MVP。

## 13. 隐私边界

桌面版核心卖点之一是“原始材料默认留在本机”。

必须坚持：

```text
原始文件
→ 本机解析 / OCR
→ 本机 PrivacyBoundary
→ 只将必要且脱敏后的内容提交给模型
```

不得宣传“100% 离线”，因为真实 GLM/OpenAI 模型仍需联网。

准确表述应为：

> 本地优先：原始劳动用工材料和结构化台账默认保存在企业本机；只有需要模型理解的最小必要内容，在本地脱敏后才提交给所配置的模型服务。

## 14. 删除、备份与迁移

删除：

- 继续复用 Web 版删除语义；
- 删除原始副本、渲染/派生文件、事实、来源、用量和风险；
- 仅保留必要的非个人化墓碑。

首版备份：

- 不自动云同步；
- 后续提供“导出本地备份包 / 导入备份包”；
- 备份包默认视为敏感数据；
- 正式备份格式在数据库 schema 稳定后单独设计，不阻断第一垂直链路。

## 15. UI 首个版本范围

首个桌面 MVP 页面：

1. 首页 / 材料导入；
2. 处理进度；
3. 人工匹配；
4. 风险 Dashboard；
5. 风险详情；
6. 员工台账；
7. 报告；
8. 设置（模型 Provider / Key / 数据目录信息）。

Web 访问码页面不迁移。

首页直接进入产品，必要时以后再加入本机 PIN / 企业口令。

## 16. 首个可验收垂直切片

第一阶段不追求一次迁完全部 Web 功能。

必须先打通：

```text
启动 Tauri
→ sidecar healthy
→ 建立 SQLite
→ 选择 synthetic 文件
→ 创建 Analysis
→ 导入文件
→ Fake Provider 处理
→ R01—R20 运行
→ Dashboard 可见
→ 风险详情可见来源
→ 删除分析
→ 重启应用后数据状态正确
```

第一阶段验收只使用 synthetic 数据。

## 17. 构建与发布策略

第一阶段目标构建矩阵：

- macOS arm64；
- Windows x64。

Python sidecar 必须在目标平台原生构建，不假设一个二进制跨平台运行。

首选 sidecar 打包方式：

- 先用 PyInstaller 形成平台独立可执行文件；
- 如 PyInstaller 对依赖处理出现不可接受问题，再评估 Nuitka；
- 不要求最终用户安装 Python。

Tauri 打包：

- macOS `.app` / `.dmg`；
- Windows `.msi` / `.exe` 安装形态按 Tauri 稳定支持选择。

代码签名、公证和 Windows SmartScreen 信誉是正式分发问题：

- 本地开发演示不以正式签名为阻断项；
- 向第三方分发前必须单独完成 macOS 签名/公证和 Windows 签名策略。

## 18. 测试策略

### Python Core

- 尽量移植 Web 版已有 pytest；
- SQLite 必须成为桌面默认测试数据库；
- R01—R20 规则回归测试必须全部保留；
- 文件解析、来源、隐私、匹配、删除测试必须保留。

### Desktop Sidecar

新增：

- sidecar boot / ready；
- localhost-only；
- session token；
- app data path；
- DesktopProcessingQueue；
- restart persistence；
- clean shutdown。

### React

- Vitest；
- Testing Library；
- 复用 Web 版业务文案和状态测试。

### Tauri

- Rust unit tests；
- sidecar lifecycle tests；
- macOS / Windows build smoke。

### E2E

至少包含：

```text
启动应用
→ 导入 synthetic 材料
→ 处理完成
→ Dashboard
→ 风险详情
→ 删除
```

## 19. 仓库结构建议

```text
/
  AGENTS.md
  README.md
  docs/
    architecture.md
    product-spec.md
    superpowers/specs/

  apps/
    desktop/
      package.json
      vite.config.ts
      src/
      src-tauri/

  python/
    pyproject.toml
    src/qian_labor/
    tests/
    desktop_entrypoint.py

  fixtures/
    synthetic/

  scripts/
    build_sidecar.py
    verify_desktop.py
```

说明：

- `python/src/qian_labor/` 作为从 Web 版抽取/迁移的业务内核；
- 不把 Web repo 整仓复制进来；
- 每个被迁移模块都要有来源和回归测试；
- 桌面仓库从此独立演进，Web 仓库保持冻结基线。

## 20. 分阶段实施顺序

### Phase 0 — 仓库基线

- README；
- AGENTS；
- Tauri/Vite/React scaffold；
- Python package scaffold；
- CI matrix；
- synthetic fixtures；
- 架构边界测试。

### Phase 1 — Sidecar + SQLite 垂直链路

- sidecar 启停；
- SQLite file database；
- app data path；
- localhost token；
- 创建 analysis；
- 文件导入；
- Fake Provider；
- Dashboard 最小数据链路。

### Phase 2 — 迁移业务内核

按依赖顺序迁移：

1. models / database；
2. upload validation / LocalStorage；
3. parsers；
4. AI schemas / Fake Provider；
5. matching；
6. risk rules / evaluation；
7. sources；
8. deletion；
9. report。

每一步必须有 Web 基线回归测试或等价桌面测试。

### Phase 3 — 完整 UI

- processing；
- matching review；
- Dashboard；
- finding detail；
- employee ledger；
- report；
- settings。

### Phase 4 — 真实模型

- GLM Provider；
- 安全 Key；
- synthetic real-provider smoke；
- 文本 + 扫描/图片路径；
- 隐私 Provider 边界验收。

### Phase 5 — 平台发布

- macOS arm64 build；
- Windows x64 build；
- sidecar / OCR 依赖打包；
- 安装、升级、卸载验证；
- 离线 Fake 演示包。

## 21. 明确非目标

桌面 MVP 不做：

- 多租户；
- 企业账号体系；
- 云同步；
- 多人实时协作；
- SSO；
- 云端 RBAC；
- 自动在线升级（首版可后置）；
- 完整 HR SaaS；
- 全量 Python→Rust 重写；
- 本地大模型推理；
- 自动作出辞退/解除决定。

## 22. 成功标准

桌面 MVP 达标时，应能对外准确描述：

> 企安用工 Desktop 是面向中小企业的本地优先劳动用工风险体检 Agent。Windows/macOS 安装后即可使用，无需企业部署 Docker、数据库或云服务器。原始劳动用工材料和风险台账默认存储在本机，系统在本机完成解析、员工归属、确定性规则计算和风险追溯；仅在需要模型语义理解时，将经过本地脱敏的最小必要内容提交给所配置的模型服务。

技术验收至少达到：

```text
MACOS_ARM64_BUILD=PASS
WINDOWS_X64_BUILD=PASS
SIDECAR_BOOT=PASS
SQLITE_PERSISTENCE=PASS
SYNTHETIC_IMPORT=PASS
FAKE_PROVIDER_PIPELINE=PASS
R01_R20_REGRESSION=PASS
MATCH_REVIEW=PASS
DASHBOARD=PASS
SOURCE_TRACE=PASS
DELETE_CLEANUP=PASS
NO_DOCKER_REQUIRED=true
NO_CLOUD_SERVER_REQUIRED=true
REAL_PROVIDER_SMOKE=PASS_OR_EXPLICITLY_NOT_RUN
```

## 23. 设计结论

本项目采用：

> **Tauri 2 + React + TypeScript + Vite + SQLite + Python sidecar**

Rust 负责桌面壳、系统能力、sidecar 生命周期和敏感凭据边界；Python 继续承载现有的文档理解、隐私处理、匹配和劳动规则业务内核；React 负责桌面 UI。

首要原则：

> **先把现有法律业务内核安全地装进桌面应用，而不是为了技术纯度重写一遍。**
