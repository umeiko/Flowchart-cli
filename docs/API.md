# Flowchart Agent HTTP API

API 版本：`v1`（最小服务化阶段）
默认地址：`http://127.0.0.1:8765`

服务启动后可访问：

- Web 工作台：`/`
- Swagger UI：`/docs`
- OpenAPI JSON：`/openapi.json`
- 健康检查：`/health`

## 启动

```bash
uv run flowchart-agent server
uv run flowchart-agent server --host 127.0.0.1 --port 8765 -o output
```

服务参数也可统一放在 `.env`，命令行参数优先：

```ini
SERVER_HOST=127.0.0.1
SERVER_PORT=8765
SERVER_OUTPUT=output
SERVER_DATA_DIR=
SERVER_OPEN_BROWSER=true
```

Windows 离线包可双击 `launch_server.exe`：它调用同目录的
`flowchart-agent.exe server`，等待 `/health` 就绪后按 `SERVER_OPEN_BROWSER`
打开网页。`launcher.exe` 仍负责首次模型配置和启动 TUI。

当前 Server 是单进程 MVP。用户、登录令牌、Session 元数据和对话历史保存在 SQLite；
服务重启后会按需恢复 Session 与 Agent 历史。运行中的 Run 状态仍为进程内状态。

`-o/--output` 同 TUI 的 `flowchart-agent chat -o output`。每个用户的每个 Session
使用独立目录 `output/server/users/<user-id>/sessions/<session-id>/`；API 只暴露当前
登录用户的当前 Session。工作区之外的路径返回 `403`。

## 基本流程

```text
POST /v1/sessions
       ↓
POST /v1/sessions/{session_id}/files       可选
       ↓
POST /v1/sessions/{session_id}/runs
       ↓
GET  /v1/runs/{run_id}/events              SSE
GET  /v1/sessions/{session_id}/active-run  切换页面后恢复运行状态
POST /v1/runs/{run_id}/cancel              停止运行
       ↓
GET  /v1/sessions/{session_id}/diagram
```

## 用户与头像

- `GET /v1/auth/me`：读取当前用户；设置头像后返回 `avatar_url`。
- `GET /v1/auth/avatar`：读取当前用户头像。
- `PUT /v1/auth/avatar`：请求体直接发送图片二进制，上传或替换头像。
- `DELETE /v1/auth/avatar`：移除头像并恢复用户名首字母占位图。

头像保存在 SQLite 的当前用户记录中，与 Session 文件树隔离。服务端根据文件签名只接受
PNG、JPEG、WebP 和 GIF，单文件上限 2 MB；不接受可能包含活动内容的 SVG。旧数据库会在
启动时自动增加头像字段。Web 右上角以“头像 + 用户名”显示账户入口，用户设置和退出登录
位于其二级菜单。

## 会话

### `POST /v1/sessions`

```json
{
  "engine": "mermaid",
  "verification_mode": "full",
  "style": "default"
}
```

字段均可省略，默认值来自服务端 `.env`。

## Session 文件工作区

- `GET /v1/sessions/{session_id}/workspace/tree`：只返回当前用户当前 Session 的文件树。
- `GET /v1/sessions/{session_id}/workspace/files/content?path=workspace/需求.md`：读取 Session 文件。
- `GET /v1/sessions/{session_id}/workspace/files/download?path=workspace/需求.md`：下载文件；路径为目录时，服务端临时打包为保留目录层级的 ZIP 后下载。
- `POST /v1/sessions/{session_id}/workspace/files?filename=需求.md`：保存到 Session 私有工作区。
- `POST /v1/sessions/{session_id}/workspace/entries`：新建空白文件或目录。
- `POST /v1/sessions/{session_id}/workspace/transfer`：复制、移动或重命名文件/目录。
- `DELETE /v1/sessions/{session_id}/workspace/entries?path=workspace/旧文件.txt`：递归删除文件或目录。

