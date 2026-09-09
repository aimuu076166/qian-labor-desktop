# 企安用工真实模型闭环修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.
> 用户已要求新任务使用 **gpt-5.6-luna / max** 执行。本文件是公开执行方案；当前 Luna Max 会话按此方案修改、验证并记录门禁结果，未通过的门禁不会被标记为完成。
> 本方案是一轮新的、用户要求执行的有限修复任务，不是重新运行旧员工工作台八项改造。已经完成的旧任务不得重做。

**Goal:** 在现有员工工作台基础上修复真实模型分析、身份与来源、进度交互、原生报告打印，使同一套 10 名虚构员工、8 份中文混合材料在最终 Mac 包内真实可用；全部门禁通过才将 PR #2 转 Ready，绝不合并。
**Architecture:** 保留 Tauri / React / Python / SQLite、现有规则引擎与人工复核。以本地解析片段为证据，模型负责受约束的提取与独立条款意见，程序负责校验、归属、版本和任务状态。不通过放宽校验、自动确认或样本专用逻辑制造成功。
**Tech Stack:** Tauri 2、React 19、TypeScript、TanStack Query、Python 3.12+、Pydantic、httpx、SQLAlchemy/SQLite、现有 DOCX/XLSX/PDF/OCR 解析器、pytest、Vitest、Rust tests。

---

## 0. 约束、权限、资料顺序

### 0.1 固定目标

- GitHub：aimuu076166/qian-labor-desktop。
- PR：https://github.com/aimuu076166/qian-labor-desktop/pull/2。
- 唯一发布分支：release/v0.1.0-rc-packaging；禁止修改 main、force push、合并、tag 或正式 Release。
- 方案起点 HEAD：a16bcdebc493f0061360eacc9632f56461ac41a4；tree：ca33cb9b3b1f4be3c9a0ddced7f286020c6d1619。
- 启动时再次核对本地与远端，不能把本方案记录当成实时 Git 状态。
- 只交付 Apple Silicon Mac RC，最终文件留在原 GitHub 仓库的 Actions 产物体系，不额外放下载目录或桌面。
- 主模型仍为 glm-5.3-flash，尊重用户已保存的服务地址；不得擅自换模型、重置成普通按量端点或增加 Keychain 密码步骤。
- 仅排除工资表、考勤表的核算比对及工资/加班/补偿金额计算；合同工资条款审核保留。
- 首页仍是当前员工工作台；导入是入口按钮。不得增加登录、云同步、HR/薪酬模块、Word 编辑器或通用模型平台。
- 正式数据、历史报告、原始提取结果不得覆盖或清空。所有测试只用明确标记的合成资料。

### 0.2 先读再做

1. 仓库 AGENTS.md。
2. 本方案完整正文。
3. docs/superpowers/specs/2026-08-27-qian-labor-desktop-architecture-design.md。
4. docs/superpowers/specs/2026-08-31-qian-labor-desktop-real-app-closure.md。
5. docs/superpowers/specs/2026-09-04-local-private-provider-secret-store-design.md。
6. docs/superpowers/specs/2026-09-04-glm-5-3-flash-unification-design.md。
7. 私有临时交接文档指向的实际验收记录、旧 progress.md 和 execution-plan.md。旧方案与后来用户明确确认的范围冲突时，以本方案及用户最新指令为准，记录具体裁决。
8. 如需改真实 API 参数，先核对官方文档 https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash 和 https://docs.bigmodel.cn/cn/coding-plan/tool/others；不能凭记忆声称套餐不支持或启用不存在的参数。

### 0.3 安全与继续条件

