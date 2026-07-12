这份 `README.md` 文档将作为你整个 HRS (Historical Records Scraper) 项目的“架构蓝图”和“进化史”。它不仅能帮你梳理思绪，未来如果项目开源或有其他开发者加入，他们也能通过这份文档一秒看懂你的代码库。

***

# HRS 史料全自动采集与 AI 校对系统 (v3.2.0)

## MOFA 《日本外交文书》接入规范（开发中）

MOFA 数字合集是按年份、卷册和事项组织的编纂出版物目录，不是 JACAR 式的远程关键词档案检索库。HRS 的 MOFA 流程因此固定为：

```text
卷号总目录 → 1921—1927目标卷册 → 事项PDF目录 → 本地下载/去重 → 全文或OCR检索 → 研究处理
```

### 文献出处

译稿规范格式：

```text
日本外交文書：「文书标题」（第526—529頁）、『日本外交文書』大正十年第二冊（日本外務省編、日本外務省発行、1975年）
```

- 优先标注书籍印刷页码；只有印刷页码无法获得时才使用 `PDF第X—Y頁`。
- “原始文书形成年”和“《日本外交文书》出版年”必须分字段保存。
- 出版年或印刷页码未核实时，`Citation_Text` 保持空值，`citation_status=pending_bibliography`；不得生成伪完整引用。

### 文档身份与存储

- `source=mofa`，`collection=日本外交文書`。
- 已知印刷起页时，编号形如 `MOFA_T10_2_00526`。
- 目录阶段尚不知印刷页时，使用 PDF URL 的稳定哈希编号，形如 `MOFA_T10_2_UXXXXXXXXXXXX`。后续识别页码只补充元数据，不改号、不搬迁 bundle。
- sidecar 采用 `schema_version=2`，通用身份写入 `identity`，MOFA 原生字段写入 `source_metadata`。
- PDF 先写入 `.part` 临时文件，校验 `%PDF-` 文件头后原子替换，避免中断文件被误标为已下载。
- 去重同时参考 SQLite `source + native_id` 和物理 PDF；物理文件存在时可自动修复 sidecar、manifest 与数据库状态。

### 接入进度

- Phase 1：通用史料身份、MOFA 编号/引用、sidecar/manifest v2。
- Phase 2：1921—1927年卷册发现、PDF 目录解析、分类与去重。
- Phase 3：PDF 下载、SQLite 审计、bundle 写入与物理自愈。
- Phase 4：下载页来源切换、MOFA 安全默认扫描、目录结果展示、标题命中/全部正文显式下载、任务监控与中止。
- Phase 5A：真实 PDF 获取验证、文本层诊断、OCR 页识别与全语料规模估算。
- Phase 5B—7：本地全文索引、选择性 OCR、研究流水线和回归工作待后续版本完成。

### Phase 5A 实测结论（2026-07-13）

- MOFA CDN 对“冷启动 PDF 直链”可能返回 `403 Access Denied`。已验证的可行流程是：使用同一 `requests.Session` 先访问该 PDF 所属卷册页，再带 `Referer` 请求 PDF；如首次 PDF 返回 403，强制重新预热后重试一次。
- 正式 `MofaDownloadService` 已真实下载 `showaki111_07.pdf`：6,035,018 字节、27 页，文件头为 `%PDF-`，能正常写入临时 bundle、SQLite、sidecar 与 manifest。
- 跨年份抽查 3 份正文：1921年 50 页/10,744,278 字节，1925年 25 页/5,714,221 字节，1927年 27 页/6,035,018 字节。三份合计 102 页，每页均为单张图像，PyMuPDF 提取文本字符数均为 0，因此全部需要 OCR。
- 官方 1921—1927 目录共 20 个卷册、296 个 PDF，其中正文 240 个：1921年 55、1922年 44、1923年 40、1924年 21、1925年 32、1926年 34、1927年 14。
- 以 3 份样本均值粗略估算，240 个正文 PDF 约为 **8,160 页 / 1.80 GB**。该数字只用于容量和 OCR 任务规划，不是完整下载后的精确统计。

可重复运行的诊断命令：

```bash
python scripts/run_mofa_phase5a_diagnostics.py sample1.pdf sample2.pdf sample3.pdf --live-catalog
```


# HRS 史料全自动采集与 AI 校对系统 (v2.6.7)
`HRS (Historical Records Scraper)` 是一款面向近代史研究场景的桌面端生产力工具，覆盖史料抓取、OCR 校对、翻译与价值甄别全流程。  
`v2.6.7` 在 `v2.6.6`（google-genai + Chat Session）基础上，完成了 **Core Scraper 下载链路的深度重构**，重点落在“多源站点下载战术拆分、稳健性增强、数据完整性保障、可视化监控升级”四个维度：

- **核心下载架构重构**：淘汰旧 Selenium 点击/输入/ACV 打包链路，改为参数化 URL 直达检索页 + 列表页直接抽取元数据；确立三分支下载战术（JACAR/NAJ 直链串流、東洋文庫 IIIF 组装、Hoover 宕机登记）。
- **多源站点适配增强**：東洋文庫新增 `opac.tbopac.com` 跳板页自动解析，统一提取真实阅读器 URL 并衔接 IIIF Manifest 流程；Hoover 分支加入 pending 列表防重复登记。
- **稳健性与防错强化**：`requests.Session` 统一接入 50X 自动重试；翻页改为 `wait.until(EC.staleness_of(...))`；新增无结果识别、严格校验失败证据输出、Windows 文件名末尾空格/点号容错。
- **元数据与去重矩阵完善**：列表页全量 `<dl>` 元数据写入同名 sidecar；本地状态矩阵支持“PDF 存在但 JSON 缺失时仅补 sidecar”“双存在则跳过”。
- **IIIF 页级断点续传**：新增 `.iiif_resume` 页分片缓存与复用机制，支持中断续跑；单页失败写入 `IIIF_Error_Log.txt`，中断时保留断点。
- **监控体验升级**：终端实现类 pip 单行动态进度（含全局进度与 IIIF 卷级降噪）；GUI 新增独立下载监控弹窗，展示任务清单、状态、进度与速度，并在遍历阶段即建立条目（含“已下载”即时标记）。
# HRS 史料全自动采集与 AI 校对系统 (v2.6.6)

