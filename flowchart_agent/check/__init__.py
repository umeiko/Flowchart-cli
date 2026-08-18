"""检查管线：两级分类路由的 check 分支（文档/图片检视）。

结构：CheckAgent（编排）→ ItemCheckAgent（逐项检查子 Agent）；
检查项清单见 items.py（渐进式披露：路由到 check 后才向模型/用户展示）。
"""

from .agent import CheckAgent
from .item_agent import ItemCheckAgent
from .items import CHECK_ITEMS, items_overview, resolve_items

__all__ = [
    "CheckAgent",
    "ItemCheckAgent",
    "CHECK_ITEMS",
    "items_overview",
    "resolve_items",
]
