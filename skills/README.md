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
layout: node_width=172,node_height=28,gap_y=28   # 可选：drawio 流程图布局参数
prompt_hint: 节点文字一律使用中文                  # 可选：直通生成子模型的作图要求
kind: check                                      # 可选：声明为检查标准 Skill
---

正文：写给主 Agent 的操作手册。use_skill 被调用时，这段内容会原样注入对话，
agent 会遵照执行——所以可以引用现有工具（read_document / find_files /
create_diagram / modify_diagram / get_current_diagram 等）编排多步流程。
```

frontmatter 的可选 `layout` 字段：声明 drawio 流程图的框尺寸/间距
（`node_width`/`node_height`/`gap_x`/`gap_y`，逗号分隔，单位 px）。
agent 读取该技能（use_skill）的那一刻就确定性生效，后续
create_diagram / modify_diagram 自动沿用——比让 agent 记着传参可靠得多。
`node_height` 是最小高度，文字换行多时会自动加高。

frontmatter 的可选 `prompt_hint` 字段：一行作图要求（语言、内容规范等），
use_skill 时存进会话，create/modify 时自动注入需求文本**直通生成子模型**。
技能正文只有主 Agent 可见，子模型只认 requirement——语言这类必须落到
子模型的规则请写在这里，不要只写在正文里。

检查标准使用 `kind: check`。检查路由把当前 Session 中此类 Skill 的完整正文交给文件
子 Agent；没有匹配 Skill 时会拒绝执行并要求用户提供审查标准文档，不会回退到代码
内置规则。Core 只给子 Agent 提供通用 `image_reasoning(prompt, image_paths)`：它不会
内置检查项、分类、PASS/FAIL 判定或报告格式。一个 Skill 应先用 `## execution` 描述
如何选择检查项、何时读取文档、如何调用视觉工具和怎样汇总，再定义检查项，例如：

```markdown
## execution

每张图片与每个适用检查项分别调用 image_reasoning；把本检查项完整规则、必要文档内容
和严格输出格式都放入 prompt，最后用 write_file 写入 CSV。

## check: unique_item_id | 检查项显示名

applies_to: 流程图, 界面截图

写给检查模型的具体标准，可用 {document} 插入用户文档全文。
```

`applies_to: *` 表示适用于任何图片。Skill 可以要求视觉模型判断适用性，并把
`PASS` / `FAIL` / `NA` 等结果协议写在正文中；Core 不解释这些领域语义。批量检查还可
通过 `## batch` 约定目录扫描和 `batch_plan.json` 格式：短生命周期子 Agent 只生成
清单并退出，随后每个案例独立执行，避免上下文跨案例累计。

注意事项：

- 本系统只承载"指引型"技能：正文是给模型看的操作说明，不包含可执行脚本。
  需要跑代码的能力请做成内置工具（`flowchart_agent/skills/builtin.py`）。
- 没有 frontmatter（`---` 头）的 .md 文件会被忽略（如本文件）。
- 目录可用环境变量 `FLOWCHART_SKILL_DIR` 覆盖，默认 `./skills`。
