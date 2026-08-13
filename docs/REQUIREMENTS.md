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

### 非目标（Non-Goals，本阶段）

- 不做 Web UI / 服务化部署（后续阶段再考虑）。
- 不做图表的精细布局调优（节点位置、样式美化仅做基础支持）。
- 不支持手绘图/图片逆向生成文档（仅文档 → 图）。

## 2. 用户场景

1. 用户提供一份流程描述文档（如 `test_datas/1.txt` 的登录流程文档），工具输出：
   - `*.mmd` Mermaid 源码文件
   - `*.png`（或 svg）渲染图
2. 用户直接粘贴一段自然语言描述，工具交互式生成图表。
3. 用户对结果不满意时，提供自然语言反馈，Agent 在上一轮结果基础上修正。

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
| `prompts.py` | 生成、修复、视觉验证三类 Prompt 模板 |
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

## 4. 功能需求

### FR-1 文档生成 Mermaid
- 输入：自然语言文档文本（文件路径或字符串）；可选参考图片（手绘草图、现有流程图截图，`TEXT_MODEL_VISION=true` 时随首轮生成消息发给主模型）。
- 输出：Mermaid 代码（自动提取 ```mermaid 代码块，容忍模型输出的多余解释文字）。
- 图型默认 `flowchart TD`；文档明确为时序/类图等时可切换。

### FR-2 渲染校验 loop（语法/结构层）
- 渲染前先经 `mermaid.parse` 快速语法预检（Node + jsdom，约 1 秒，不启动 Chromium）；预检失败直接反馈修复，省去完整渲染。
- 使用 `mmdc`（mermaid-cli）渲染，作为语法与结构的权威校验。
- 渲染失败时，将 stderr 错误信息 + 当前代码反馈给文本模型修复，进入下一轮。
- 渲染产物：`output/round_<n>.mmd` 与 `output/round_<n>.png`，保留每轮中间结果便于调试；最终产物同时输出 SVG（`current.svg` / `final.svg`）。
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
- run 模式支持 `--style <名称>`；风格指令注入代码开头（已有 init 指令时不重复注入），
  最终 .mmd 产物自带风格，导入 draw.io 等工具渲染效果一致。

### FR-3 多模态视觉验证 loop（语义层）
- 渲染成功后，把 PNG 图片 + 原始文档一起交给多模态模型。
- 验证模型输出结构化结论：`PASS` 或 `FAIL: <具体问题列表>`。
- 检查维度（写入验证 Prompt）：
  - 节点是否完整覆盖文档中的步骤；
  - 节点之间的连接/方向是否正确；
  - 分支判断逻辑是否与文档一致；
  - 节点文字是否与文档语义一致（无臆造、无遗漏）。
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
- 图像输入（`TEXT_MODEL_VISION=true` 时启用）：用户可在 TUI 拖入图片（显示为彩色芯片，Backspace 整块删除）随消息发给主模型；`read_image` Skill 支持模型主动查看指定路径图片；`create_diagram` 接受 `image_path` 参考图；修改时把当前渲染图一并发给主模型。无视觉能力时 `read_image` 不下发，图片输入给出明确提示。
- 支持对当前流程图的多轮自然语言修改（`modify_diagram` 在已有代码上修订，并重走渲染+视觉验证循环）。
- 每次生成/修改的版本产物保存在 `output/v<n>/`，当前结果固定在 `output/current.*`。
- Skill 抽象（name/description/inputSchema/handler）为最小级别，后续可平移到 MCP。

## 5. 非功能需求

- **依赖最小化**：Python 侧仅 `openai` + `python-dotenv`；渲染依赖系统安装的 `mmdc`（`npm i -g @mermaid-js/mermaid-cli`）。
- **可观测性**：每轮打印阶段日志（生成/渲染/验证结果），同时写入 `output/run.log`；每轮中间产物（mmd/图片）与模型原始输出（`round_<n>_generate_raw.txt` / `round_<n>_verify_raw.txt`）落盘，可复盘分步生成过程。
- **可测试性**：渲染、提取等纯逻辑模块可单测；LLM 调用集中在 client 层便于 mock。
- **失败可读**：最终失败时给出人类可读的诊断（哪一步卡住、模型最后的批评意见）。

## 6. 验收标准

1. 对 `test_datas/` 中至少 3 份流程文档，端到端跑出通过视觉验证的 PNG 图。
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
