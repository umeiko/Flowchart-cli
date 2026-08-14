# 自定义技能包（Skill Packs）

本目录下的每个 `.md` 文件就是一个"提示词型"技能包，主 Agent 会自动发现并按需使用。
适合接入社区流传的 SKILL.md 式技能，或沉淀自己的操作指引（导出格式、行业规范、
第三方工具对接步骤等）。

新增技能包：丢一个带 frontmatter 的 `.md` 文件进来即可，无需改代码、无需重启
（下一次 `list_skill_packs` 即可被发现）。文件格式：

```markdown
---
name: skill-name        # 唯一标识（小写，agent 用它引用）
description: 一句话描述 + 触发场景（agent 据此决定是否选用，写清楚触发词很重要）
---

正文：写给主 Agent 的操作手册。use_skill 被调用时，这段内容会原样注入对话，
agent 会遵照执行——所以可以引用现有工具（read_document / find_files /
create_diagram / modify_diagram / get_current_diagram 等）编排多步流程。
```

注意事项：

- 本系统只承载"指引型"技能：正文是给模型看的操作说明，不包含可执行脚本。
  需要跑代码的能力请做成内置工具（`flowchart_agent/skills/builtin.py`）。
- 没有 frontmatter（`---` 头）的 .md 文件会被忽略（如本文件）。
- 目录可用环境变量 `FLOWCHART_SKILL_DIR` 覆盖，默认 `./skills`。