- 用户已授权原分支推送与正常 macOS Git 凭据访问，也已授权这八份合成材料通过现有智谱 Coding Plan 分析。新任务仍必须遵守其当时实际的文件、网络和应用操作权限；若权限工具要求确认，就按正规流程请求，不绕过。
- 不读取、复制或输出原始 API Key、pepper、系统登录密码；不得用 shell 导出钥匙串内容。已有 Key 只让应用经其既有代码使用。
- 不向模型发送验收答案、source-observations.json、manifest、计划文件或真实员工资料。
- 实际模型调用只针对批准的合成材料，限诊断所需失败材料与最终整体验收；记录调用数、已知用量和重试。没有费用数据时写未知，不能把失败记录中的 0 token 宣称为免费。
- 新下载软件启动、覆盖安装、删除、数据外发如触发产品的即时确认，必须取得批准。不得覆盖用户 /Applications 中安装的旧 App；测试新包用独立临时安装位置。
- 不修改或终止无法确认归属的进程。启动前后记录测试 App 与 sidecar 的 PID/路径，避免同 bundle ID 的旧 App 干扰。
- 本轮由 Luna Max 主执行，按顺序修改；不要让多个写入者同时改 processing.py/App.tsx。独立代码复核可另派只读 Luna Max 审查席，不能让实施者自评代替独立复核，不擅自切其他模型。
- 不因普通子任务完成反复询问“继续吗”；真正缺权限、存在不可逆风险、需要扩展产品范围或无法形成安全修复路径时，说明具体阻碍再询问。
- 每步记录到本轮独立进度文件；保留旧工作台任务账本，不覆写、不删除它。每阶段写测试命令、真实结果、SHA、遗留问题。无证据不得标 complete。

## 1. 已证实的失败基线

这是旧 a16bcde 安装包的真实模型测试，不是新修复后的结果：

| 材料 | 旧结果 | 本轮必核内容 |
| --- | --- | --- |
| 01 员工花名册 CSV | processed，10 条事实 | 10 个明确工号及入职日期、来源行、可确认的员工身份 |
| 02 劳动合同与工资条款 DOCX | 两次 AI_TIMEOUT，各约180秒 | 10 人签约/期限/工资条款；意见与事实分开 |
| 03 试用期 XLSX | AI_SCHEMA_INVALID | 10 人试用期日期、考核记录，不误解释为考勤核算 |
| 04 续签通知 PDF | processed，17 条事实；第一页员工归属遗漏 | 两页各5人；拟续签不得成为已签署/已生效事实 |
| 05 社保 CSV | processed，21 条事实；来源未定位 | 工号、2026-08、缴纳主体及原文备注，不能因备注推造缴费事实 |
| 06 解除通知 PNG | processed，5 条事实，部分未定位 | SYN-010、解除与签收日期、原图证据；同一日也不能混淆事件含义 |
| 07 解除通知扫描 PDF | processed，5 条事实，来源未定位 | 与06同源内容语义一致，扫描页真实定位 |
| 08 嵌图 DOCX | AI_SCHEMA_INVALID，旧解析器仅提示有嵌图 | 正文 SYN-001；内嵌图片 SYN-010，不可按整文件单一身份串人 |

- 最终5份 processed、3份 failed，共58条事实；41条 unlocated_needs_review、17条 locally_located。
- 10名员工产生29条匹配候选，候选数不是员工数。旧原生UI只确认过1名，仍28条待处理，不能当10人验收通过。
- 共10条调用记录：6成功、4失败。花名册恢复后没有重复调用；两次任务最终均 in_flight=0。
- 进度长期5%，材料完成信息要导航或状态版本变化才刷新。
- 一名员工建档、确认原值、保存事实重评、冻结报告、Mac原生保存PDF功能成功。18页PDF有分页/页边问题，不是版式验收通过。
- Python801、前端263、Rust28及CI曾通过，只证明旧版本对应检查；不能复用为新修改通过凭据。
- AI_SCHEMA_INVALID 的具体不合格字段当前没有证据。禁止在诊断前宣称已经找到该字段或把锅归给服务商。

## 2. 文件责任与变更边界

以下均为从仓库根解析的路径；工作目录的绝对地址在私有交接文件中。

