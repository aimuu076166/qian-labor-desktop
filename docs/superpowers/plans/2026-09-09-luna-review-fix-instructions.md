# Luna Max 返工执行方案 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> 这是用户在独立复核后明确要求的返工。执行模型保持 gpt-5.6-luna / max。继续现有执行任务，不创建新会话、不复制派发、不重做已完成工作。
> 本文件补充并优先约束同目录 2026-09-09-real-model-acceptance-repair.md；原方案未完成的门禁全部有效，不得缩减。

**Goal:** 修复独立复核发现的三项具体缺陷，补齐响应诊断、来源和交互遗漏，并在最终精确HEAD的Mac包内完成真实10人/8材料闭环。
**Architecture:** 保留原桌面架构、规则语义和人工确认。以原始解析位置及本地验证为证据，分块单位必须保持身份上下文；旧提取缓存显式升级，历史结果不静默修改。
**Tech Stack:** 现有Python/Pydantic/httpx/SQLite、React/Tauri、pytest/Vitest；不增加服务端或新产品模块。

## A. 当前现场与复核裁决

- 已核对本地提交 f6ee945a99cbdf8f7836db77a2e7ed480043fc22，文件树905523c0e7b45098189ad6432f22f6e087bb28e1，工作区干净。
- 远端此前核对head20b173dd3d16095331f6280b29e3e64681cd4df3，PR2 Draft未合并。启动必须再查，不把上述快照当实时状态。
- 原分支 release/v0.1.0-rc-packaging，禁止main/强推/merge/tag/正式Release。
- 旧账本把Task1–5写成“offline complete”，不代表满足原任务；本次独立复核只重跑33个相关测试，33通过仍未覆盖以下新复现。
- 原超时合同序列化输入实际2531字符、XLSX3843字符，新16000字符门槛对两者均仍产生1个请求。分块代码存在，不等于原超时根因已修复。
- 对旧验收库只读查询证实，5份旧processed材料的新代码extract key仍能匹配原succeeded记录。
- 嵌图OCR定位复现：段落2/图片1上下文未进入最终source，结果只剩page=1与bbox，proof可为locally_located且requires_review=false。
- 原始材料08正文SYN-001、嵌图SYN-010，必须分别处理。不得以整文件主员工覆盖图片员工。
- 以下步骤只修原约定范围。保持隐私边界，不读取API Key/pepper，不修改原样本答案，不让用户承担本应由任务完成的验收。

## B. Task R0：先核对并纠正账本

- [ ] 完整读原方案、本返工单、AGENTS及私有交接。真实worktree地址由已有HANDOFF提供，每条命令显式使用该目录。
- [ ] 确认没有第二个写入者；原审查会话不会改生产代码。
- [ ] 执行git status --short、branch --show-current、rev-parse HEAD、rev-parse HEAD^{tree}、diff --check；刷新PR2与远端head。
- [ ] 本地f6与远端20不同，先用正常fetch获取对象并验证完整tree；确认同树且无未提交代码后再安全对齐。保留本返工计划，禁止reset --hard或人工重拼整个App.tsx。
- [ ] 在现有Luna账本追加“独立复核返工”，将旧Task1–5细分为已实现/缺失/未验收，不覆盖历史记录。
- [ ] 为每个R1–R7记录baseline、RED、实现diff、GREEN、未通过项。不得在实际包验收前写本轮完成。

## C. Task R1：表格必须按完整行分块

**修改：** python/src/qian_labor/jobs/processing.py。
**测试：** python/tests/desktop/test_extraction_chunking.py。

现有chunk以ParsedBlock为单位，而表格一个单元格也是一个block；“不拆block”不能保证“不拆员工行”。

- [ ] 将以下完整测试追加到现有test_extraction_chunking.py，运行确认在返工前失败：