`HRS (Historical Records Scraper)` 是一款面向近代史研究场景的桌面端生产力工具，覆盖史料抓取、OCR 校对、翻译与价值甄别全流程。
`v2.6.6` 在 `v2.6.5` 服务层与 Jinja2 模板基础上，完成 **google-genai SDK 接入与有状态 Chat Session 升级**：

- `LlmService` 切换到 `from google import genai` 与 `from google.genai import types`，复用单一 `genai.Client`；OCR 多模态请求改用 `types.Part.from_bytes(...)`；
- Analysis / Translation 改为**多轮 Chat Session**：进入逐页循环前一次性渲染 `system_prompt` 并 `client.chats.create(model=..., config=...)`，循环内仅 `chat.send_message(turn_prompt)`，跨页连贯性完全由 SDK 原生历史承担；
- 物理拆分 Jinja2 模板为 `analysis_system / analysis_turn` 与 `translation_system / translation_turn`，删除 `prev_page_raw` / `prev_translation_context` 拼接逻辑；
- Analysis 通过 `response_mime_type="application/json"` 利用 SDK 原生能力强制 JSON 输出，告别 Markdown 代码块解析的脆弱性。

# HRS 史料全自动采集与 AI 校对系统 (v2.6.5)

`HRS (Historical Records Scraper)` 是一款面向近代史研究场景的桌面端生产力工具，覆盖史料抓取、OCR 校对、翻译与价值甄别全流程。
`v2.6.5` 在 `v2.6.4` 的 Prompt 工程化基础上，完成了工作台结构升级：通过“状态提升（Lift State Up）+ 全局文件树组件 + 事件总线（AppState）”解耦页面与文件库；并引入 `services` 服务层（PDF/缓存/LLM）持续瘦身 `BaseDocumentScreen`。同时修复了主题切换下侧栏与下载控制台的颜色一致性问题，提升交互稳定性与可维护性。

# 📚 HRS 史料全自动采集与 AI 校对系统 (v2.6.3)

**HRS (Historical Records Scraper)** 是一款专为近代史学者、档案研究人员打造的桌面端生产力软件。  
本版本在保留高并发抓取与史料级 OCR 能力的基础上，进一步完成了 **OCR→翻译→甄别** 的流水线重构：翻译与甄别默认以 `OCR_Cache` 的人工可校对文本为底稿（而非重复上传图像），并补齐“手动修改后可显式落盘”的缓存持久化链路，降低 Token 浪费并提升研究可复现性。

# 📚 HRS 史料全自动采集与 AI 校对系统 (v2.6.2)

**HRS (Historical Records Scraper)** 是一款专为近代史学者、档案研究人员打造的桌面端生产力软件。
本软件致力于解决日本亚洲历史资料中心（JACAR）等档案网站“批量下载难”的问题，并深度集成 **Google Gemini** 多模态 API（支持 **Flash** 与 **3.1 Pro** 预览模型切换），实现针对大正/昭和时代日文历史档案的“史料级” OCR 识别与排版还原。

---

## ✨ 核心特性 (Features)

* 🚀 **高并发史料抓取**：采用 Selenium（包工头） + 多线程纯 API 请求（打工人）的分离架构，无视繁琐的网页跳转，直接暴力拉取原始 PDF。
* 👁️ **史料级 AI OCR**：默认 `gemini-3-flash-preview`（低成本），可切换 `gemini-3.1-pro-preview`（高质量）。内置史料级 Prompt 与“安全审查豁免（BLOCK_NONE）”配置，精准识别旧体字、繁体字与草书，保留历史语境，支持残缺字体智能推测与 `【?】` 标记。
* 📄 **全量与单页 OCR**：支持整本 PDF 逐页识别；另提供「仅识别当前页」「重新识别当前页」，在已有 `paged_v1` 缓存上定点更新，节省 Token。
* 📊 **本地费用与用量**：每次 Gemini 调用后追加写入根目录 `api_cost_log.csv`（UTF-8 BOM，便于 Excel）；界面展示本次全量任务累计 Token 与 JPY/CNY 预估。
* 💾 **智能分页与缓存系统**：采用 `paged_v1` 结构的 JSON 本地缓存。一次识别，永久保存。支持按页阅读、单页/全本导出（`.md` / `.docx`）。
* 🎨 **现代化 GUI 体验**：基于 CustomTkinter 构建。包含平滑折叠侧边栏（Ease-out 动画）、全局 Design Token、Nerd Font 图标与 OCR 操作区「图标+文字」分离按钮封装，提供一致的交互体验。
* 结构化分析与双轨存储：当模型返回可解析 JSON 时，基类拦截器将结果写入 `Database_JSON/`（`Document_ID + 页级后缀` 防覆盖），并同步维护 `Analysis_Cache/` 的 `paged_v1` 数据，分别服务数据库归档与 UI 展示。
* Prompt 配置解耦：分析与翻译 Prompt 分别集中在 `config/academic_prompts.py` 与 `config/translation_prompts.py`，通过占位符机制实现 `Translation_Plugins` 与插件库单源同步。
* 翻译动态组装机制：翻译页以 `TRANSLATION_BASE_PROMPT` 为底座，按 `Analysis_Cache` 中的插件标签动态加载插件文本；并在条件命中时自动注入全档 OCR 语境与上一页译文尾部，增强术语连贯与段落衔接。
* 按页 Prompt 能力贯通：`get_academic_prompt(page_index=...)` 与 `_detect_text_from_image(..., page_index=...)` 在基类/子类全链路一致，支持跨页连贯性控制与上下文精细化注入。
* 分析排障体验增强：当 JSON 解析失败时，支持弹窗查看只读“原始响应”，避免受控表单空白导致的排障盲区。
* 爬虫无头模式：`core_scraper.jacar_auto_search(..., headless=...)` 与抓取页开关联动，支持后台稳定抓取。
* 全局文件树与事件总线：`FileTreeSidebar` 作为全局组件统一管理文件选择；`AppState.selected_pdf_path` 在 OCR/翻译/分析页面间共享，页面切换不再重复重建目录树。
* 服务层解耦：`PdfService`、`CacheService`、`LlmService` 分离 PDF 处理、缓存协议与 Gemini 调用/追踪，降低 `base_screen.py` 耦合度并便于后续测试。
* （增量补记）Prompt 模板引擎：已引入 Jinja2 与 `TemplateService`，`OCR/Analysis/Translation` 的 Prompt 现由 `templates/*.jinja` 渲染输出；保留原“Prompt 配置解耦”描述作为历史阶段痕迹。
* （增量补记）OCR 容错与断点恢复：超时页支持自动重试（默认最多 3 次），请求超时会提示“网络无响应”；并在已有 `paged_v1` 缓存时从首个未完成页继续处理，而不是简单整包命中即结束。
* （增量补记）阅读区双向联动：PDF 页码与右侧工作区页码已实现双向绑定（任一侧切页，另一侧同步）。
* （v2.6.6 增量补记）google-genai SDK 全量接入：`LlmService` 使用 `from google import genai` + `from google.genai import types`，统一通过 `self.client = genai.Client(api_key=...)` 调用，并复用一份 `DEFAULT_SAFETY_SETTINGS`；OCR 多模态请求改用 `types.Part.from_bytes(data=..., mime_type="image/jpeg")` 作为列表中的一项。
* （v2.6.6 增量补记）Chat Session 化的 Analysis / Translation：`BaseDocumentScreen` 新增 `use_chat_session`、`chat_response_mime_type`、`chat_temperature` 类属性，并提供 `get_system_prompt()` / `get_turn_prompt()` 双钩子；运行时 `_start_new_chat_session()` 在进入逐页循环前 `client.chats.create(model=..., config=...)`，循环内统一走 `chat.send_message(turn_prompt)`。
* （v2.6.6 增量补记）Jinja 模板物理拆分：`templates/analysis_prompt.jinja` 与 `templates/translation_prompt.jinja` 已删除，按 system / turn 解耦为 `analysis_system.jinja`、`analysis_turn.jinja`、`translation_system.jinja`、`translation_turn.jinja`；Translation turn 模板**彻底删除** `prev_page_raw` 与 `prev_translation_context` 拼接逻辑，跨页记忆完全交给 Chat Session 原生 history。
* （v2.6.6 增量补记）Analysis 强制 JSON：Analysis 子类设 `chat_response_mime_type = "application/json"`，借助 SDK 原生能力约束输出形态，降低 Markdown 包裹解析失败的概率。
* （v2.6.6 增量补记）Translation 文档级聚合：`TranslationScreen` 新增 `_load_analysis_pages` / `_aggregate_active_plugins` / `_build_document_context_summary`，将插件与剧情大纲一次性注入 system；逐页 turn 仅含 `context_info` 与当前页原文，token 占比显著下降。
* （v2.6.6 增量补记）Token 拆算与隐式缓存可视化：`utils/token_logger.py` 对齐新版 SDK `response.usage_metadata` 语义。新 SDK 的 `prompt_token_count` **包含**缓存 Token，逻辑统一为 `non_cached_input = prompt_token_count − cached_content_token_count`、`output_tokens = candidates_token_count`，并新增对 `thoughts_token_count` 的捕获（按输出费率计入成本）；`api_cost_log.csv` 增加「思维Token(Thoughts)」与「缓存命中率(%)」两列，便于在 Excel 一眼判断隐式缓存是否生效；所有字段全部 `None` 容错。

