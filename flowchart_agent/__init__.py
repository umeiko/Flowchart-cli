"""Flowchart AI Agent：自然语言文档 → Mermaid 图表（生成-渲染-验证循环）。"""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("flowchart-ai")
except Exception:  # 未以包形式安装（直接源码运行）时的回退
    __version__ = "1.1.0"