```python
def test_chunk_boundary_keeps_employee_and_date_in_same_table_row():
    identity = ParsedBlock("SYN-001", "table_cell",
                           {"table": 1, "row": 1, "column": 1})
    value = ParsedBlock("2026-01-01", "table_cell",
                        {"table": 1, "row": 1, "column": 2})
    parsed = ParsedDocument("docx", [
        ParsedBlock("A" * 15866, "paragraph", {"paragraph": 1}),
        identity, value,
    ])
    inputs = ProcessingPipeline._extraction_inputs("synthetic.docx", b"", parsed)
    identity_chunk = next(i for i, item in enumerate(inputs) if identity in item.blocks)
    value_chunk = next(i for i, item in enumerate(inputs) if value in item.blocks)
    assert identity_chunk == value_chunk
```

- [ ] 在序列化预算前建立原子单元：同一文件中相同(sheet, table, row)的整行单元格一起；正文段落独立；PDF页面不串页。同表头按需要作为明确上下文重复，不能重复成事实。
- [ ] 先按原子单元计算预算，再装箱到请求。超过预算的一整行不能被切开或丢掉；须在该文件第一模型调用前明确报长度限制，保持可恢复。
- [ ] 增加CSV、XLSX、DOCX table各一例；相同row但不同sheet/table不得拼成一行。
- [ ] 增加表头、重复日期、超长单行、多个块刚好在预算边界、取消后不启动下一块的测试。
- [ ] 原文所有数据块至少出现一次，非上下文块不得无理由重复；不增加样本工号专用代码。

**验证：**
```sh
python/.venv/bin/python -m pytest python/tests/desktop/test_extraction_chunking.py python/tests/desktop/test_grounded_extraction.py python/tests/desktop/test_task_lifecycle.py -q
```
**通过标准：** 新RED变GREEN；同一行工号和日期位于同请求；原有完整性/取消控制不退化。

## D. Task R2：显式升级提取缓存，保护历史

**修改：** ai/grounding.py、jobs/processing.py及desktop/workspace对needs_reextraction/恢复预览的传递。
**测试：** test_grounded_extraction.py、test_task_lifecycle.py、test_report_versions.py。

- [ ] 新增以下直接版本契约测试，旧实现必须失败：

```python
def test_v2_extraction_does_not_reuse_v1_key_but_keeps_parse_key():
    from qian_labor.ai.grounding import EXTRACTION_VERSION
    from qian_labor.jobs.processing import ProcessingPipeline
    assert EXTRACTION_VERSION == "parser-grounding-v2"
    assert ProcessingPipeline._job_key("a", "f", "parse", "digest") == "a:f:parse:digest"
    assert ProcessingPipeline._job_key("a", "f", "extract", "digest") != (
        "a:f:extract:digest:parser-grounding-v1:contract-advisory-v1"
    )
```

- [ ] 版本改为parser-grounding-v2，所有依赖该版本的缓存key/证据proof一致。不要给旧行批量改version让它假装新结果。
- [ ] 在临时测试库种入真实旧v1成功job、facts及advisory。断言current cached_extraction为false、needs_reextraction为true、恢复预览不列作可复用。
- [ ] 读取workspace、启动App或读取报告不能自动消耗额度；用户明确开始分析后才运行新提取。
- [ ] 重新提取前后保留旧提取、人工修订、冻结报告hash。新事实不能默默继承旧确认状态；旧版本报告按原快照展示。
- [ ] 同一v2版本、文件hash和既有配置条件一致时，恢复仍复用已完成文件；不得每次启动都全部重跑。
- [ ] 现有写死v1的测试逐个判定：历史兼容控制保留v1；“当前版本”断言更新v2。禁止一键全局替换历史schema/fixtures。

**验证：**
```sh
python/.venv/bin/python -m pytest python/tests/desktop/test_grounded_extraction.py python/tests/desktop/test_task_lifecycle.py python/tests/desktop/test_effective_facts.py python/tests/desktop/test_report_versions.py -q
```
**通过标准：** 旧v1不能绕过新检查；GET无模型调用；v2恢复有效；旧报告及人工记录不变。

