# 部署指南

版本：v1.0.0
适用：flowchart-ai 1.x

## 1. 运行时依赖总览

| 依赖 | 版本要求 | 用途 | 是否可离线 |
|---|---|---|---|
| Python | ≥ 3.10 | 主程序 | 可（uv 可代装） |
| uv | ≥ 0.5 | Python 环境与依赖管理 | 可（单文件二进制） |
| Node.js | ≥ 18 LTS | 运行 mmdc 与语法预检 | 可（离线安装包） |
| mermaid-cli（mmdc） | 11.x | 渲染（内含 Chromium） | 需注意 Chromium 下载，见 §5.3 |
| 项目本地 npm 依赖 | mermaid + jsdom | 语法预检 | 可（随仓库或离线包） |
| 模型 API | OpenAI 兼容端点 | 生成/验证 | **硬网络需求** |

软件本体是 CLI 工具，无常驻进程、无数据库、无端口占用。

## 2. 离线二进制包（推荐：免 uv / npm 安装）

适合网络受限或不想装工具链的环境（如同事机器、公司内网）。GitHub Release 提供
开箱即用包（推送 `v*` tag 由 `.github/workflows/release.yml` 自动构建）：

| 包 | 平台 |
| --- | --- |
| `flowchart-agent-win-x64.zip` | Windows 10/11 x64 |
| `flowchart-agent-macos-arm64.tar.gz` | macOS Apple Silicon |
| `flowchart-agent-macos-x64.tar.gz` | macOS Intel |

包内已含：主程序、Node 独立二进制、mermaid-cli 与语法预检依赖（`vendor/`，
安装时已跳过 Chromium 下载）、`styles/` 与 `skills/` 模板、`.env.example`、
快速上手说明。**无需安装 uv、Python、Node、npm**，唯一外部依赖是系统浏览器。

使用步骤：

1. **Windows 最简单方式**：双击包内 `launcher.exe`，按提示填 3 项模型配置
   （API 地址 / Key / 模型名；浏览器地址自动嗅探，一般不用填），完成后自动
   进入对话模式；以后每次使用也直接双击它——检测到合法配置就跳过向导。
2. 手动方式：解压后复制 `.env.example` 为 `.env` 填入模型配置。
   渲染用系统浏览器：Windows 装过 Chrome/Edge 即可（程序自动探测常见安装路径，
   探测不到再在 `.env` 配 `CHROME_PATH`）；macOS 自动探测 /Applications 下的 Chrome。
   macOS 首次运行如被 Gatekeeper 拦截：`xattr -d com.apple.quarantine flowchart-agent`。
3. 命令行运行 `./flowchart-agent chat`（Windows 为 `flowchart-agent.exe chat`）；
   产物默认写到当前目录的 `output/`。

维护者本地打 macOS 包（CI 之外的调试手段）：

```bash
uv run --with pyinstaller pyinstaller packaging/flowchart-agent.spec \
  --distpath dist/bin --workpath build/pyinstaller --noconfirm
uv run python packaging/make_bundle.py --platform macos-arm64 --exe dist/bin/flowchart-agent
# 产物：dist/flowchart-agent-macos-arm64.tar.gz
```

## 3. 标准部署（可联网）

```bash
# 1) 安装 uv（任选其一）
curl -LsSf https://astral.sh/uv/install.sh | sh      # macOS/Linux
brew install uv                                       # macOS
winget install astral-sh.uv                           # Windows

# 2) 获取代码并安装 Python 依赖（自动建 .venv、按 uv.lock 锁定版本）
git clone <内网仓库地址> flowchart_ai && cd flowchart_ai
uv sync

# 3) 安装 Node 依赖
npm install                                           # 本地语法预检依赖
npm install -g @mermaid-js/mermaid-cli                # 全局渲染器

# 4) 配置模型
cp .env.example .env   # 填入文本/多模态模型的 key 与 base_url

# 5) 验证
uv run flowchart-agent run test_datas/gen/1.txt -o output
```

日常使用：`uv run flowchart-agent chat`（或 `uv run python -m flowchart_agent chat`）。

## 4. Windows 部署注意事项

- 推荐使用 **Windows Terminal** 或 VS Code 终端；老式 cmd.exe（GBK 代码页）可能出现
  边框字符/中文乱码，程序已做 UTF-8 best-effort 处理，但终端字体仍需支持 Unicode。