| 文件 | 本轮责任 |
| --- | --- |
| python/src/qian_labor/ai/zhipu_provider.py | HTTP响应诊断、结束原因、模型输出契约、有限等待 |
| python/src/qian_labor/ai/providers.py | 保持安全错误接口；确需时增加不含原文的诊断元信息 |
| python/src/qian_labor/ai/schemas.py | 最小兼容的来源引用/模型契约；不放松业务事实类型 |
| python/src/qian_labor/ai/grounding.py | 本地来源编号、定位、身份约束；版本化缓存 |
| python/src/qian_labor/parsers/protocols.py | 嵌图与来源上下文的最小协议扩展 |
| python/src/qian_labor/parsers/registry.py | DOCX内嵌图提取、真实顺序/位置及资源限制 |
| python/src/qian_labor/jobs/processing.py | 有边界的分块、结果完整性、逐文件进度、事实归属 |
| python/src/qian_labor/jobs/control.py | 仅当请求/分块处理需调整时接入原有取消检查点 |
| python/src/qian_labor/matching/service.py | 保持显式归属、事务和版本校验，不自动跨企业合并 |
| python/src/qian_labor/desktop/workspace.py、schemas.py | 前后端已有状态字段的真实传递；最小兼容扩展 |
| apps/desktop/src/App.tsx | 当前公司/分析/后端实例下的刷新与运行中操作限制 |
| apps/desktop/src/features/processing/ProcessingPanel.tsx | 文件名、阶段、完成/失败数，不伪造进度 |
| apps/desktop/src/features/matching/MatchingReview.tsx | 按员工候选组织、来源可见、逐项结果可恢复 |
| apps/desktop/src/lib/errorMessages.ts | 与安全后端错误一一对应的用户提示 |
| apps/desktop/src/features/report/ReportView.tsx、ReportVersions.tsx | 草稿/冻结版本状态、打印内容边界 |
| apps/desktop/src/styles.css | 打印页边、长内容分页和窄屏不裁切 |
| docs/release/v0.1.0-rc.1-checklist.md | 只记录真实新验收；不把NOT_RUN改成PASS |

不做大规模重命名或重构。不增加数据库服务。若实现确实需要持久化结构迁移，必须单列必要性、备份/事务/回滚和旧库升级测试；不能为了方便直接改用户库。

## 3. 执行步骤与停止门槛

### Task 0：锁定现场、建立新账本

- [ ] 读取以上资料，列明旧已完成任务与本轮新增任务；检查无其他写入者。
- [ ] 在真实 release worktree 中执行：

```sh
git status --short
git branch --show-current
git rev-parse HEAD
git rev-parse --git-common-dir
git remote -v
git diff --check
```

预期：原分支、基线SHA；唯一已知新增文件应包括本计划。发现其它变更先逐项辨别所有者，不能stash/reset/覆盖。
- [ ] 通过Git或已认证GitHub工具刷新PR2 head/draft/merged、远端分支；意外变化先核对，不自动改 main 或强推。
- [ ] 将本轮账本、测试日志、审查diff放独立临时工作区；公开计划不得带本机账号路径、数据库内容或密钥。
- [ ] 复核合成样本SHA与原8份文件；原件保留不改。若临时原件丢失，用仓库已有 synthetic_chinese_materials.py 生成并记录新hash，说明容器格式元数据差异，不能假称字节相同。
- [ ] 建立验收表，以原始材料/source-observations为答案来源，不以模型输出或生产grounder反推期望值。

### Task 1：先把错误变成可定位证据

覆盖：zhipu_provider.py、providers.py、errorMessages.ts、processing错误链；测试 python/tests/regression/test_zhipu_provider.py 与 python/tests/desktop/test_processing_failure_codes.py。

- [ ] 先追加可运行的失败测试，至少包括“有效JSON但finish_reason=length仍不能成功”：

```python
def test_length_finish_reason_rejects_even_parseable_json():
    payload = _success_response().json()
    payload["choices"][0]["finish_reason"] = "length"
    def handler(request):
        return httpx.Response(200, json=payload)
    with pytest.raises(AIProviderError):
        _provider(handler).extract("synthetic.txt", b"SYN-001 signed contract")
```