## E. Task R3：嵌图保留真实位置，不编造Word页号

**修改：** parsers/protocols.py、registry.py、jobs/processing.py、ai/schemas.py、grounding.py；必要的桌面来源展示/报告类型同步。
**测试：** test_chinese_materials.py、test_evidence_identity.py、test_grounded_extraction.py及来源展示测试。

- [ ] 在test_chinese_materials.py加入如下RED（现有helper/import可直接复用）：

```python
def test_docx_embedded_image_has_context_but_no_invented_physical_page():
    materials = build_materials()
    parsed = ParserRegistry().parse(
        "08-嵌图合同待核对.docx", materials["08-嵌图合同待核对.docx"]
    )
    image = parsed.vision_pages[0]
    assert image.page is None
    assert image.locator["paragraph"] == 2
    assert image.locator["image"] == 1
```

- [ ] VisionPage.page允许None；DOCX内嵌图设None，PDF仍用真实页号。page_count计算只收集有效正整数，不能max(None,1)或把图片数当Word页数。
- [ ] SourceLocator新增可选image序号；旧payload缺字段必须能读取。image等位置值必须从本地解析上下文取得，不直接信模型。
- [ ] OCR块上合并本地嵌图上下文，使每条最终证据含paragraph/table/row/column/image及局部bbox。OCR实际坐标保留，不能只在空image_context块留元数据。
- [ ] 对DOCX图不写page。明确bbox是该图片内坐标，不是整个Word页面坐标；上下文冲突或定位不唯一强制复核。
- [ ] 检查POSITION_KEYS、SourceLocator转换、事实/条款grounding、存储JSON、员工详情及报告展示全链路，不得只把字段加在parser里。
- [ ] 按生产OCR合并路径组装一个带paragraph2/image1及可定位合成原文的案例，断言ground_result后的source.paragraph==2、source.image==1、source.page is None。
- [ ] 增加两张同文本图片、正文001图010、表格单元格内图片、模型伪造page99/image99、来源重复的测试。模型错误提示不得被悄悄替换后作为可信结果。
- [ ] 稳定片段引用按原计划补齐：文件hash+真实解析位置+块内容hash的确定性ID，只接受当前请求实际包含的ID；本地映射决定位置。伪造/跨文件ID拒绝，ID本身不能证明事实语义正确。

**验证：**
```sh
python/.venv/bin/python -m pytest python/tests/desktop/test_chinese_materials.py python/tests/desktop/test_evidence_identity.py python/tests/desktop/test_grounded_extraction.py python/tests/desktop/test_report_versions.py -q
pnpm --dir apps/desktop test --run tests/grounded-materials.test.tsx tests/report.test.tsx
```
**通过标准：** 事实和条款都能指到原段落/原图；不得再出现DOCX伪造第1页且无需复核的复现结果。

## F. Task R4：完成错误诊断，再解决真实超时/schema错误

**修改：** ai/zhipu_provider.py、providers.py、processing错误传递与lib/errorMessages.ts。
**测试：** test_zhipu_provider.py、test_processing_failure_codes.py、processing-errors.test.tsx。