- mmdc 调用已内置 `cmd /c` 兼容分支，无需额外配置。
- 若遇到 puppeteer/Chromium 下载失败，见 §5.3 的离线方案。

## 5. 公司内网 / 离线部署

前提：准备一台**可联网的中转机**，把离线包制作好后拷贝进内网。

### 5.1 Python 侧

**有内网 PyPI 镜像时**（最简单）：

```bash
export UV_INDEX_URL=https://<内网pypi镜像>/simple
uv sync
```

**完全离线时**，在中转机执行：

```bash
# 中转机：导出锁定依赖并下载 wheel 包
uv export --format requirements-txt --no-hashes > requirements-lock.txt
pip download -r requirements-lock.txt -d wheels/
# 把 wheels/ 和 requirements-lock.txt 拷入内网
```

内网机器：

```bash
uv venv
uv pip install --no-index --find-links wheels/ -r requirements-lock.txt
uv pip install --no-index --find-links wheels/ -e .   # 安装项目本体（提供 flowchart-agent 命令）
```

> uv 本身是单文件二进制，直接拷贝 `uv`（或 `uv.exe`）到内网机器即可；Python 解释器也可用
> 中转机 `uv python install 3.13` 后，把 `~/.local/share/uv/python` 下对应目录整体拷贝。

### 5.2 Node.js 与本地 npm 依赖

- Node.js：下载对应平台的离线安装包（node-vXX.x.x.pkg / .msi / tar.xz）拷入内网安装。
- 项目本地依赖：`node_modules/` 可直接随仓库打包带走；或中转机 `npm install` 后
  `tar czf node_modules.tar.gz node_modules`，内网解压。也可配置内网 npm 镜像：
  `npm install --registry=https://<内网npm镜像>`。

### 5.3 mmdc 与 Chromium（离线部署的最大坑）

`npm i -g @mermaid-js/mermaid-cli` 时会由 puppeteer **联网下载 Chromium**，内网会失败。两种解法：

**方案 A：复用系统 Chrome（推荐）**

```bash
# 安装时跳过 Chromium 下载
PUPPETEER_SKIP_DOWNLOAD=1 npm i -g @mermaid-js/mermaid-cli
# Windows: 先 set PUPPETEER_SKIP_DOWNLOAD=1 再执行 npm i -g
```

然后在 `.env` 中把 `CHROME_PATH` 指向内网机器已有的 Chrome/Edge，工具会在每次渲染时
自动生成 `puppeteer-config.json` 并以 `mmdc -p` 指定它，无需手工维护 JSON：

```ini
# Windows 示例（.env 中反斜杠无需转义）
CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
# 或用 Edge：CHROME_PATH=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
# macOS 示例：CHROME_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
```

生成的配置文件固定为 `<输出目录>/.puppeteer-config.json`，内容为
`{"executablePath": "<CHROME_PATH>"}`；不设置 `CHROME_PATH` 时行为不变（使用 puppeteer 自带 Chromium）。

**方案 B：离线拷贝 Chromium**：中转机正常安装后，把 puppeteer 缓存目录
（`~/.cache/puppeteer`，Windows 为 `%USERPROFILE%\.cache\puppeteer`）整体拷贝到
内网机器相同位置。

### 5.4 模型 API

内网必须能访问 `.env` 中配置的两个 `BASE_URL`。若公司有自己的模型网关，
直接把 `TEXT_MODEL_BASE_URL` / `VISION_MODEL_BASE_URL` 指向内网网关（OpenAI 兼容协议）即可，
多模态模型需支持图片输入（如 qwen-vl 系列、gpt-4o 系列）。

## 6. 部署后验证清单

```bash
node --version && mmdc --version          # Node 与渲染器
uv run flowchart-agent --help             # 入口可用
node scripts/mermaid_parse.mjs --help 2>/dev/null || true   # 预检脚本存在
uv run flowchart-agent run test_datas/gen/1.txt -o output       # 端到端
```

端到端跑通标准：`output/final.mmd` 与 `output/final.png` 生成，`output/run.log` 无报错。

## 7. 升级与回滚

- 升级：`git pull && uv sync && npm install`（依赖以 `uv.lock` / `package-lock.json` 锁定，全环境一致）。
- 回滚：检出旧版本 tag 后重新 `uv sync` 即可。
- 版本号见 `pyproject.toml` 与 `flowchart_agent/__init__.py`（运行时从包元数据读取）。