该测试追加在现有 test_zhipu_provider.py，复用文件中真实已有的 helper/import；不是生产代码的新API。
- [ ] 逐个加JSON不完整、消息content为空、非对象JSON、非法字段、互相冲突的value字段、401/429/500、超时、用户取消测试；所有输入均合成。
- [ ] 安全诊断仅允许：HTTP状态、固定结束原因枚举、耗时、尝试次数、输入/输出长度、固定校验错误类型和预定义schema字段路径。字段路径中的自由字典key也要白名单化。
- [ ] 不输出 Pydantic errors 的 input/ctx、response.text、请求头、源excerpt或完整request/response。API错误与日志各做敏感信息泄漏断言。
- [ ] 分开记录JSON/结构/语义类别，但保持对外稳定安全错误接口；原AI_SCHEMA_INVALID仍可作为兼容总类，不能令前端未知错误静默。
- [ ] 校验 finish_reason；截断即使JSON可解析也不得完整成功。Provider数据先通过约束再入库，禁止过滤坏事实后报告整份成功。
- [ ] 在离线错误链测试通过后，才通过正常应用重跑原失败材料的最小诊断集。记录真实错误类型；没有原始失败回复就明确它已无法回放，不编造“真实响应fixture”。

验证：
```sh
python/.venv/bin/python -m pytest python/tests/regression/test_zhipu_provider.py python/tests/desktop/test_processing_failure_codes.py python/tests/desktop/test_processing_failures.py -q
pnpm --dir apps/desktop test --run tests/processing-errors.test.tsx tests/settings.test.tsx
```

门槛：测试无失败，诊断中无敏感原文；给出每个已复现失败的证据和修复方向，再进入下一步。不能只新增日志便标分析已修复。

### Task 2：修复分块、请求完成性及嵌图处理

覆盖：protocols.py、registry.py、grounding.py、processing.py、zhipu_provider.py。
测试：test_grounded_extraction.py、test_chinese_materials.py、test_task_lifecycle.py，以及新增 test_extraction_chunking.py。

- [ ] 先加原文全覆盖测试：每个非空解析块必须至少属于一个请求；只有明确作为上下文的表头可重复；不得静默截断。
- [ ] 表格不能拆散同一行的工号与事实单元格。正文按真实段落边界拆；PDF保留原页号；所有请求保存原文件及原位置映射。
- [ ] 新增分块器作为纯函数，输入 ParsedDocument/原文件上下文，输出有限 ExtractionInput 列表。首选以文本字符预算和完整行/段边界切分；具体预算先根据Task1记录确定并写入常量与边界测试，不凭“更长超时”掩盖响应体过大。
- [ ] 不默认按十个人发十次请求，不并发轰炸；优先紧凑分块。保留总调用计数和单次超时，不能引入无限自动纠错/重试。
- [ ] 一个文件的某块失败，文件不能被标为完整processed；已完成其它文件仍保留。同一文件结果合并时防重、不得丢未处理块。调用记录与结果写入服从原有取消/commit_boundary。
- [ ] DOCX提取内嵌 raster image 的本地关系资源，保留图片所属段落/表格/序号；不联网获取外链，不执行宏，不执行ZIP路径。图片大小/数量/解码像素限制沿用或严于现有解析边界。
- [ ] 将图片走已有本地脱敏/OCR/视觉请求路径，不能直接把未脱敏图片发外网。若图片定位只能达到“段落内第1图”，诚实表示，不伪造Word页码。
- [ ] 第08份正文SYN-001与图片SYN-010必须各保留归属。未支持的图形必须显式部分完成/阻断，不能算支持该份样本验收通过。
- [ ] 不默认启用流式或关闭thinking。若Task1证明必须改传输模式，先验证官方支持，再单列SSE半帧、终止标记、仅思考无正文、断流、usage、取消与超时的测试；这些通过前不得替换现有路径。
- [ ] 修改提取/来源契约时升级缓存版本。旧成功文件缓存只在版本与配置符合既有规则时复用；旧失败不得伪装成功。不要无理由改变parse缓存或报告历史。