---

## 🗺️ 项目架构与模块关系 (Architecture)

本软件当前采用 **模块化 + 模板方法（Template Method）+ 服务层（Service Layer）+ 状态提升（Lift State Up）** 的分层架构：  
抓取、文档处理、UI 组件与配置彼此解耦；`BaseDocumentScreen` 负责统一工作流；全局文件树与选中文件状态提升到 `ScreenManager + AppState`，`OCR/Translation/Analysis` 仅保留差异化配置与 Prompt。

```text
HRS_Project/
│
├── HRS_app.py                  # 🏁【程序入口】初始化主窗口、左侧导航栏与 ScreenManager
├── core_scraper.py             # ⚙️【爬虫核心】Selenium 翻页 + 多线程 API 下载（支持 headless 开关）
├── api_cost_log.csv            # 📒【本地账单】Gemini 用量与费用日志（gitignore，运行时生成）
│
├── utils/                      # 🧮【工具层】
│   ├── token_logger.py         # Token 拆算（含 cached/thoughts）、按模型计价、写入 13 列 api_cost_log.csv
│   ├── gemini_trace_logger.py  # Gemini 请求/响应/缓存写入事件追踪（jsonl）
│   ├── trace_report.py         # Trace 日志转 Markdown 报告与清理工具
│   └── app_state.py            # 全局状态单例：selected_pdf_path + 订阅/发布事件总线
│
├── screens/                    # 📺【视图层】
│   ├── HRS_manager.py          # 🔀 路由管家：管理全局文件树显隐、页面路由与内容区装配
│   ├── scraper_screen.py       # 🚀 抓取控制台：参数输入、进度显示、触发 core_scraper
│   ├── base_screen.py          # 🧠 文档工作台基类（模板方法）：阅读器三栏、状态机、分页编辑、服务层调度、响应 JSON/纯文本拦截与子类钩子
│   ├── ocr_screen.py           # 👁️ OCR 子类：图像输入链路（requires_image_input=True）、OCR Prompt
│   ├── translation_screen.py   # 🌐 翻译子类：读取 OCR_Cache 文本底稿，执行学术翻译 Prompt
│   ├── analysis_screen.py      # 🔍 受控表单编辑 JSON 字段、enrich_json_data 元数据注入、与基类 JSON 拦截器协作 等
│   └── setting_screen.py       # ⚙️ 系统设置页：API Key 配置入口
│
├── components/                 # 🧱【组件层】
│   ├── HRS_navigation.py       # 🧭 左侧折叠导航栏（含动画、路由按钮）
│   ├── file_tree_sidebar.py    # 🌲 全局史料文件树组件（仅在文档页显示）
│   └── ui/
│       ├── button.py           # 标准化按钮组件
│       └── input.py            # 标准化输入框组件
│
├── config/                     # 🛠️【配置层】
│   ├── settings.py             # 🎨 主题色、版本号、路由常量
│   ├── academic_prompts.py     # 分析用学术 Prompt（含与翻译插件枚举联动占位符）
│   ├── translation_prompts.py  # 翻译核心底座与插件库
│   └── api_key_store.py        # 🔒 API Key 本地存取
│
├── services/                   # 🛎️【服务层】
│   ├── pdf_service.py          # PDF 打开、分页渲染、字节提取
│   ├── cache_service.py        # paged_v1 读写、缓存路径构建、目录清理
│   └── llm_service.py          # Gemini 调用、超时控制、trace/token 记录、JSON 拦截落盘
│   └── template_service.py     # （增量补记）Jinja2 模板渲染服务（Singleton）
│
├── templates/                  # （增量补记）Prompt 模板目录（v2.6.6 起按 system/turn 物理拆分）
│   ├── ocr_prompt.jinja            # OCR 单次调用的系统级 Prompt
│   ├── analysis_system.jinja       # Analysis Chat Session 的系统前缀（人物 + JSON Schema + 跨页继承法则）
│   ├── analysis_turn.jinja         # Analysis 每轮的 <SOURCE_TEXT> 包装
│   ├── translation_system.jinja    # Translation 系统前缀（底座法则 + 聚合插件 + 全书剧情大纲）
│   └── translation_turn.jinja      # Translation 每轮 turn_prompt（仅当前页原文 + 衔接指令）
│
├── PROMPT_FLOW.md              # （增量补记）OCR -> Analysis -> Translation 的 Prompt 调取链路文档
│
├── Historical_Documents/       # 📚 新版统一史料根目录（按来源与原生 ID 组织）
│   ├── jacar/<JACAR_REF>/
│   ├── mofa/<年份>/<卷代码>/<MOFA_ID>/
│   └── research/               # 后续候选史料工作包
├── JACAR_Downloads/            # 📂 旧版 JACAR 数据；迁移期间只读兼容保留
├── OCR_Cache/                  # 💾 OCR 结果缓存（paged_v1，哈希文件名，运行时生成）
├── Database_JSON/              #分析 JSON 单页归档（运行时生成；与 Analysis_Cache 的 paged_v1 形成「数据库轨 / UI 轨」分工）
├── Translation_Cache/          # 💾 翻译结果缓存（paged_v1，运行时生成）
└── Analysis_Cache/             # 💾 分析结果缓存（paged_v1，运行时生成）
```

