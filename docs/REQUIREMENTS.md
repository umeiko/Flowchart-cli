# Flowchart AI Agent 开发需求文档

版本：v0.1（初稿）
日期：2026-08-12

## 1. 项目背景与目标

构建一个 Agent 工具：**输入自然语言文档（流程描述、需求说明等），自动输出正确的 Mermaid 图表代码及渲染后的图片**。

核心难点在于 LLM 一次性生成的图表常常存在语法错误、结构错误或语义偏差，因此本工具的核心是一个 **"生成 → 渲染校验 → 多模态视觉验证 → 反馈修复" 的循环（loop）**，通过多轮迭代收敛到正确结果。

### 目标（Goals）

- 支持中文/英文自然语言文档输入，生成 Mermaid flowchart（后续可扩展 sequence、class 等图型）。
- 生成结果必须经过 mermaid-cli 真实渲染验证，保证语法与结构合法。
- 引入多模态模型对渲染图进行视觉校验，判断图表是否忠实表达原文档语义。
- 整个流程自动化循环，带最大轮次限制与明确的失败退出。
- 文本模型与多模态模型使用相互独立的 API 配置（不同 provider / base_url / key）。
- 除生成图外，支持对已有文档/图片的**检视（检查图）**：按两级分类路由分发到对应检查 Agent，产出检查报告。

### 非目标（Non-Goals，本阶段）

- 不做 Web UI / 服务化部署（后续阶段再考虑）。
- 不做图表的精细布局调优（节点位置、样式美化仅做基础支持）。
- 不支持手绘图/图片逆向生成文档（仅文档 → 图）。

## 2. 用户场景

1. 用户提供一份流程描述文档（如 `test_datas/gen/1.txt` 的登录流程文档），工具输出：
   - `*.mmd` Mermaid 源码文件
   - `*.png`（或 svg）渲染图
2. 用户直接粘贴一段自然语言描述，工具交互式生成图表。
3. 用户对结果不满意时，提供自然语言反馈，Agent 在上一轮结果基础上修正。
4. 用户要求检视已有文档/图片（如"检查这份文档里的流程图和操作步骤是否一致"），
   工具经两级路由分发到对应检查 Agent，输出 markdown 检查报告。

## 3. 系统架构

```
                 ┌────────────────────────────────────────────┐
                 │              Agent 主循环                   │
                 │                                            │
  文档输入 ────► │  1. 生成器 (文本LLM)                        │
                 │        │ Mermaid 代码                      │
                 │        ▼                                   │
                 │  2. 渲染器 (mermaid-cli)                   │
                 │        │ 失败 ──► 错误信息作为反馈 ─┐       │
                 │        ▼ 成功                        │       │
                 │  3. 验证器 (多模态LLM, 看图比对文档)   │       │
                 │        │ 不通过 ─► 批评意见作为反馈 ──┤       │
                 │        ▼ 通过                        │       │
                 │  4. 输出 mmd + 图片                  ◄┘       │
                 │        (超过最大轮次则报告失败)               │
                 └────────────────────────────────────────────┘
```

### 模块划分

| 模块 | 职责 |
|---|---|
| `config.py` | dotenv 加载配置；文本/多模态两套独立的模型配置 |
| `llm/client.py` | OpenAI 兼容协议客户端封装（文本对话 + 带图对话 + function calling） |
| `mermaid/extract.py` | 从 LLM 输出中提取 Mermaid 代码块 |
| `mermaid/render.py` | 调用 mmdc 渲染，返回成功/失败与错误信息 |
| `prompts/` | Prompt 模板包：按子 Agent 分文件（generate/verify/style/restyle/ocr/route；check/ 仅兼容旧执行器） |
| `router.py` | 一级分类器：用户输入 → generate / check / chat |
| `check/` | 旧检查执行器及解析兼容；主检查路径不依赖这里的固定检查项 |
| `agent.py` | 子循环（FlowchartAgent）：生成→渲染→验证→修复；支持从已有代码修订 |
| `session.py` | 对话会话状态：当前图、累积需求、版本化产物目录 |
| `skills/` | 最小 Skill 抽象（name/description/inputSchema 与 MCP 同构）及内置 Skill |
| `main_agent.py` | 主 Agent：function calling 对话循环，调度 Skill |
| `chat_cli.py` | 交互式 REPL |
| `cli.py` | 命令行入口（`run` 批处理 / `chat` 交互两个子命令） |