至少加入以下真实解析测试到 test_chinese_materials.py（现有imports可直接复用）：
```python
def test_embedded_image_has_a_vision_input_not_only_a_warning():
    materials = build_materials()
    parsed = ParserRegistry().parse(
        "08-嵌图合同待核对.docx",
        materials["08-嵌图合同待核对.docx"],
    )
    assert parsed.needs_vision
    assert len(parsed.vision_pages) >= 1
    assert all(page.image_bytes for page in parsed.vision_pages)
    assert any("SYN-001" in block.text for block in parsed.blocks)
```

若最小协议选择新增嵌图集合而不是vision_pages，需在生产修改前同步测试的具名契约并记录理由；不能删除“真实图片字节及上下文存在”的断言。

验证：
```sh
python/.venv/bin/python -m pytest python/tests/desktop/test_grounded_extraction.py python/tests/desktop/test_chinese_materials.py python/tests/desktop/test_extraction_chunking.py python/tests/desktop/test_task_lifecycle.py -q
```

门槛：8份解析内容完整且受限；一块失败/取消不会给出假完整结果；重试/恢复不重复提交或串任务。

### Task 3：本地来源绑定与员工/事件语义

覆盖：grounding.py、schemas.py、processing.py、matching/service.py；新增 test_evidence_identity.py，扩展 test_grounded_extraction.py、test_matching_api.py。

- [ ] 给解析块生成稳定、文件内唯一、可复算的引用ID；ID与原文、原位置、材料hash关联。Provider只能引用本次请求实际给出的ID。
- [ ] 从本地映射取位置，模型返回位置只能作校验提示；伪造/矛盾提示不能被悄悄修正成可信。
- [ ] 精确quote匹配保留，仅规范化既有允许的空白/脱敏形式。不用模糊相似度冒充来源证据。
- [ ] 表格一条事实可用事实单元格及同行工号共同证明；不能因为日期单元格本身没有工号就丢掉真实同行归属。跨行同日期不得串人。
- [ ] 图片/扫描页沿用真实OCR坐标，无法证明坐标时只到图或页级并标待复核，不让模型虚构精确bbox。
- [ ] 多人材料中不因document-level employee_number把所有事实归给同一个人；每条事实须有本地可核对身份或明确未归属。
- [ ] 同一公司相同明确工号可作为分组键，跨公司、同名、缺工号、互相矛盾编号不得自动合并。永久EmployeeRecord仍须既有显式用户确认。
- [ ] 将“计划/拟/待签/草案”与已发生状态分开。现有fact catalog无法表达时保留为条款观察或待确认候选，不自创规则事实，也不把缺日期填今天。
- [ ] 新增负例：拟续签→不能证明已签、通知送达日→不能自动当解除日、主体备注→不能自动当已缴费、正文001/图片010→不能串人。
- [ ] 第08图片的真值要从原图确认。原source-observations只列正文而未枚举嵌图，不可据此忽略图片；扩展独立测试oracle，不改原材料让答案变容易。

以下独立负例追加到 test_grounded_extraction.py，可用现有result helper：
```python
def test_same_date_other_employee_does_not_ground_a_fact():
    from qian_labor.ai.grounding import ExtractionInput, ground_result
    from qian_labor.parsers.protocols import ParsedBlock
    item = ExtractionInput(
        "synthetic.txt", b"",
        (
            ParsedBlock("SYN-001", "table_cell", {"table": 1, "row": 1, "column": 1}),
            ParsedBlock("SYN-002", "table_cell", {"table": 1, "row": 2, "column": 1}),
            ParsedBlock("2026-01-01", "table_cell", {"table": 1, "row": 2, "column": 2}),
        ),
    )
    extracted = result("2026-01-01", table=1, row=2, column="2")
    proofs = ground_result(extracted, item, "synthetic.txt")
    assert proofs[0]["status"] == "unlocated_needs_review"
    assert extracted.facts[0].needs_human_confirmation
```

此负例可作为原实现已通过的保留控制，不能谎称每个新增测试都是RED；另加Task1/Task2真实失败与同行正确绑定的RED。