文件树只展示 `workspace/`（用户上传）、`attachments/`（对话附件）、`generate/`
（多轮生成源码与日志）和 `check/`（验证产物）。`client/`、其他 Session、用户目录和
数据库均不可见。`path` 必须相对于当前 Session 根目录；服务端拒绝 `../`、绝对路径和
符号链接越界。Web 工作台右侧预览区支持 Markdown 渲染、CSV 表格、XML/drawio/
Mermaid/文本源码查看，以及 PNG/JPEG/WebP/GIF/BMP/SVG 图片预览。CSV 预览最多展示
前 500 行、80 列，完整内容仍可通过“打开原文件”获取。

同一边界也强制应用于 Agent 的 `read_document`、`find_files`、`grep_files`、
`read_image`、`ocr_image`、`create_diagram.image_path` 和文档检查分支。工具参数中的
相对路径按当前 Session 解析；默认搜索 `.` 只遍历上述四个目录，不能读取服务端项目目录、
数据库、其他用户或其他 Session。TUI 不经过 Server，仍保留原有的本机文件读取行为。

四个 Workspace 顶级目录不可重命名、移动或删除。所有结构变更都拒绝覆盖现有目标，
Session 执行 Run 时返回 `409`。Web 文件树支持拖拽移动和右键复制、剪切、粘贴、
重命名、删除、文件下载、目录 ZIP 下载，以及新建目录和空白文本文件。

同名上传不会覆盖现有文件，服务端依次使用 `_1`、`_2` 后缀。拖到 Web 文件树区域
调用工作区上传接口；拖到对话输入区则调用会话附件接口，文件保存到当前 Session，
并把返回的文件 ID 加入下一次 Run 的 `attachments`。

### `GET /v1/sessions/{session_id}`

读取会话配置、版本和当前是否有图。

### `GET /v1/sessions/{session_id}/messages`

返回完整聊天历史。为避免泄露部署机器目录，Web API 会把当前 Session 内文件的绝对路径
转换为 `workspace-file:<编码后的相对路径>` 链接；工作台点击链接后会在文件树中定位文件，
并在右侧预览。数据库和 TUI 仍保留原始绝对路径，便于本机命令行直接访问。

### `PATCH /v1/sessions/{session_id}`

请求结构与创建会话相同。会话正在运行任务时返回 `409`。

### 上下文统计与压缩

- `GET /v1/sessions/{session_id}/context`：返回主 Agent 当前消息、工具定义和总上下文的
  token 估算、配置窗口及占用百分比。
- `POST /v1/sessions/{session_id}/context/compact`：调用一次主文本模型，把旧对话与工具结果
  压缩成工作摘要并保留最近一轮；Run 执行中返回 `409`。

统计使用跨模型的近似字符算法，仅用于容量提示，不等同于模型供应商计费 token。
窗口大小由 `.env` 的 `TEXT_MODEL_CONTEXT_WINDOW` 配置，默认 `128000`。Web 的摘要与截断点
保存在 SQLite，服务重启后继续生效；完整聊天记录仍保留用于界面查看，不会因压缩而删除。
TUI 对应 `/context` 和 `/compact` 命令，其摘要仅在当前 TUI 进程内生效。

### 文件子 Agent 1.0

主 Agent 可调用 `delegate_task`，把较大文件（建议 32KB 以上）、跨文件检索和局部编辑
交给一个无持久历史的文件子 Agent。每个 Session 同一时刻只能运行一个子 Agent；任务完成后
只把精炼报告交回主 Agent，过长结果会被截断以保护主上下文。

主 Agent 与子 Agent 共用 `list_dir`；子 Agent 还固定拥有 `read_document`、`find_files`、`grep_files`、`write_file`、
`replace_in_file`；根据模型与界面配置附加 `read_image`、`ocr_image`、`run_command`。
`list_dir` 以固定两级 tree 展示目录，目录优先且文件附带大小，不读取文件内容；两端使用
同一实现与 Session 路径边界。Server 为主/子 Agent 额外注入 Session 路径策略：文件工具
只能搜索 `workspace/`、`attachments/`、`generate/`、`check/`，参数和结果均使用 Session
相对路径，不向模型或 WebUI 暴露部署机器绝对路径；TUI 保留本机绝对路径表现。
配置视觉模型时，文件子 Agent 额外获得通用 `image_reasoning` 工具。参数只有视觉模型
prompt 和当前 Session 内的图片路径；工具本身不知道检查项、适用分类、PASS/FAIL/NA 或
CSV 格式。检查路由会把当前 Session 中完整的 `kind: check` Skill 交给子 Agent，由 Skill
定义如何选择检查项、何时读取文档、如何构造视觉 prompt、调用几次以及怎样汇总。
`image_reasoning` 的推理增量和正文增量通过 `subagent.tool.progress` 发送；WebUI 中点击
“图像推理”工具卡即可在详情窗口实时查看请求、视觉模型推理和输出，完成后显示最终结果。