### 双层 Agent 结构

```
chat REPL
   │
   ▼
主 Agent（文本 LLM + function calling）          ← 理解用户意图
   │  调用 Skill：read_document / create_diagram /
   │              modify_diagram / get_current_diagram
   ▼
DiagramSession ──► FlowchartAgent 子循环          ← 保证图的正确性
                     生成 → mmdc 渲染 → 视觉验证 → 修复
```

Skill 的 `parameters` 采用 JSON Schema，与 MCP 工具的 `inputSchema` 同构，
为后续把能力暴露为 MCP server（或接入外部 MCP 工具）预留最小抽象。

### 意图路由与 Skill 驱动检查

每条用户输入先经一级分类（`router.py`，文本模型 JSON 输出）：generate（生成/修改图）
走主 Agent function calling 流程；check（检视已有文档/图片）把当前 Session 中
`kind: check` Skill 的完整执行协议交给文件子 Agent；chat 或分类不可信时回退主 Agent
流程。检查标准、检查项选择、适用性、判定与报告格式均由 Skill 定义，Core 不写死。

```
用户输入（检查需求 + 图片/路径）
   │
   ▼
1. 主 Agent 加载当前 Check Skill，并明确本案例图片、可选文档与报告目标
   │
   ▼
2. 文件子 Agent 按 Skill 选择检查项；只有需要时才读取相关文档
   │
   ▼
3. 子 Agent 调用通用 image_reasoning(prompt, image_paths)；Core 只转发视觉模型
   推理/正文增量，不解释领域规则
   │
   ▼
4. 子 Agent 用 write_file 写 CSV → output/generate/check_results/<case-id>/report.csv
```

Web/TUI 把 `image_reasoning` 显示为普通子 Agent 工具调用。Web 点开调用详情时，上方
展示完整请求，下方实时追加视觉模型推理和正文，完成后展示最终返回。批量检查先由短
规划子 Agent 按 Skill 的 `## batch` 协议生成 `batch_plan.json` 并退出，再逐案例启动
独立子 Agent，避免长文档和视觉输出跨案例累计上下文。

## 4. 功能需求