验证：
```sh
python/.venv/bin/python -m pytest python/tests/desktop/test_grounded_extraction.py python/tests/desktop/test_evidence_identity.py python/tests/desktop/test_matching_api.py python/tests/desktop/test_effective_facts.py python/tests/desktop/test_company_workspaces.py -q
```

门槛：明确的10人身份和关键事实有证据；新增定位率不能以错位/误归属换取；无证据内容不进入已确认规则结果。

### Task 4：匹配流程与实时进度

覆盖：App.tsx、ProcessingPanel.tsx、MatchingReview.tsx、workspace/schema及errorMessages。
测试：task-lifecycle.test.tsx、matching.test.tsx、company-workbench.test.tsx、grounded-materials.test.tsx、effective-fact-workbench.test.tsx。

- [ ] 使用fake timers+延迟响应写RED：task版本不变而第二份材料完成，页面仍必须更新；不是只测试某个“设置state”的helper。
- [ ] 运行中对当前公司/分析/后端generation限定刷新；使用已有query机制，建议1秒级读取，文件完成后3秒内UI更新为本轮工程验收值，不宣称跨网络SLA。
- [ ] 切换A→B后，A的晚响应不能覆盖B；退出页面或任务终止停止无意义轮询；读取失败保留已有材料并显示重试，不自动重新提交模型任务。
- [ ] 显示当前文件、解析/模型处理/待核对阶段、完成数和失败数；无可信token进度则用阶段文字，不编造“分析到87%”。
- [ ] 完成一份后由后端更新聚合状态，不能只在整批尾部从5跳90。百分比若保留，要明确是文件处理进度，不代表分析正确率。
- [ ] 任务处理中禁用创建/匹配等后端busy操作并说明原因；保留服务器busy兜底，不能只靠前端禁用。
- [ ] 匹配界面按明确员工标识分组，展示每条原始材料/候选/事实；编号不明单列。用户仍能逐项检查、纠正与明确确认。
- [ ] 不新增一键盲目确认所有人。复用现有逐候选写入接口；如果按组确认逐个提交，必须显示成功/失败/未知的具体成员，只重试明确失败项，未知写入先GET核对，不重放整组。
- [ ] 已建立员工再次出现同工号，优先显示明确候选供确认，不让用户重复建档。原有版本冲突、原UUID、脱敏canonical-name核对逻辑不得回退。
- [ ] 取消立即显示“正在取消”；不再启动下一请求。正在远端进行的请求不承诺即时撤回或免收费；恢复列表明确可复用/重做项。

验证：
```sh
pnpm --dir apps/desktop test --run tests/task-lifecycle.test.tsx tests/matching.test.tsx tests/company-workbench.test.tsx tests/grounded-materials.test.tsx tests/effective-fact-workbench.test.tsx
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop lint
```

门槛：实际App组件测试通过，切换/取消/未知请求回归通过；浏览器合成测试与最终原生App实测分别记录。

### Task 5：报告状态、打印与版本完整性

覆盖：ReportView.tsx、ReportVersions.tsx、styles.css；必要时services/report.py，但不能GET时重写冻结payload。
测试：report.test.tsx、report-versions.test.tsx、test_report_versions.py。

- [ ] 写状态测试：未生成实时草稿、已保存但事实未确认的版本、旧版本过期、当前新版本；选择冻结版本后不能继续显示“尚未锁定”的矛盾状态。
- [ ] 显示的是当前选中版本的公司/范围/分析日期/结果及审核版本；旧报告不跟随当前员工修改偷偷变内容。
- [ ] 失败材料、未归属事实、关键待确认项在报告中可见，0风险不等于安全。不能为缩短页数隐藏重要缺口。
- [ ] 合并重叠print CSS，去除不适合打印的巨大留白和卡片宽度；保持屏幕样式正常。
- [ ] 先采用以下打印基线，再以原生WebKit渲染结果调优；超长内容允许分页，不能整卡break-inside:avoid造成溢出：