### 🔍 核心文件关系说明：

1. 抓取层与 UI 解耦
   `scraper_screen.py` 只负责收集参数与线程调度；真正的网页翻页、任务分发与 PDF 下载全部在 `core_scraper.py`。
   `core_scraper.jacar_auto_search(...)` 已支持 `headless` 开关：可无界面运行或可视调试运行。
2. 文档工作台模板化（减少重复代码）
   `base_screen.py` 提供统一能力：PDF 渲染、分页编辑、状态机、缓存读写、Gemini 调用、Token 汇总。
   `ocr_screen.py` / `translation_screen.py` / `analysis_screen.py` 只配置：
   - 页面文案（标题、按钮、状态词）
   - 缓存目录名（`cache_dir_name`）
   - Prompt（`get_academic_prompt`）
   - 导出行为（`export_document`）
3. 服务层解耦（Base Screen 大瘦身）
   - `PdfService`：管理 `open/close/render/get_page_bytes`，避免 UI 直接操作底层 PDF 库。
   - `CacheService`：统一 `paged_v1` 协议与缓存路径构建/清理。
   - `LlmService`：统一 Gemini 请求、120s 超时、trace 事件、token 统计与 JSON 分流逻辑。
   基类聚焦编排与 UI，同类职责更清晰。
4. NLP 流水线分层（避免重复传图）
   - OCR：`requires_image_input=True`，按页渲染图片并调用 Gemini。
   - 翻译/分析：`requires_image_input=False`，先从 `OCR_Cache` 读取文本底稿，再以纯文本调用 Gemini。
     缺少 OCR 底稿时会在 GUI 明确拦截提示，避免无效 API 消耗。
5. 缓存协议统一与可追溯
   三类工作台统一采用 `paged_v1`：`{"format":"paged_v1","pages":[...]}`。
   缓存文件名由 `PDF 路径 + mtime + size` 计算 SHA256，确保同一 PDF 的缓存可稳定定位。
   OCR / 翻译 / 分析分别写入各自目录，互不污染。
6. 编辑持久化与人机协作闭环
   右侧文本编辑区支持“💾 保存修改”显式落盘：将当前页编辑结果回写到对应缓存文件。
   这样可以把人工校对结果沉淀为后续翻译/分析可复用的稳定数据资产。
7. 状态提升与全局文件树
   `FileTreeSidebar` 从 `base_screen.py` 抽离为全局组件；点击文件后通过 `AppState.set_selected_pdf(...)` 广播。
   文档类页面在初始化时订阅 `AppState.subscribe_file_change(...)`，通过 `on_global_file_changed` 自动加载同一份 PDF。
   `HRS_manager.py` 根据路由在文档页显示文件树、在下载/设置页隐藏文件树，减少无效渲染并保持上下文连续。
8. （增量补记）Prompt 模板化（Jinja2）
   当前 Prompt 生成链路已升级为“`Screen` 组装 context -> `TemplateService.render_prompt(...)` -> `templates/*.jinja` 输出最终 Prompt”。
   兼容说明：本 README 中较早的“`TRANSLATION_BASE_PROMPT` 底座常量”描述保留为历史痕迹，现阶段底座正文已迁移到 `templates/translation_prompt.jinja`。
9. （增量补记）Translation 上下文策略已收敛
   新版本采用“远近文武分治”：远端仅注入当前页前后各 2 页的 `Core_Judgment` 摘要，近端仅注入相邻页原文截断片段（上一页尾部 / 下一页开头）。
   兼容说明：旧版“全局知识图谱/全档 OCR 语境全文注入”描述保留为历史阶段说明，不作为当前默认行为。
10. （增量补记）OCR 网络异常与断点续跑
   `LlmService` 的超时路径已改为不等待后台线程自然结束，降低“超时后额外卡死”风险；
   `BaseDocumentScreen` 在检测到部分缓存时会从断点继续处理，并在超时弹窗中提示当前已保存进度。
11. （增量补记）PDF 与工作区页码双向绑定
   当 PDF 区域切页时，工作区页码会同步；当工作区切页时，PDF 区域也同步到对应页，以减少双区域视线跳转成本。
