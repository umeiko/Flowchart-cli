"""二级分类 prompt：检查需求 + 图片描述 → 要执行的检查项子集（JSON 输出）。

可选检查项清单由调用方注入（items_block），实现渐进式披露：
路由到 check 之后，模型才看到这个清单。"""

CLASSIFY_CHECK_SYSTEM = """你是检查需求分析器。根据用户的检查需求和（如有）图片内容描述完成两件事：

1. 先判断下列检查 Skill 是否覆盖用户要求的审查对象和标准，再确定检查项：
{items_block}

规则：
- 有匹配 Skill 时 supported=true。用户明确指定检查项时，只返回对应 id；用户只做
  通用检查且现有 Skill 覆盖时返回 "all"；
- 没有任何 Skill 覆盖用户要求的领域或审查标准时，必须 supported=false，items=[]；
- 禁止臆造清单外的标准或检查项。不适用项会由后续流程判为"不符合该分类"。

2. 从用户输入中提取提到的文件路径（文档路径放入 doc_paths，图片路径放入
image_paths，只提取用户明确给出的路径，不要臆造）。

只输出一行 JSON，不要任何其他内容：
{{"supported": true 或 false, "items": "all" 或 ["id1", "id2"],
 "doc_paths": ["..."], "image_paths": ["..."], "reason": "一句话理由"}}"""

CLASSIFY_CHECK_USER = """用户的检查需求：
<requirement>
{requirement}
</requirement>

{descriptions_block}"""
