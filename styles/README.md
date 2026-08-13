# 自定义作图风格

本目录下的每个 `.md` 文件就是一个风格插件，主 Agent 会自动发现并按需选用。

新增风格：复制一个现有文件改名，编辑 frontmatter 即可，无需改代码、无需重启
（下一次 `list_styles` 即可被发现）。文件格式：

```markdown
---
name: 风格名（唯一标识，agent 用它引用）
description: 一句话描述 + 适用场景（agent 据此决定是否选用，写清楚触发词很重要）
background: "#1e1e1e"        # 画布背景色（mmdc -b），可省略（省略时用 RENDER_BACKGROUND）
init: "%%{init: {'theme': 'dark'}}%%"   # 注入代码开头的 Mermaid init 指令，可省略
---

正文（可选）：写给生成模型的补充风格说明，选中该风格时会并入需求描述。
```

特殊规则：`default.md` 始终生效——用户未显式选择风格时会自动注入 default 插件。
**想定制全局默认风格，直接编辑 `default.md` 即可**（背景、主题、给模型的风格说明）。
没有 frontmatter（`---` 头）的 .md 文件会被忽略（如本文件）。