### FR-1 文档生成 Mermaid
- 输入：自然语言文档文本（文件路径或字符串）；可选参考图片（手绘草图、现有流程图截图，`TEXT_MODEL_VISION=true` 时随首轮生成消息发给主模型）。
- 输出：Mermaid 代码（自动提取 ```mermaid 代码块，容忍模型输出的多余解释文字）。
- 图型默认 `flowchart TD`；文档明确为时序/类图等时可切换。

### FR-2 渲染校验 loop（语法/结构层）
- 渲染前先经 `mermaid.parse` 快速语法预检（Node + jsdom，约 1 秒，不启动 Chromium）；预检失败直接反馈修复，省去完整渲染。
- 使用 `mmdc`（mermaid-cli）渲染，作为语法与结构的权威校验。
- 渲染失败时，将 stderr 错误信息 + 当前代码反馈给文本模型修复，进入下一轮。
- 渲染产物：`output/round_<n>.mmd`、`round_<n>.png` 与 `round_<n>.svg`（每轮渲染成功即顺带出 SVG，失败不影响主流程），保留每轮中间结果便于调试；最终产物同时输出 SVG（`current.svg` / `final.svg`）。
- 生成/修复 Prompt 内置 Mermaid 语法硬规则（特殊字符必须双引号包裹、节点 id 只用英文/数字/下划线等），从源头减少语法错误。
- 画布背景默认白色（`RENDER_BACKGROUND` 配置），禁止透明背景——透明背景会让视觉验证对"用户描述的背景色"永远判 FAIL 形成死循环；用户在需求中指定背景色时经 `create_diagram` 的 `background` 参数逐图设置。
- 视觉验证 Prompt 含样式宽容规则：文档未明确指定样式时，配色/布局/字体不得作为 FAIL 理由。

### FR-7 风格插件系统（文件发现式）
- 风格即文件：`styles/` 目录下每个带 frontmatter 的 `.md` 文件是一个风格插件，
  定义 `name` / `description` / `background`（画布色）/ `init`（Mermaid 主题指令），
  正文为给生成模型的补充风格说明。内置 `default` 与 `dark`。
- 主 Agent 自主发现与选用：`list_styles` 列出模板，`set_style` 切换会话风格，
  `create_diagram` 支持 `style` 参数；无匹配模板时按默认风格并如实告知。
- `default.md` 始终生效：未显式选择风格时自动注入 default 插件，
  用户编辑 `styles/default.md` 即可定制全局默认风格。
- 用户新增风格只需往目录丢一个 `.md` 文件，无需改代码、无需重启（每次调用重新扫描）；
  无 frontmatter 的文件（如 styles/README.md）自动忽略。
- 风格生成子 Agent（`create_style` 工具，`style_agent.py`）：用户口述风格需求时，
  文本模型起草插件文件 → frontmatter 结构校验（复用风格 parser）→ init 指令格式检查
  （mermaid 对畸形 init 静默忽略，渲染抓不出，须主动检查包裹格式与括号配对）→
  示例图试渲染，最多 3 轮修复；通过后落盘 styles/ 并自动切换为当前风格。
- 风格转换子 Agent（`restyle_diagram` 工具，`restyle_agent.py`）：只调整当前图的
  样式层（init/classDef/class/style/linkStyle/:::标记），风格来源为现有模板
  （style_name）或自由风格文本（style_document，文档路径先 read_document）。
  内容零改动由机械骨架校验保证：剥掉新旧代码的全部样式语句后结构骨架必须逐行
  一致，否则打回重生成（最多 3 轮），另过 mmdc 渲染校验。成功后切换会话风格并
  同步 current.* 产物。
- run 模式支持 `--style <名称>`；风格指令注入代码开头（已有 init 指令时不重复注入），
  最终 .mmd 产物自带风格，导入 draw.io 等工具渲染效果一致。

### FR-3 多模态视觉验证 loop（语义层）
- 渲染成功后，把渲染图交给多模态模型检视，输出结构化结论：`PASS` 或 `FAIL: <具体问题列表>`。
- 检视分两个阶段/强度（`VERIFY_MODE` 配置，chat 中可用 `set_verification` 工具实时切换）：
  - **基础图形检视（layout，常驻底线）**：文字是否截断/溢出/重叠、节点框是否遮挡、
    连线是否混乱缠绕、分支标签是否存在可读；不逐字核对文字内容——
    视觉模型识字能力弱（如 qwen3-30B 级别）时避免读错字导致的验证死循环；
  - **完整检视（full，默认）**：在 layout 之上叠加内容与逻辑核对——节点完整性、
    连接与方向、分支逻辑、文字与文档语义一致性（prompt 携带原始文档）。
- PNG 渲染默认 2 倍缩放（`RENDER_SCALE`，mmdc `-s`）；视口宽度默认 auto
  （`RENDER_WIDTH`）：先用默认视口渲一份 SVG 探测图的自然宽度，再按
  min(自然宽度, 4096) 渲染 PNG——mermaid 会把图整体压缩进视口宽度（mmdc 默认
  800），宽流程图被压扁、甘特图等自适应视口图型又会被大视口拉宽，auto 让两类
  都按自然比例渲染；探测出的 SVG 直接留作当轮产物。SVG 为矢量图不受影响。
- FAIL 时将问题列表反馈给文本模型修复，回到 FR-2。

### FR-4 循环控制
- 最大迭代轮次可配置（默认 5 轮）。
- 达到上限仍未通过：输出最后一轮的 mmd + 图片 + 验证意见，以非零退出码报告失败。
- 支持用户交互反馈模式：命令行追加 `--feedback "文字"` 在现有结果上继续修。

### FR-5 模型配置（dotenv）
- `.env` 管理，文本模型与多模态模型完全独立配置：

```
TEXT_MODEL_NAME / TEXT_MODEL_API_KEY / TEXT_MODEL_BASE_URL
VISION_MODEL_NAME / VISION_MODEL_API_KEY / VISION_MODEL_BASE_URL
```

- 均采用 OpenAI 兼容协议（`/v1/chat/completions`），便于接入 OpenAI / DeepSeek / 通义 / 本地 vLLM 等任意兼容服务。
- `.env` 不入库，提供 `.env.example` 模板。

### FR-6 对话式主 Agent（chat 模式）
- 交互式 REPL：用户口述需求或给出文档路径，主 Agent 通过 function calling 调度 Skill 完成。
- 路径有误时主 Agent 能自我纠正：`read_document` 报错自带相似文件候选，另有 `find_files` 模糊查找 Skill，要求自动重试 2~3 次后才向用户求助。
- 文件拖入：用户可在 TUI 拖入任意文件（文档/图片等），显示为彩色 `[文件:文件名]` 芯片（Backspace 整块删除），提交时还原为完整路径随消息发出；其中图片文件在 `TEXT_MODEL_VISION=true` 时额外作为图片发给主模型。`read_image` Skill 支持模型主动查看指定路径图片；`create_diagram` 接受 `image_path` 参考图；修改时把当前渲染图一并发给主模型。无视觉能力时 `read_image` 不下发，图片输入给出明确提示。
- 主模型无视觉能力（`TEXT_MODEL_VISION=false`）时注册 `ocr_image` 工具：
  用多模态验证模型（VISION_MODEL）从素材图片逐字提取文字，是图表时附带
  连接关系描述；用户贴图的路径随消息进入对话，由 Agent 自行调用 OCR。
- 工作文档中间产物（`working_doc.md`，会话输出目录下）：多素材/复杂需求时
  Agent 先用 read_document / ocr_image 逐份获取内容，`write_working_doc` 整合为
  markdown（素材要点 + 初步生成方案），再基于它 create_diagram；
  `read_working_doc` / `write_working_doc` 工具可随时读取与整体改写，
  避免大量素材原文长期占用对话上下文。
- 支持对当前流程图的多轮自然语言修改（`modify_diagram` 在已有代码上修订，并重走渲染+视觉验证循环）。
- 流式状态显示：主 Agent 回复与生成循环的 Mermaid 原文以流式增量实时滚动展示（Live 区域，段落切换时清场，结束后整段擦除）；LLM 客户端优先 stream=True，服务商不支持或流传输失败时自动退回强制非流式重试，界面无感。
- 每次生成/修改的版本产物保存在 `output/generate/v<n>/`，当前结果固定在 `output/generate/current.*`；
  检查报告在 `output/generate/check_results/`，批量清单和汇总在
  `output/generate/batch_plans/`。
- Skill 抽象（name/description/inputSchema/handler）为最小级别，后续可平移到 MCP。
- 文件调整技能：`write_file`（新建/覆盖）、`replace_in_file`（精确替换）、
  `grep_files`（内容检索），供主 Agent 随时调整中间文档或按用户要求输出
  其它格式文件；写入路径限制在产物目录内，相对路径按产物目录解析。
- 命令执行（`run_command`，冒险功能）：主 Agent 可应用户明确要求运行单行
  shell 命令；界面层以红框展示命令、方向键选择「是/否」逐条确认，命令统一
  在产物目录下执行，执行中 Ctrl+C 直接杀掉子进程组，超时自动终止；
  `--yolo` 启动参数或会话内 `/yolo` 可切换免确认模式。
- 产物路径统一为绝对路径（会话入口将 output 目录 resolve），CLI 展示与
  日志中的生成位置均为绝对路径。

### FR-8 技能包系统（文件发现式，提示词型）
- 技能包即文件：`skills/` 目录下每个带 frontmatter 的 `.md` 文件是一个技能包，
  定义 `name` / `description`，正文为写给主 Agent 的操作手册。
  目录可用 `FLOWCHART_SKILL_DIR` 覆盖，默认 `./skills`。
- 主 Agent 自主发现与执行：`list_skill_packs` 列出技能包，`use_skill` 读取正文
  注入对话并遵照执行（可编排 read_document / find_files / 流程图工具完成多步流程）；
  系统提示要求遇到陌生领域任务时先查技能包。
- 定位是"指引型"技能（社区 SKILL.md 式）：不含可执行脚本；需要跑代码的能力
  做成内置工具（`skills/builtin.py`）。
- 无 frontmatter 的文件（如 skills/README.md）自动忽略；内置示例 drawio-export。

### FR-9 文档检视（检查图大类）
- 一级路由（`router.py`）：每条用户输入先分类 generate / check / chat；
  check 只在当前 Session 存在合法 `kind: check` Skill 时执行，否则要求用户提供标准。
- 文件子 Agent 获得通用 `image_reasoning` 工具；调用参数为完整 prompt 与图片路径。
  工具不内置检查项、分类、判定或报告格式，这些都由 Check Skill 的正文定义。
- 子 Agent 按 Skill 决定检查项、读取必要文档、逐项调用 `image_reasoning`，最后用
  `write_file` 产出 CSV；未配置视觉模型时明确回复不可用。
- `image_reasoning` 以 `subagent.tool.progress` 流式发送推理与正文增量；Web 工具详情
  实时刷新，TUI 继续以子 Agent 独立颜色展示进度。
- 批量检查先按 Skill 的 `## batch` 约定扫描目录并生成 `batch_plan.json`，规划子 Agent
  随即结束；每个案例独立执行检查，报告写入
  `output/generate/check_results/<case-id>/report.csv`，汇总写入
  `output/generate/batch_plans/`。