批量请求由主 Agent 按 Check Skill 的 `## batch` 协议编排：先启动短生命周期文件子 Agent，
且本次只向它暴露 `list_dir`、`read_document`、`write_file`。它负责扫描目录、必要时读取少量
文档并把配对关系写入 `output/generate/batch_plans/batch_plan_<id>.json`，随后立即结束；
主 Agent 校验计划内的 Session 相对路径，再逐 case 启动独立的 Skill 驱动检查任务。每个
case 的子 Agent 通过 `image_reasoning` 得到结论并将 CSV 写到
`output/generate/check_results/<case-id>/report.csv`，最后把状态汇总到同一
`batch_plans/*_summary.json`。目录扫描、图文配对和实际检查协议均由 Skill 维护。
它不能生成流程图、修改配置、加载 Skill/Style，也不能再创建子 Agent。Server 不提供
`run_command`，TUI 仍沿用命令确认机制。`find_files` 和 `grep_files` 的结果包含文件大小，
供主 Agent 判断是否应委派。子 Agent 的上限按“返回工具调用的模型回合”计数，而不是按
单个工具计数；默认 `MAX_SUBAGENT_TOOL_ITERATIONS=24`。部分兼容端点每回合只发一个工具
调用，检查项很多时可在 `.env` 中提高到 48 等值；达到上限的错误会同时报告工具回合数
与累计工具调用数。

## Client Skills / Styles

创建 Web Session 时，服务端把项目默认 `skills/*.md` 和 `styles/*.md` 复制到：

```text
output/server/users/<user-id>/sessions/<session-id>/client/skills/
output/server/users/<user-id>/sessions/<session-id>/client/styles/
```

每个 Session 的副本和挂载状态彼此隔离，不修改项目默认目录：

- `GET /v1/sessions/{session_id}/client/skills`
- `GET /v1/sessions/{session_id}/client/styles`
- `POST /v1/sessions/{session_id}/client/{kind}?filename=my-skill.md`
- `POST /v1/sessions/{session_id}/client/{kind}/generate`
- `GET /v1/sessions/{session_id}/client/{kind}/{name}`
- `PATCH /v1/sessions/{session_id}/client/{kind}/{name}`
- `DELETE /v1/sessions/{session_id}/client/{kind}/{name}`

编辑或挂载请求：

```json
{
  "content": "---\nname: ...\n---\n...",
  "mounted": true
}
```

创建/上传接口的请求体是 UTF-8 Markdown 原文。Skill 与 Style 都必须以标准
`---` front matter 开头，并至少包含非空 `name`、`description`；Style 可额外声明
`background` 与 `init`。格式不合法的文件会返回 `400`，`README.md` 等无法解析的
Markdown 不出现在资源列表中，也不能挂载。服务端默认资源带 `builtin: true`，不可
删除；用户新建或上传的资源可删除。

检查标准也是 Session Skill。其 front matter 额外声明 `kind: check`，正文用
`## check: <id> | <名称>` 与 `applies_to: <类型列表或 *>` 定义检查项；默认示例为
`skills/Check.md`，并可用 `## execution` / `## batch` 规定单项执行与批量规划流程。
路由为检查任务后，Core 只加载当前 Session 中这类 Skill 的完整正文并交给文件子
Agent；子 Agent 按正文调用通用 `image_reasoning`，Core 不解析检查项、不做二级分类，
也不判定 PASS/FAIL/NA。若没有合法的检查 Skill，接口返回拒绝说明并要求用户提供审查
标准文档，不会使用代码内置或模型臆造的标准。