- [ ] 不能把“只拒绝finish_reason=length”当成原计划诊断完成。保留兼容总错误码，但内部必须区分HTTP、超时、空正文、JSON、结构、事实契约、响应未完整结束。
- [ ] 新增安全诊断对象，允许固定category、HTTP状态、白名单finish_reason、elapsed_ms、attempt、输入/输出长度、预定义字段路径及固定校验类型；禁止response.text、Pydantic input/ctx、自定义key原文、请求头、API Key或员工原文。
- [ ] 逐项用MockTransport覆盖length、空content、非对象JSON、非法事实类型、value冲突、401、429、500、TimeoutException，验证界面能解释类别、材料不丢、无无限重试。
- [ ] 加含“synthetic-secret-marker”和“synthetic-person-text”的无效回复，断言诊断、异常字符串、日志均不包含这些marker。只允许本地测试输入中出现。
- [ ] 先对原合同2531字符与XLSX3843字符确认真实请求大小/耗时/结束原因/安全字段错误；不是再把16k阈值下调到恰好令样本通过就宣布解决。
- [ ] 真实请求使用已授权合成材料和正常App配置。脚本环境无AI_API_KEY只能说明脚本没配置；先核对应用UI已有配置和本机运行路径，不能要求用户粘贴Key进聊天或宣称无法测试。
- [ ] 若改分块策略，必须依据输出体量/多人结构诊断，保持整行/身份与上下文；若改stream/thinking/输出限制，先查官方文档并补相应半帧/断流/取消/usage测试。
- [ ] 对观察到的schema差异只做明确无歧义的兼容转换。非法或矛盾结果保留失败，不能丢坏事实转processed。记录真实根因仍未知的项，未知就是未完成。
- [ ] 原两份失败材料实际成功且逐条来源/语义符合要求，才可标本项完成；只跑模拟测试不得标真实修复通过。

**离线验证：**
```sh
python/.venv/bin/python -m pytest python/tests/regression/test_zhipu_provider.py python/tests/desktop/test_processing_failure_codes.py python/tests/desktop/test_processing_failures.py -q
pnpm --dir apps/desktop test --run tests/processing-errors.test.tsx tests/settings.test.tsx
```

## G. Task R5：补齐原交互/报告漏项

不能仅列候选按钮就宣称“按员工分组完成”；不能仅改print CSS就宣称报告原生验收完成。

- [ ] MatchingReview按当前企业内明确工号分组显示；未知与冲突独立，保留每条材料来源，仍逐项显式确认。不添加一键盲确认。
- [ ] 当前task active时，匹配/创建员工入口与提交明确disabled并说明原因，保留服务器busy兜底；不是只禁用submitting期间。
- [ ] Material页面与Processing页面都在当前活动分析下刷新。写真实App组件延迟响应测试：task版本不变但文件完成，3秒内刷新；切A→B不被A晚响应覆盖；终止/卸载停止轮询。
- [ ] 报告选中冻结版本时显示冻结状态，不继续出现“尚未锁定”的矛盾文案。事实变更只标旧报告过期，不能改旧hash。
- [ ] 超过一页高的表格行不能因break-inside:avoid而裁切；用真实原生打印确认后再决定CSS调整。
- [ ] 针对新增行为写失败测试。既有报告测试通过不等于新增要求已有覆盖。

**验证：**
```sh
pnpm --dir apps/desktop test --run tests/task-lifecycle.test.tsx tests/matching.test.tsx tests/company-workbench.test.tsx tests/report.test.tsx tests/report-versions.test.tsx
pnpm --dir apps/desktop typecheck
pnpm --dir apps/desktop lint
```

## H. Task R6：独立复核与全量门禁

- [ ] R1–R5修改完成、覆盖测试通过后冻结diff，提供每项RED/GREEN、真实调用诊断摘要、变更文件和当前SHA。
- [ ] 请求独立只读审查席复核本返工项和修复引入的回归，主执行不能自签通过。审查模型仍Luna Max，不擅自切其他模型。
- [ ] 审查重点：整行上下文、旧库缓存/历史保护、嵌图无伪页号、隐私错误诊断、来源ID防伪、active操作禁用、旧报告hash。
- [ ] Critical/Important不允许因测试总数多而豁免；修复后定向复核。原R01–R20规则语义不修改，资料不足不等于安全。
- [ ] 按原完整方案Task6运行Python全套、frontend全套/typecheck/lint/build、Rust test/check/fmt、源码sidecar、敏感/历史扫描、diff检查。
- [ ] cargo命令找不到先查交接已知工具链与本机runtime，不能直接宣称机器没有Cargo。确实不可用可用同HEAD CI验证，但记录本机该门禁NOT_RUN，不能将“806pass+1fail”写成全部通过。
- [ ] 仅提交确认过的源码、测试及公开计划。GitHub写入完整文件树必须由Git对象/文件读取生成，禁止手工转抄长App.tsx导致截断。
- [ ] local/remote commit可以不同但最终tree必须核验一致，随后获取真实remote对象对齐；Git更新必须fast-forward，禁止force。