```css
@page { size: A4; margin: 16mm 14mm; }
@media print {
  .report-view { width: auto; max-width: none; overflow: visible; }
  .report-view h2, .report-view h3, .report-view h4 { break-after: avoid; }
  .report-view p { orphans: 3; widows: 3; }
  .report-view table { width: 100%; table-layout: fixed; }
  .report-view th, .report-view td { overflow-wrap: anywhere; }
  .report-view thead { display: table-header-group; }
  .report-view tr { break-inside: avoid; }
}
```

若一行本身超页高，要允许该行可读续页，不能照搬avoid造成裁切；为长单条材料补测试。
- [ ] 页码若原生打印链路可靠支持则加入；不得画一个重复“第1页”或为了页码引入新的打印引擎。不能支持时明确说明，裁切/乱码/内容遗漏仍为硬阻断。
- [ ] 通过最终App调用真实macOS打印→保存PDF，渲染所有页并视觉检查；不能只用浏览器生成PDF或CSS快照替代。
- [ ] PDF仅作为临时验收证据，最终GitHub证据只放无敏感合成信息和需要的摘要，不复制用户材料。

验证：
```sh
pnpm --dir apps/desktop test --run tests/report.test.tsx tests/report-versions.test.tsx
python/.venv/bin/python -m pytest python/tests/desktop/test_report_versions.py -q
```

门槛：报告不失真；长短两种合成报告可读打印；旧版本hash不变。

### Task 6：整合、全量测试、独立复核

- [ ] 各Task覆盖测试全部通过后冻结diff，不一边审查一边改。
- [ ] 记录基线SHA、当前提交/工作树hash、逐文件清单。检查新代码不含SYN-001之类验收专用分支、真实Key、私有绝对路径。
- [ ] 运行以下现有门禁；如node/pnpm/cargo不在PATH，读取本机runtime配置或交接记录，不能删除对应测试来通过：

```sh
python/.venv/bin/python -m pytest python/tests -q
python/.venv/bin/python -m compileall -q python/src python/tests scripts
pnpm --dir apps/desktop test --run
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop lint
pnpm --dir apps/desktop build
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --all --check
python/.venv/bin/python scripts/verify_desktop.py
python/.venv/bin/python scripts/scan_sensitive.py
python/.venv/bin/python scripts/scan_public_history.py --repo .
git diff --check
```

- [ ] 同时对新增未跟踪文件做敏感扫描；最终暂存后再跑，不能只扫tracked旧文件。
- [ ] scripts/verify_desktop.py 是Fake Provider证据，scripts/real_provider_smoke.py若缺Key为NOT_RUN，不得声称真实分析已通过。
- [ ] 独立只读审查本轮diff，重点：隐私、schema fail-closed、跨员工归属、数据旧版本、取消/未知请求、报告真实性。需要独立Luna Max审查席时只给范围和文件，不要求它迎合既定设计。
- [ ] 每条Critical/Important必须解决并做定向复核；不能以审查轮次或赶时间豁免。发现必须扩产品范围/不可安全修复的结构问题，就报告证据和有限选择，不无限循环也不虚报完成。
- [ ] 只提交本轮确认的源码、测试、公开计划与必要文档，不提交临时DB、原始日志或整个临时安装目录。提交后新HEAD全量门禁必须与实际变更对应。

### Task 7：真实模型与最终Mac包验收

- [ ] 开发修复后先用原合成样本进行必要真实模型验证；模型请求只能由正常应用配置链路发送。
- [ ] 推送原release分支，监控该HEAD的desktop-ci及desktop-rc。不要反复触发新构建试图掩盖软件失败。
- [ ] 从成功run下载最终aggregate artifact，独立核对ZIP digest、内部SHA256、manifest HEAD、架构及bundle。CI成功不是业务验收成功。
- [ ] 将该包安装到独立临时位置，经允许后从正常应用入口打开，保持用户原安装与资料不变。
- [ ] 在最终App里完成下面的整轮操作，不能由数据库写入代替点击：