12. （v2.6.6 增量补记）google-genai Chat Session 接入
   `LlmService` 提供两条主通道：
   - `detect_text(...)`：OCR 等单次调用，使用 `self.client.models.generate_content(model=..., contents=[Part.from_bytes(...), prompt], config=...)`；
   - `start_chat_session(...)` + `send_chat_message(chat, turn_prompt, ...)`：Analysis / Translation 的有状态多轮通道，底层为 `self.client.chats.create(model=..., config=GenerateContentConfig(system_instruction=..., response_mime_type=..., temperature=..., safety_settings=...))`。
   `BaseDocumentScreen._start_new_chat_session()` 在 `_extract_text_with_gemini_ocr` 与 `_run_single_page_worker` 进入循环前一次性渲染 system 并新建 Chat；切换 PDF 或点击「强制重新」时会自然覆盖 `self.current_chat`，达成"重新开始即清空上下文"。
13. （v2.6.6 增量补记）Token 拆算与本地账单对齐新版 SDK
   `utils/token_logger.py` 严格按新版 SDK 的 `usage_metadata` 语义执行四步拆算：
   - `cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0`
   - `total_input   = getattr(meta, "prompt_token_count", 0) or 0`
   - `non_cached_input = total_input - cached_tokens`
   - `output_tokens = getattr(meta, "candidates_token_count", 0) or 0`
   并新增 `thoughts_token_count` 捕获（Gemini 3 thinking 模型），按 Google 计费口径与 `candidates` 一同走"输出费率"。`api_cost_log.csv` 由 11 列扩展至 13 列：在「输出Token」之后插入「思维Token(Thoughts)」，在「总Token」之后插入「缓存命中率(%)」，可在 Excel 直接核对隐式缓存的生效情况。返回字典保持 `prompt_non_cached / cached_content_token_count / candidates_token_count / total_token_count / cost_usd|jpy|cny` 等键向后兼容，同时新增 `thoughts_token_count / billable_output_tokens / cache_hit_ratio` 供下游审计或 UI 扩展。
   - **迁移提示**：旧 `api_cost_log.csv`（11 列）与新行（13 列）共存不会影响数值正确性，但 Excel 顶端表头需手动补齐两格；由于该文件已 gitignore，最简单的做法是删除老文件让程序首次运行时重写新表头。

---

## 📈 版本更迭记录 (Changelog)
### v3.2.0 · MOFA integration（开发分支）
* **Phase 1**：新增通用 `DocumentIdentity`、MOFA 稳定编号、标准引用生成器、sidecar/manifest v2；存储层可从 manifest/sidecar 恢复 `source=mofa`，并保持旧 JACAR 数据兼容。
* **Phase 2**：新增 MOFA 1921—1927 卷册目录采集器；官网验证可发现 20 个目标卷册，并解析事项 PDF、扉页/目录、索引与奥付。
* **Phase 3**：新增 MOFA PDF 原子下载、文件头校验、SQLite 状态/文件登记、bundle sidecar/manifest 写入、重复下载跳过与物理自愈。
* **Phase 4**：下载控制台增加 `JACAR / MOFA` 来源切换。MOFA 默认为“仅扫描目录”，必须显式选择才会下载“目录标题命中项”或“范围内全部正文 PDF”；扫描后独立展示卷册、PDF、正文和标题命中数量，下载任务复用 SQLite 监控弹窗。
* **Phase 5A**：修复 MOFA CDN PDF 冷直连 403（同 Session 卷册页预热 + Referer + 一次强制重试）；新增逐页文本层/图像诊断、OCR 待处理页列表、实时目录规模估算 CLI。真实样本验证表明抽查的1921/1925/1927年正文均为纯影像 PDF。
* **Phase 5B-0**：新增统一根目录 `Historical_Documents`。JACAR 新写入路径为 `jacar/<Ref>/`，MOFA 为 `mofa/<年份>/<卷代码>/<native_id>/`，物理目录不再由检索关键词决定。SQLite 继续以 `source:native_id` 防重，并新增 `document_keywords` 多对多表保存一份史料的全部关键词命中关系。
* **Phase 5B-1**：新增独立导航页“MOFA史料库”。官网1921—1927目录同步后缓存至 SQLite `mofa_catalog_items`，离线仍可按年份、卷册、事项类型和处理状态浏览。页面实时检测每项的 PDF、`mineru/raw`、`mineru/imported` 与 `search/search_text.paged.json`，显示“未下载 → 待OCR → 待导入 → 待生成检索文本 → 可检索”的处理阶段；现有 MOFA 扫描工作流会复用并更新同一目录缓存。
* **Phase 5B-2**：MOFA 下载入口统一迁入“MOFA史料库”，支持表格多选、下载当前筛选范围内缺失 PDF、顺序下载、队列暂停/继续、停止当前任务和基于 SQLite/物理文件状态的再次续跑。已完成文件自动跳过，停止时只清理当前 `.part`，不会删除成功 PDF。原“史料下载”页面恢复为 JACAR 专用，避免两套 MOFA 下载入口产生状态分叉；MOFA 后端工作流继续保留用于兼容和测试。
* **Phase 5B-2.1**：MOFA史料库增加本地史料快捷操作。双击条目用系统默认 PDF 阅读器打开；详情区常驻“打开史料 / 在文件夹中显示”按钮；右键菜单提供打开史料、Finder/资源管理器定位、打开 bundle 文件夹、访问 MOFA 官网以及复制本地路径、MOFA ID、官网链接。右键会先选中目标行，缺失或被移动的文件会禁用本地操作并提示刷新状态。

#### Historical_Documents 迁移规则

* 升级不会自动移动或删除 `JACAR_Downloads`，旧 PDF 与缓存关系继续可读。
* 迁移工具默认只生成计划：`python3 scripts/migrate_to_historical_documents.py`。
* 人工检查无冲突后，显式执行：`python3 scripts/migrate_to_historical_documents.py --execute`。
* 执行模式只复制缺失文件并更新 SQLite `files.path`；旧目录仍保留作为回滚副本。目标文件校验不一致或同一 Ref 存在多个正式旧目录时，整份 bundle 会跳过并报告 `CONFLICT`。
* 同一 Ref 的历史 PDF 若 SHA-256 完全一致，会合并到同一个目标 bundle；旧副本仍保留，不会自动删除。
* `_scratch/duplicates` 是旧 bundle 迁移产生的冲突隔离区，不作为正式史料副本迁入。