## I. Task R7：最终安装包与真实10人/8文件验收，全部由任务完成

- [ ] 新HEAD推原release分支；监控该HEAD CI与RC，下载最终aggregate artifact，独立核对ZIP/内部SHA/manifest/head/tree/架构。
- [ ] 旧artifact10096271960只是上一候选，不可当本返工后的包。不要先给用户新候选下载链接试错。
- [ ] 用独立临时安装目录，通过正常Mac应用入口运行。若产品要求即时启动/外发确认，正式请求；不能绕过，也不能直接把验收推给用户。
- [ ] 沿用已保存的CodingPlan/GLM配置，不读取Key/pepper文件；如果应用UI确实未配置，再说明具体缺失并让用户在App输入，不在聊天中索取。
- [ ] 新合成企业导入原8份，核对hash；真实跑完整分析，逐项核对10人的身份、事实、来源、拟续签/已签、正文/图像双员工。
- [ ] 实际执行取消→恢复，完成文件不重复调用；确认/修订→本地重评，模型调用数不增加；生成冻结报告→原生保存PDF→渲染全页检查。
- [ ] 正常退出重开后10人、材料、任务、修订及报告恢复；不会自动重发模型请求。按实际归属检查本次App/sidecar清理，不kill用户旧App。
- [ ] 不能取得实际授权就记录BLOCKED具体动作及未验收项；不能把“用户稍后自己点PDF”当任务完成。
- [ ] 每个失败都对应证据和修复，不改样本、不删不利断言、不伪造人工确认。重打包后重新验证相应最终HEAD，不能拼接旧版本PASS。
- [ ] 所有门禁满足才PR2 Ready，绝不merge/tag/formalRelease。最终证据写PR描述/评论可避免文档提交不断改变已经验收的HEAD。

## J. 回报格式（必须逐项填写实际结果）

| ID | 要求 | 状态定义 |
| --- | --- | --- |
| R1 | 完整行不拆、边界/大行/取消测试 | PASS必须有原复现RED→GREEN |
| R2 | v1失效提示、v2复用、无自动调用、历史不变 | PASS必须有临时旧库测试 |
| R3 | DOCX无假页号，段落/图片传到事实与报告 | PASS必须有完整链路证据 |
| R4 | 安全诊断、真实合同及XLSX问题解决 | 离线PASS与真实PASS分列 |
| R5 | 分组、active禁用、实时刷新、报告状态 | 新增组件测试及原生结果分列 |
| R6 | 独立复核、全套、本地/远端tree一致 | 未执行不能写PASS |
| R7 | 新HEAD包、真实10/8、PDF、重启/进程 | 任一核心失败则整体FAIL/BLOCKED |

状态只能用NOT_STARTED、IN_PROGRESS、PASS、FAIL或BLOCKED；“offline complete”不能代替真实验收PASS。
阶段回报提供完成了哪些ID、下一ID及真正阻碍，不再输出“方案分8阶段”代替实施报告。
完成时提供最终HEAD/PR/唯一Mac下载入口/验收证据与非阻断限制。原审查任务复核发现仍未解决时，不能自行宣布结束。

## K. 权限与范围重申

用户本次明确要求你执行此返工，无需再问是否开始。本机目录、Git、网络、GUI仍服从即时权限工具。
只允许本任务合成材料测试；Key和pepper不得进入聊天、日志、SQLite、前端或Git。
不新增产品功能，不重开会话、不再派一个新主执行。你是唯一写入者，审查席只读。