AI 生成接口接收 `{"name":"...","description":"..."}`。Style 复用带真实
Mermaid 试渲染的 `create_style`；Skill 使用对称的 `create_skill` 子 Agent 生成并
校验 front matter。两者均写入当前 Session 的客户端目录，而不是项目默认目录。

挂载状态保存在 SQLite 的 `resource_mounts` 表，刷新页面、切换 Session 和重启服务后
仍会恢复。Skill 可以同时挂载多个；Style 每个 Session 只能激活一个，挂载新 Style
会自动替换旧 Style。

每次 Run 前，服务端会同时执行两层保障：把挂载资源作为“用户明确要求使用”的指令
注入主 Agent Prompt，并确定性激活 Core。挂载 Skill 会调用同等的 `use_skill` 逻辑，
把 `prompt_hint`（旧 Skill 无此字段时使用正文）直通作图子模型，同时应用布局参数；
挂载 Style 会直接设置 `DiagramSession.style`。因此不依赖主 Agent 是否自行决定调用
`use_skill/set_style`。执行中的 Run 不允许创建、编辑、删除资源或修改挂载状态，返回
`409`。项目原生资源工具也继续从当前 Session 的客户端副本发现资源。

生成类任务在调用作图工具前会额外执行一次 Skill 相关性路由，根据本轮需求和所有已
挂载 Skill 的名称、类型、描述及指引摘要判断是否明显无关。若存在无关 Skill，任务会
在作图前拒绝，并点名提醒用户取消挂载；Core 不维护固定场景或 Skill 白名单。检查过程
与结果始终追加到 `generate/run.log`；通过检查并实际开始生成后，也会写入对应
`generate/v<n>/run.log`，与该轮后续生成日志连续展示。

## 附件

### 从 output 工作区加入附件

`POST /v1/sessions/{session_id}/files/from-workspace`

```json
{"path": "generate/current.mmd"}
```

该接口不复制文件，只把现有工作区文件注册为当前 Session 的待用附件并返回 `file_id`；
Web 中选中文件后点击“加入聊天附件”即可调用。路径仍受 `output` 边界检查保护。

### `POST /v1/sessions/{session_id}/files?filename=requirements.md`

请求体直接发送文件二进制，不使用 multipart。响应：

```json
{
  "id": "file_...",
  "filename": "requirements.md",
  "size": 1024
}
```

提交 Run 时将文件 ID 放入 `attachments`。浏览器本地路径不会传给 Agent。

## Run

### `POST /v1/sessions/{session_id}/runs`

```json
{
  "input": "根据附件生成登录流程图",
  "attachments": ["file_..."]
}
```

接口立即返回 `202`；任务在后台线程执行。同一会话同时只允许一个活动 Run，不同会话可并行。

### `GET /v1/runs/{run_id}`

状态为 `queued`、`running`、`cancelling`、`completed`、`failed` 或 `cancelled`。

### `GET /v1/sessions/{session_id}/active-run`

返回当前 Session 的活动 Run；没有活动任务时返回 `null`。Web 工作台切换回正在出图的
Session 时通过该接口恢复停止按钮，并重新订阅 SSE 事件。

### `POST /v1/runs/{run_id}/cancel`

请求合作式取消。服务端会在模型调用返回、渲染完成和验证完成等安全检查点停止后续步骤；
已经成功渲染的最后一轮候选图会保留为 `current.*`。正在进行的单次模型 HTTP 请求不会被
强制中断。

### `GET /v1/runs/{run_id}/events`

响应类型为 `text/event-stream`。支持标准 `Last-Event-ID` 请求头和
`?after=<event-id>` 断点续读。事件数据中的当前 Session 文件路径也使用与聊天历史相同的
Web 安全链接，不发送部署机器的绝对路径。

```json
{
  "id": 3,
  "run_id": "run_...",
  "session_id": "sess_...",
  "type": "assistant.delta",
  "timestamp": "2026-08-29T02:20:30+00:00",
  "data": {"text": "正在生成"}
}
```