1. 首页打开，模型未配置时仍能浏览/导入；已配置时不重置用户地址或要求系统密码。
2. 新建明确合成测试企业；导入原8份文件，核对文件hash及实际数量。
3. 开始真实分析，观察逐文件变化。若需要演示取消，已完成文件保持，恢复列表与真实请求次数一致。
4. 10人来源识别完成，按原文确认身份；不伪造自动通过。不得重复员工、跨公司或正文/图片串人。
5. 对照原始资料逐项核验员工与关键日期、合同期限、试用期、社保主体/月份、解除/送达、工资条款。原文可确认不代表法律意见已验证。
6. 所有模型事实核查其材料和位置；关键事实缺失为失败。非关键模糊原文可以待复核，但不能把清晰的样本整体标模糊来过关。
7. 确认一个真实原值，再修订一个合成值，原始提取不变；影响结果变旧，点击本地重评后各视图一致，模型调用数不增加。
8. 生成冻结报告，保存原生PDF，检查全部页面；修改事实后旧报告hash不变，状态明确过期。
9. 正常退出再打开，企业/10人/材料/任务/修订/报告均恢复；无非预期自动模型请求。
10. 仅删除新测试企业/材料时，经所需批准验证受控删除，旧用户企业与文件不受影响。验证前后进程归属，不粗暴killall。

- [ ] 失败记录包含：HEAD、artifact/run、系统/架构、文件hash、实际步骤、请求数、错误类型、期望/实际、是否复现；无原文/Key泄漏。
- [ ] 如果最终包失败，保持Draft，修复后必须生成新HEAD对应包重新验收。旧PDF、旧App、旧CI不得拼接为新通过证据。
- [ ] 若验收之后仅补公开报告文档产生新HEAD，严格重新构建并核对该HEAD包；不要形成“文档又改HEAD”的无限循环。最终实测证据优先写PR描述/评论，避免证据提交再次改变已验收HEAD。

## 4. 交付判定表

| 门禁 | 必须取得的结果 |
| --- | --- |
| 来源完整 | 原8份保留；支持的正文/表格/图片均进入处理，不静默丢块 |
| 模型可用 | 无未解释的timeout/schema失败；不靠隐藏失败伪造8/8 |
| 10人身份 | 10个明确工号归属正确；同名/冲突负例仍人工门控 |
| 关键语义 | 拟续签不当已签，解除/送达不混淆，正文与嵌图不串人 |
| 追溯 | 清晰关键事实定位到真实来源，模型位置不能自证 |
| 交互 | 逐文件实时反馈，运行中禁用无效动作，未知写入先核对 |
| 持久化 | 重开后材料、10人、修订、报告一致，无隐式模型重跑 |
| 报告 | 当前/旧版本明确，资料不足可见，PDF无内容遗漏/裁切/乱码 |
| 工程 | 新HEAD全量测试、独立复核、CI、包hash/manifest一致 |
| 发布 | 上述全部满足才PR2 Ready，禁止merge/tag/formal Release |

8份中一份仍失败不是“基本完成”。模型不确定性可以诚实表达，但不能成为清晰验收材料缺失的普遍豁免。不得承诺识别所有现实世界文件100%正确，承诺的是本轮样本和负例可复现、结果可追溯、失败可处理。

## 5. 交付给用户的最终说明

必须包含：本轮具体修复、最终HEAD、PR状态、仅一个明确的Mac下载入口、安装/未公证限制、完整10人/8文件验收摘要、仍存在的非阻断限制和证据链接。不把中间候选包交给用户反复试错。

若阻断则不提供“已验收下载”说法，逐项说明未达门槛、证据和需要用户决定的实际事项。不要再泛泛要求用户重新确认整个旧方案。

## 6. 编制自检

- 覆盖本次四项故障，未引入登录/薪酬/云端新产品。
- 没有把不合格字段、超时根因等未知信息写成已确认结论。
- 明确第08的双员工正文/图片边界，补足原观察清单的图像盲区。
- 测试控制与真正RED区分；既有fixture/helper有明确位置。
- 分块预算、可能的SSE实现须服从先诊断后决策，禁止按此计划直接猜测远端协议。
- 合成样本、真实调用、最终安装包三种证据区分。
- 计划与私有交接分离；本公开文件不含凭据、真实员工材料或用户私有路径。