### v2.6.7
* **版本**：`config/settings.py` 中 `APP_VERSION = "V2.6.7"`，与窗口标题、导航栏版本显示保持同源常量。
* **重构（下载主链路）**：`core_scraper.py` 彻底移除旧版 Selenium 点击/ACV/ZIP 依赖，改为 `urlencode` 参数直达检索 + 列表页 XPath/CSS 直刮元数据。
* **新增（三分支战术）**：
  * `JACAR / NAJ`：解析直链并分块串流下载。
  * `東洋文庫 IIIF`：解析 manifest，逐页拉图并用 `fitz` 组装 PDF；支持 OPAC 跳板页真实阅读器 URL 自动解析。
  * `Hoover`：不可用站点自动跳过并登记到 `Hoover_Pending_Tasks.txt`，新增本地重复登记防护。
* **强化（网络稳健性）**：`requests.Session + Retry` 统一处理 50X 自动重试；翻页切换改为 `EC.staleness_of`，降低 AJAX 伪刷新导致的元素陈旧错误。
* **强化（容错与证据）**：新增“无结果页面”识别与友好退出；严格校验模式下，缺失关键字段行写入 `failed_rows_*.jsonl` 证据文件；修复 Windows 非法尾字符命名问题。
* **新增（数据完整性）**：列表页全量 `<dl>` 元数据入库 sidecar；三分支均输出同名 sidecar；本地去重矩阵支持“仅补 sidecar”“完整存在即跳过”。
* **新增（IIIF 页级续传）**：引入 `.iiif_resume` 目录缓存单页分片 PDF；中断后优先复用已完成页，仅补缺页；失败页记录到 `IIIF_Error_Log.txt`。
* **新增（监控体验）**：
  * 终端：单行动态进度、全局汇总、IIIF 卷级降噪显示；
  * GUI：独立下载监控弹窗，遍历阶段预建任务清单，并实时更新“待下载/正在下载/已下载/已中止/失败”、进度条与速度。

### v2.6.6
* **版本**：`config/settings.py` 中 `APP_VERSION = "V2.6.6"`，与窗口标题、导航栏版本显示保持同源常量。
* **重构（基建）**：`services/llm_service.py` 切换到 `from google import genai` / `from google.genai import types`，统一持有 `self.client = genai.Client(api_key=...)`；`update_api_key` 改为按需重建客户端；新增 `DEFAULT_SAFETY_SETTINGS` 复用常量；`_call_gemini_with_timeout` 改名为通用的 `_run_with_timeout`，OCR 与 Chat 共用一套立即放弃后台线程的超时实现。
* **重构（OCR 多模态）**：`detect_text` 在图片输入路径下改用 `types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")` + 文本 prompt 的列表 contents；控制参数（safety、response_modalities 等）统一通过 `types.GenerateContentConfig` 传递。
* **新增（Chat Session 通道）**：`LlmService.start_chat_session(model_name, system_instruction, response_mime_type="text/plain", temperature=0.3)` 以严格关键字参数 `model=` / `config=` 调用 `self.client.chats.create(...)`；`LlmService.send_chat_message(chat, ...)` 负责单轮 `chat.send_message(turn_prompt)` 调用、token 统计、trace 事件与 JSON 拦截/`Database_JSON` 落盘。
* **重构（视图层钩子）**：`BaseDocumentScreen` 引入 `use_chat_session` / `chat_response_mime_type` / `chat_temperature` 类属性，以及 `get_system_prompt()` / `get_turn_prompt()` / `_start_new_chat_session()` 钩子；`_detect_text_from_image` 在 `use_chat_session=True` 时改走 `send_chat_message`，否则保留 OCR 单次调用路径；`open_pdf` 切换 PDF 时同步清空 `self.current_chat`。
* **重构（模板物理拆分）**：删除 `templates/analysis_prompt.jinja` 与 `templates/translation_prompt.jinja`，新增 `analysis_system.jinja` / `analysis_turn.jinja` / `translation_system.jinja` / `translation_turn.jinja` 四个模板；`config/academic_prompts.py` 与 `config/translation_prompts.py` 新增对应 `render_analysis_system / render_analysis_turn / render_translation_system / render_translation_turn`。
* **删除（翻译上下文清理）**：按要求**彻底删除** Translation 模板中 `prev_page_raw` 与 `prev_translation_context` 的拼接逻辑；跨页连贯性、人物译名一致性、术语承接等完全依赖 Chat Session 原生 history。
* **强化（Analysis 强 JSON）**：`AnalysisScreen` 设 `chat_response_mime_type = "application/json"`，通过 SDK 原生能力将输出格式约束在 JSON 上；同时把跨页元数据继承法则直接写入 system_prompt，模型在每轮可直接看到自己上一轮的 JSON 输出，自然完成继承。
* **强化（Translation 文档级聚合）**：`TranslationScreen` 新增 `_load_analysis_pages`、`_aggregate_active_plugins`、`_build_document_context_summary`、`_build_context_info_for_page`；启用插件按文档级聚合并以原序列输出，剧情大纲改为汇总每页 `Core_Judgment`，整体仅在 system 中注入一次。
* **重构（Token 拆算）**：`utils/token_logger.py` 对齐新版 SDK 的 `response.usage_metadata` 语义。严格执行四步拆算：`cached_tokens = getattr(meta,'cached_content_token_count',0) or 0` → `total_input = getattr(meta,'prompt_token_count',0) or 0` → `non_cached_input = total_input - cached_tokens` → `output_tokens = getattr(meta,'candidates_token_count',0) or 0`，全部带 `None` 容错。新增 `思维Token(Thoughts)` 与 `缓存命中率(%)` 两列写入 `api_cost_log.csv`，并把 `thoughts_token_count` 按输出费率纳入成本计算；返回字典向后兼容（`prompt_non_cached / candidates_token_count` 等键不变），同时新增 `thoughts_token_count / billable_output_tokens / cache_hit_ratio` 供下游审计扩展。
* **迁移提示**：`api_cost_log.csv` 列数由 11 增至 13（新增思维 Token 与缓存命中率）。`.csv` 已 gitignore，建议手动删除旧文件让程序首次运行时重写新表头；旧表头与新行混存不影响数值正确性，但 Excel 中需要手动补齐两个表头单元格。
* **测试**：`test_prompt_plugins.py` 升级为针对 `system + turn` 四张模板的离线渲染检查，并校验"已废弃片段"（`prev_page_raw`、上一页译文接续参考等）不会再出现在渲染产物里。