- 新增或修改检查行为只需编辑 Check Skill；无需修改 Core 或注册表。

## 5. 非功能需求

- **依赖最小化**：Python 侧仅 `openai` + `python-dotenv` + TUI 两库；渲染依赖 `mmdc`（源码运行用系统安装，离线包内置 vendor/node + mermaid-cli）。
- **离线分发**：PyInstaller 单文件冻结（`packaging/flowchart-agent.spec`）+ `packaging/make_bundle.py` 组装 vendor（Node 独立二进制、mermaid-cli 跳过 Chromium、语法预检依赖）与 styles/skills 模板，产出 win-x64 zip 与 macOS tar.gz；`.github/workflows/release.yml` 在 `v*` tag 上自动构建并挂 Release。冻结后 styles/skills/.env 默认解析到 exe 旁目录（`runtime.py`），渲染用浏览器自动探测（CHROME_PATH 优先）。CI 不调用任何 LLM API。
- **可观测性**：每次生成/修改的完整过程写入 `output/generate/v<n>/run.log`（run 模式为 `output/generate/run.log`）；检查过程以子 Agent 与 `image_reasoning` 工具事件进入会话事件流，报告写入 `output/generate/check_results/`。任务上下文、每轮生成/渲染/验证结果、每次 LLM 请求（模型、流式与否、消息数、耗时、输出规模）与 mmdc 渲染命令均可追踪；chat 模式另有会话级 `output/chat.log` 记录全部用户输入与工具调用（含参数与结果摘要）。每轮中间产物（mmd/图片）与模型原始输出（`round_<n>_generate_raw.txt` / `round_<n>_verify_raw.txt`）落盘，可复盘分步生成过程。
- **可测试性**：渲染、提取等纯逻辑模块可单测；LLM 调用集中在 client 层便于 mock。
- **失败可读**：最终失败时给出人类可读的诊断（哪一步卡住、模型最后的批评意见）。

## 6. 验收标准

1. 对 `test_datas/gen/` 中至少 3 份流程文档，端到端跑出通过视觉验证的 PNG 图。
2. 人为注入一份有分支判断的文档，生成的图中分支逻辑与文档一致。
3. 人为把 API key 改错，工具给出清晰报错而非 traceback 淹没。
4. 渲染失败 loop 可演示：首轮生成非法 Mermaid 时能在后续轮次自行修复（可通过测试桩模拟）。

## 7. 里程碑

| 阶段 | 内容 | 产出 |
|---|---|---|
| M1 | 框架搭建 + 配置层 + 渲染封装 | 可运行的最小骨架（本阶段） |
| M2 | 生成→渲染校验 loop 打通 | 对样例文档生成语法正确的图 |
| M3 | 多模态验证 loop 接入 | 语义验证生效，可自动修复语义错误 |
| M4 | 交互反馈模式 + 更多图型 | `--feedback` 可用，支持 sequence 等 |
| M5 | 测试与文档完善 | 单测覆盖纯逻辑模块，README 完整 |