| 事件类型 | 说明 |
| --- | --- |
| `run.queued` | Run 已进入队列 |
| `run.started` | Run 开始执行 |
| `run.cancelling` | 已收到停止请求，正在等待安全检查点 |
| `assistant.delta` | 主 Agent 回复文本增量 |
| `reasoning.status` | 思考状态，不包含原始思维链 |
| `reasoning.delta` | 生成/验证模型的推理增量，供工作台灰色单行滚动展示；不持久化 |
| `tool.started` | Agent 开始调用工具；`data.arguments` 为模型传入的工具参数 |
| `generation.round_started` | 生成循环开始新一轮 |
| `generation.delta` | 图表源码生成增量 |
| `generation.stage` | 生成、渲染、视觉/代码验证等单行阶段状态 |
| `workspace.changed` | 当前 Run 可能已写入中间产物；`data.refresh_diagram` 表示是否也应刷新当前图 |
| `verification.delta` | 视觉/代码验证模型的流式响应进度；正文片段或工具参数字符数 |
| `usage.delta` | 本轮流式字符增量，Web 端据此展示近似 token 数与耗时 |
| `tool.completed` | 工具调用完成；`data.result` 为返回给模型的完整结果，Web 可点开调用记录排查 |
| `subagent.started` | 文件子 Agent 接手任务；Web 中该记录可点击，详情上方显示完整委派输入，下方实时汇集子 Agent 思考与当前输出 |
| `subagent.reasoning.delta` | 子 Agent 推理增量，Web/TUI 以独立颜色展示 |
| `subagent.delta` | 子 Agent 最终报告的流式增量 |
| `subagent.tool.started` | 子 Agent 开始调用受限工具 |
| `subagent.tool.progress` | 子 Agent 工具的流式进度；`image_reasoning` 会附带 `reasoning_delta` / `output_delta`，Web 工具详情实时追加展示 |
| `subagent.tool.completed` | 子 Agent 工具调用完成 |
| `subagent.completed` / `failed` / `cancelled` | 子 Agent 结束状态 |
| `progress.updated` | 路由、批量规划与逐案例调度等阶段性进度 |
| `resource.activated` | Run 前服务端已确定性激活一个挂载的 Skill 或 Style |
| `run.completed` | 完成，`data.reply` 为最终回复 |
| `run.failed` | 失败，`data.error` 为错误信息 |
| `run.cancelled` | 已停止，`data.reply` 为停止说明 |

## 当前图与产物

- `GET /v1/sessions/{session_id}/diagram`：当前版本、引擎、源码和产物 ID。
- `GET /v1/sessions/{session_id}/artifacts`：列出产物。
- `GET /v1/sessions/{session_id}/artifacts/{artifact_id}/content`：下载产物。

`reasoning.delta` 只在当前 Run 的内存事件流中短暂存在，不写入消息历史；Web 只保留并
展示最新一行。若模型网关不提供 `reasoning_content`，界面退化为“思考中…”状态。
`usage.delta` 是界面响应度指标，不等同于模型供应商账单中的精确 token usage。

每轮图表代码成功渲染后，Core 会在视觉验证前立即同步 `current.mmd/current.drawio`、
`current.png` 和 `current.svg`。因此即使后续验证失败、进入下一轮或被用户停止，当前图
也指向本次 Run 最新的可渲染候选，而不是上一次历史版本。

`create_diagram` 与 `modify_diagram` 工具提供可选布尔参数 `visual_verification`
（默认 `true`）。主 Agent
识别到用户明确要求快速、简单、尽快或直接出图时传 `false`；Core 仍执行代码生成与
渲染校验，但首次成功渲染后立即发布结果，不调用视觉/代码验证模型，也不会产生
`verification.delta` 或 `round_*_verify_raw.txt`。该参数仅作用于本次创建，不修改
Session 的默认验证模式；新建和修改均遵循相同规则。

## 当前限制

- 已提供面向内网 MVP 的用户名/密码认证和用户级隔离；尚未加入公网级安全能力。
- Server 不注册 `run_command`，无法通过 Web 服务执行任意 shell 命令。
- 当前使用单进程内存状态，不应启动多个 Uvicorn worker。

协议演进原则：在 `/v1` 内优先做向后兼容扩展；破坏性变更使用新的主版本路径。