### v2.6.5
* **版本**：`config/settings.py` 中 `APP_VERSION = "V2.6.5"`，与窗口标题及导航栏版本显示保持同源常量。
* **重构（核心）**：引入 `utils/app_state.py` 全局状态单例与事件总线；新增 `selected_pdf_path`、`subscribe_file_change`、`set_selected_pdf`，实现文档页面间共享选中文件上下文。
* **重构（核心）**：新增 `components/file_tree_sidebar.py`，将文件树遍历/渲染/折叠状态/选中高亮从 `base_screen.py` 完整抽离为全局组件，避免页面切换重复构建目录树导致卡顿。
* **重构（路由层）**：`screens/HRS_manager.py` 升级为“全局文件树 + 内容区”容器；文档页显示文件树，下载/设置页隐藏；并支持侧栏宽度拖拽与稳定显隐。
* **重构（基类瘦身）**：`screens/base_screen.py` 移除左侧文件树相关逻辑，`PanedWindow` 由 4 栏改为 3 栏（阅读器/操作区/校对区）；初始化时订阅全局文件选择事件并通过 `on_global_file_changed` 自动打开 PDF。
* **优化（主题一致性）**：修复 `FileTreeSidebar` 与 `ScraperScreen` 在 Light 模式下的颜色不一致问题（标题可见性、浅色边框异常、背景不统一）；主题切换时同步刷新 `ScreenManager` 背景。
* **增量补记（Prompt 模板化）**：新增 `services/template_service.py` 与 `templates/ocr_prompt.jinja`、`templates/analysis_prompt.jinja`、`templates/translation_prompt.jinja`，将 Prompt 组装升级为 Jinja2 模板渲染。
* **增量补记（Analysis 约束增强）**：当上一页存在有效 `Date_Written` 时，Analysis 模板注入“跨页一致性强制约束”；同时保留原有“系统附加连贯性指令”。
* **增量补记（Translation 上下文收敛）**：由“全局图谱/全量上下文”调整为“远端摘要（前后各2页）+ 近端原文（前1后1页截断）+ 上一页译文尾部”。
* **增量补记（OCR 健壮性）**：新增页级超时自动重试、网络无响应提示、断点续跑；修复超时路径下的潜在长等待问题。
* **增量补记（交互联动）**：新增 PDF 区域与工作区页码双向绑定显示逻辑。

### v2.6.4
* **版本**：`config/settings.py` 中 `APP_VERSION = "V2.6.4"`，与窗口标题及导航栏版本显示保持同源常量。
* **新增 / 强化**：基类 JSON 拦截器支持结构化结果分流写入 `Database_JSON/`，并通过页级后缀策略降低同文档多页覆盖风险。
* **新增**：`config/academic_prompts.py` 与 `config/translation_prompts.py` 配置解耦；`__TRANSLATION_PLUGIN_ENUM__` 与 `TRANSLATION_PLUGINS` 动态同步。
* **新增**：`get_academic_prompt(self, page_index=None)` 与 `_detect_text_from_image(..., page_index=None)` 签名贯通，支持按页 Prompt 组装。
* **新增**：分析滚动记忆——非首页可参考 ocr_pages 中上一页 JSON 注入连贯性说明（analysis_screen.py）。
* **新增**：翻译动态插件与多源上下文——按 Analysis_Cache 中本页的 Translation_Plugins 装载 TRANSLATION_PLUGINS 全文；可选注入 全档 OCR（OCR_Cache）与 上一页译文尾部；界面 plugin_status_label 展示当前组装状态（translation_screen.py）。
* **新增**：AnalysisScreen.enrich_json_data — 从 PDF 文件名等解析元数据并写入 JSON 载荷（与拦截器写库前钩子配合）。
* **优化**：分析 JSON 解析失败时 弹窗展示原始响应，改善排障体验。
* **优化**：侧栏 史料分析 入口置于 史料翻译 之上（HRS_navigation.py）。

### v2.6.3

* **版本**：`config/settings.py` 中 **`APP_VERSION = "V2.6.3"`**，与主窗口标题与导航栏版本标签保持同源常量一致。
* **重构**：引入文档工作台模板基类 **`BaseDocumentScreen`**（`screens/base_screen.py`），统一承载 PDF 阅读渲染、目录树、任务状态机、缓存协议、Gemini 调用与 Token 汇总；`ocr_screen.py`/`translation_screen.py`/`analysis_screen.py` 改为继承式配置，显著减少重复代码。

  ​	INFO：为增加翻译页面、校对页面，需要实现OCR 的页面组件复用，所以做了`base_screen.py`来复用组件。其他页面继承。这极大的缩小了功能代码文件的代码数量和结构。以`screens/ocr_screen.py`为例，这是重要的OCR提取功能页面，曾经这里塞满了UI、缓存、线程、状态机与 Gemini 调用逻辑相关的代码。经过职责收敛后，前文件只保留 OCR 的“差异化配置”，如：

  - `requires_image_input = True`（OCR 仍是图像输入链路）

  - `cache_dir_name = "OCR_Cache"`

  - 按钮文案与状态词（`primary_action_label`、`progress_verb` 等）

  - 空页标记与默认提示文案

    重构前的代码数量大概是1200+行，重构之后代码数量为38行。
* **新增**：正式接入两条新工作台路由：**史料翻译**（`translation`）与 **史料分析**（`analysis`）；`HRS_manager.py` 与 `HRS_navigation.py` 已完成页面注册与导航入口扩展。
* **修复**：实现 **NLP 流水线模式**：  

  * OCR 页面（`requires_image_input=True`）继续走“图像→Gemini”多模态识别；  
  * 翻译/分析页面（`requires_image_input=False`）改为从 **`OCR_Cache`** 读取 `paged_v1` 文本底稿后再调用 Gemini，避免重复传图导致的 Token 浪费。
