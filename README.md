# Flowchart AI Agent

**用自然语言生成 Mermaid 流程图的循环式 Agent**：生成 → 语法预检 → mermaid-cli 渲染 → 多模态视觉验证 → 反馈修复，直到图真正画对为止。

![main](docs/images/main.png)


## 效果示例

| 线性流程（登录） | 分支与回退（注册） |
| :---: | :---: |
| ![登录流程](docs/images/case_login.png) | ![注册流程](docs/images/case_register.png) |
| 输入：`test_datas/1.txt`，1 轮通过 | 含判断分支与回退循环，1 轮通过 |

<details>
<summary>登录流程的输入文档与生成代码</summary>

输入（`test_datas/1.txt` 节选）：

```
开始 → 输入手机 → 输入密码 → 登录 → 登录成功 → 完成
```

输出（`docs/images/case_login.mmd`）：

```mermaid
flowchart TD
    A([开始]) --> B[输入手机]
    B --> C[输入密码]
    C --> D[登录]
    D --> E[登录成功]
    E --> F([完成])
```
</details>

更多玩法：

- **对话修改**：`把登录改成验证码登录` —— 在现有图上修订，重走验证循环
- **贴图生成**：拖入手绘草图/现有流程图截图（`TEXT_MODEL_VISION=true`），模型看图作画；主模型无视觉能力时也不怕——`ocr_image` 工具用验证模型把素材图片里的文字和连接关系逐字提取出来再作画
- **工作文档**：素材多/需求杂时，Agent 会把各素材要点和初步生成方案整合成 `working_doc.md`（可用工具随时读写修改），再基于它生成，不把所有素材原文堆在对话里
- **流式输出**：模型回复与 Mermaid 生成过程边产出边滚动显示
- **风格模板**：`styles/` 目录下每个 `.md` 文件就是一个风格插件，主 Agent 自己发现并按需选用；往目录丢一个 markdown 即可新增风格；`default.md` 始终生效，编辑它即可定制全局默认风格
- **风格生成 Agent**：现有模板都不满意时直接说"帮我做一个手绘风格的模板"，`create_style` 工具会自动起草风格插件、校验格式、试渲染，通过后写入 `styles/` 并立即启用
- **风格转换 Agent**：`换成深色风格`、`按这个文档调整风格` —— 只改样式层不改内容，骨架校验机械保证节点/连线/文字零改动；风格来源可以是现有模板（`style_name`）或口述/文档输入的风格要求（`style_document`）
- **技能包（Skill Packs）**：`skills/` 目录下每个带 frontmatter 的 `.md` 就是一个提示词型技能，主 Agent 遇到陌生领域任务时自己发现并遵照执行；从社区看到好用的 SKILL，丢进目录即可用。

## 安装

```bash
# 1) Python 依赖（推荐 uv，按 uv.lock 锁定版本；会自动创建 .venv）
uv sync
# 无 uv 时：python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# 2) Node.js ≥ 18（渲染与语法预检的运行时）
brew install node        # macOS；Windows 用官网安装包，内网离线装法见部署文档 §4.2

# 3) Mermaid 渲染器（全局安装，提供 mmdc 命令）
npm install -g @mermaid-js/mermaid-cli

# 4) 语法预检依赖（读取项目根目录 package.json，安装 mermaid + jsdom，
#    供 scripts/mermaid_parse.mjs 使用；可选但推荐，
#    跳过则自动退回纯 mmdc 校验，不影响主流程）
npm install
```

Windows 用户建议使用 Windows Terminal 或 VS Code 终端。

## 配置

```bash
cp .env.example .env
# 编辑 .env
```

`.env` 中各项含义见 `.env.example` 注释。公司 Windows 环境 mmdc 因 puppeteer 自带
Chromium 不可用而报错时，设置 `CHROME_PATH` 指向本机 `chrome.exe`（或 Edge），工具会
自动生成 puppeteer 配置并用 `-p` 渲染，详见 `docs/DEPLOYMENT.md` §4.3。

## 使用

```bash
# 交互模式（推荐）：口述需求或给文档路径，可持续对话修改
uv run flowchart-agent chat -o output

# 批处理模式：单文档 → 图，跑完退出（--style 指定 styles/ 目录中的风格模板）
uv run flowchart-agent run test_datas/1.txt -o output
uv run flowchart-agent run test_datas/1.txt -o output --style dark
```

## 实现方案

![双层 Agent 生成验证循环](docs/images/architecture_loop.svg)

上图就是本工具的工作方式：主 Agent（文本 LLM + function calling）理解你的意图并调度 Skill，
子循环负责把图画对——语法预检不合法就修，视觉验证不通过就改，最多迭代若干轮后收敛。

详细设计见 [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)，部署见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 目录结构

```
flowchart_agent/
├── config.py            # dotenv 配置
├── images.py            # 图片校验与 base64 编码
├── llm/client.py        # OpenAI 兼容客户端
├── mermaid/extract.py   # 从 LLM 输出提取 Mermaid 代码
├── mermaid/render.py    # mmdc 渲染 + 快速预检
├── prompts.py           # 生成/修复/验证 Prompt
├── agent.py             # 生成-渲染-验证子循环
├── session.py           # 会话状态
├── skills/              # 最小 Skill 抽象
│   ├── base.py          #   Skill 定义
│   └── builtin.py       
├── main_agent.py        # 主 Agent
├── styles.py            # 风格插件加载器（扫描 styles/*.md）
├── tui_chips.py         # TUI 处理
├── chat_cli.py          # 交互式 REPL
└── cli.py               # 命令行入口
styles/                  # 作图风格插件（.md 文件，可自行新增）
scripts/
└── mermaid_parse.mjs    # Node 侧语法预检
docs/
├── REQUIREMENTS.md      # 开发需求文档
├── DEPLOYMENT.md        # 部署指南
```
