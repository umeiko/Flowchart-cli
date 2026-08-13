# 部署指南

版本：v1.0.0
适用：flowchart-ai 1.x

## 1. 运行时依赖总览

| 依赖 | 版本要求 | 用途 | 是否可离线 |
|---|---|---|---|
| Python | ≥ 3.10 | 主程序 | 可（uv 可代装） |
| uv | ≥ 0.5 | Python 环境与依赖管理 | 可（单文件二进制） |
| Node.js | ≥ 18 LTS | 运行 mmdc 与语法预检 | 可（离线安装包） |
| mermaid-cli（mmdc） | 11.x | 渲染（内含 Chromium） | 需注意 Chromium 下载，见 §4.3 |
| 项目本地 npm 依赖 | mermaid + jsdom | 语法预检 | 可（随仓库或离线包） |
| 模型 API | OpenAI 兼容端点 | 生成/验证 | **硬网络需求** |

软件本体是 CLI 工具，无常驻进程、无数据库、无端口占用。

## 2. 标准部署（可联网）

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
uv run flowchart-agent run test_datas/1.txt -o output
```

日常使用：`uv run flowchart-agent chat`（或 `uv run python -m flowchart_agent chat`）。

## 3. Windows 部署注意事项

- 推荐使用 **Windows Terminal** 或 VS Code 终端；老式 cmd.exe（GBK 代码页）可能出现
  边框字符/中文乱码，程序已做 UTF-8 best-effort 处理，但终端字体仍需支持 Unicode。
- mmdc 调用已内置 `cmd /c` 兼容分支，无需额外配置。
- 若遇到 puppeteer/Chromium 下载失败，见 §4.3 的离线方案。

## 4. 公司内网 / 离线部署

前提：准备一台**可联网的中转机**，把离线包制作好后拷贝进内网。

### 4.1 Python 侧

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

### 4.2 Node.js 与本地 npm 依赖

- Node.js：下载对应平台的离线安装包（node-vXX.x.x.pkg / .msi / tar.xz）拷入内网安装。
- 项目本地依赖：`node_modules/` 可直接随仓库打包带走；或中转机 `npm install` 后
  `tar czf node_modules.tar.gz node_modules`，内网解压。也可配置内网 npm 镜像：
  `npm install --registry=https://<内网npm镜像>`。

### 4.3 mmdc 与 Chromium（离线部署的最大坑）

`npm i -g @mermaid-js/mermaid-cli` 时会由 puppeteer **联网下载 Chromium**，内网会失败。两种解法：

**方案 A：复用系统 Chrome（推荐）**

```bash
# 安装时跳过 Chromium 下载
PUPPETEER_SKIP_DOWNLOAD=1 npm i -g @mermaid-js/mermaid-cli
# Windows: 先 set PUPPETEER_SKIP_DOWNLOAD=1 再执行 npm i -g

# 编写 puppeteer 配置，指向内网机器已有的 Chrome/Edge
cat > puppeteer-config.json <<'EOF'
{ "executablePath": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" }
EOF
# Windows 示例："executablePath": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
```

本工具渲染命令尚未暴露 `-p` 参数，离线部署时请在 `flowchart_agent/mermaid/render.py`
的 `_mmdc_command()` 中为 mmdc 追加 `"-p", "puppeteer-config.json"`（一行改动）。

**方案 B：离线拷贝 Chromium**：中转机正常安装后，把 puppeteer 缓存目录
（`~/.cache/puppeteer`，Windows 为 `%USERPROFILE%\.cache\puppeteer`）整体拷贝到
内网机器相同位置。

### 4.4 模型 API

内网必须能访问 `.env` 中配置的两个 `BASE_URL`。若公司有自己的模型网关，
直接把 `TEXT_MODEL_BASE_URL` / `VISION_MODEL_BASE_URL` 指向内网网关（OpenAI 兼容协议）即可，
多模态模型需支持图片输入（如 qwen-vl 系列、gpt-4o 系列）。

## 5. 部署后验证清单

```bash
node --version && mmdc --version          # Node 与渲染器
uv run flowchart-agent --help             # 入口可用
node scripts/mermaid_parse.mjs --help 2>/dev/null || true   # 预检脚本存在
uv run flowchart-agent run test_datas/1.txt -o output       # 端到端
```

端到端跑通标准：`output/final.mmd` 与 `output/final.png` 生成，`output/run.log` 无报错。

## 6. 升级与回滚

- 升级：`git pull && uv sync && npm install`（依赖以 `uv.lock` / `package-lock.json` 锁定，全环境一致）。
- 回滚：检出旧版本 tag 后重新 `uv sync` 即可。
- 版本号见 `pyproject.toml` 与 `flowchart_agent/__init__.py`（运行时从包元数据读取）。