* **新增**：`_get_ocr_text_for_page` 与全书 OCR 预检机制。若底稿缺失、页码越界、内容为空或命中“未识别到文本”占位，将在 GUI 明确提示：先在「史料校对」完成 OCR 提取与校对。
* **新增**：右侧编辑区加入 **「💾 保存修改」** 显式落盘动作（独立底部操作区，避免窄窗口被工具栏遮挡）。仅在用户点击时写盘，按 `{"format":"paged_v1","pages":[...]}` 覆写当前工作台缓存。
* **优化**：翻译与分析 Prompt 文案去图像依赖，改为明确处理 OCR 文本底稿，并保留 `■` / `【?】` 等史料不确定性标记语义。
* **优化**：分析工作台支持“整份档案优先”交互，隐藏单页分析入口按钮（仍保留结果分页阅读与跳转）。
* **修复**：兼容 Python 3.9 注解解析差异（`components/ui/button.py`、`components/ui/input.py` 增加 `from __future__ import annotations`），避免 `str | tuple` 运行时报错。
* **修复**：修正 `core_scraper.py` 中下载目录创建逻辑的缩进错误，消除潜在 `IndentationError` 风险。

### v2.6.2

* **版本**：`config/settings.py` 中 **`APP_VERSION = "V2.6.2"`** 与 **`HRS_app.py` 窗口标题**、**`components/HRS_navigation.py` 导航栏版本标签**（均引用同一常量）保持一致。
* **新增**：**单页精准 OCR**——「仅识别当前页」「重新识别当前页」：基于当前 PDF 页调用 Gemini，读/写 `paged_v1` 缓存时按总页数补齐列表，避免索引越界。
* **重构**：OCR 操作区主按钮由 **`_build_icon_button`** 统一生成（图标与文字可分别调字号；`RUNNING` 时同步禁用并弱化标签颜色）。
* **修改**：**停用 Vision 命名**——环境变量与本地配置统一为 **`GOOGLE_GEMINI_API_KEY`** / **`google_gemini_api_key`**（`api_key_store.py`）；代码中不再读取 **`GOOGLE_VISION_API_KEY`**。若旧版 `.secrets/api_config.json` 仅存 **`google_vision_api_key`**，首次加载仍会读出以完成迁移，**保存新密钥后**将只写入 **`google_gemini_api_key`**。

### v2.6.1

* **新增**：本地 Token/费用监控：OCR 调用 Gemini 后自动写入 `api_cost_log.csv`，记录时间、文件名、模型、输入/缓存/输出/总 Token 及 USD/JPY/CNY 预估费用（追加写入，不覆盖历史）。
* **新增**：OCR 模型一键切换：操作区模型下拉，支持 `gemini-3-flash-preview`（默认）与 `gemini-3.1-pro-preview`。
* **新增**：按模型计费逻辑：`utils/token_logger.py` 支持 Flash 固定单价与 Pro 阶梯单价（≤200k / >200k）。
* **新增**：任务内实时费用展示：OCR 界面显示当前模型与本次任务累计 Token、JPY、CNY，新任务开始时清零。
* **修改**：完成 Gemini 命名迁移：界面文案与错误提示统一为 Gemini，推荐使用环境变量 **`GOOGLE_GEMINI_API_KEY`**。（对旧 Vision 环境变量与 JSON 键的彻底移除见 **v2.6.2**。）
* **新增**：导航与页面多处 Emoji 替换为 **Symbols Nerd Font** 图标，统一字体与间距。

### v2.4 - 性能与体验的飞跃
* **重构**：引入全局设计 Token（`config/settings.py`），彻底消灭 UI 代码中的硬编码颜色，实现风格统一。
* **重构**：对 `ocr_screen.py` 引入 **异步分批渲染（Batch Rendering）** 技术。解决左侧文件列表在面临海量 PDF（1000+）时引发的主线程卡死问题。
* **新增**：完善 OCR 任务控制状态机。在任务 `RUNNING` 时严格禁用侧边栏与其他无关文件按钮，杜绝多线程竞态崩溃。
* **新增**：**缓存可见化**。左侧列表动态标记 `🟢 [已缓存]`，并拆分“删除全部缓存”、“删除当前缓存”与“强制重新识别”按钮，极大提升用户容错率。
* **优化**：加入可平滑伸缩的左侧导航栏（`HRS_navigation.py`），附带 Ease-out 动画引擎。

### v2.3 - 史料 AI 引擎接入
* **新增**：彻底废弃传统云端 OCR，全面接入 Google **Gemini** 官方客户端（当前代码路径为 `google.genai` / `genai.Client`），默认可按版本配置使用 `gemini-3.1-pro-preview` 等模型。
* **新增**：史料专用 Prompt 注入，并通过代码层面强行下调 `HarmBlockThreshold`，解决“支那”等历史名词触发 API 报错的问题。
* **新增**：引入 `paged_v1` 本地缓存协议，使用 PDF 路径 + 文件大小 + 修改时间计算 SHA256 哈希值，实现文件的唯一绑定与零延迟读取。
* **新增**：集成 PyMuPDF (`fitz`) 与 `Tkinter.Canvas`，实现原生内置的高清 PDF 阅读器，支持鼠标拖拽与滚轮缩放。

### v2.1/2.2 - 架构解耦与现代化 UI
* **重构**：将原本臃肿的几千行单文件代码，拆分为标准的 MVC 目录结构。
* **视觉**：废弃老旧的内置 Tkinter，全面拥抱 CustomTkinter，确立深色/浅色双轨模式与圆角扁平化设计语言。
* **新增**：增加 API Key 本地安全管理模块（`api_key_store.py`），防止密钥硬编码泄露。

---

## 🔒 隐私与安全说明
* 本软件所有的网络请求仅指向目标档案网站（JACAR）与 Google Gemini 官方 API。
* 通过环境变量 **`GOOGLE_GEMINI_API_KEY`** 或设置页本地保存密钥；写入 **`.secrets/api_config.json`** 时字段名为 **`google_gemini_api_key`**。若你仍持有仅含旧键 `google_vision_api_key` 的配置文件，程序会尝试读取该键以便迁移，保存新密钥后将只保留 Gemini 键名。
* **注意**：`.secrets/` 与 `api_cost_log.csv` 等已加入 `.gitignore`，请勿将个人密钥或本地账单提交至公共仓库。

---
*Developed by Merin | HRS Project 2026*
