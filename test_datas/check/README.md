# 检查（check）测试用例

每个子目录对应一类检查 Agent，含文档 + 图片素材与**预埋问题清单**。
用 chat 模式直接说，例如：

```
检查一下 test_datas/check/network/network.txt 里的组网图有没有问题，图是 test_datas/check/network/topology.png
```

## flowchart/ 流程图检查

- `1.txt` + `1.jpg`：**应通过**（PASS）的干净用例（登录流程）。
- `2.txt` + `2.jpg`：预埋问题——缺开始节点、判断节点未用菱形、
  Loop/Count 循环分支流向与文档描述不一致。

## network/ 组网图检查

- `network.txt` + `topology.png`（由 topology.mmd 渲染）。预埋问题：
  - 图与描述不一致：文档规定数据库网段 192.168.30.0/24（VLAN 30），图中 DB01 标的是 192.168.20.11；
  - 图自身规划错误：办公终端静态地址 192.168.10.11 与 Web01 冲突，
    且文档明确"办公终端不分配静态 IP"。

## schematic/ 原理图检查

- `schematic.txt` + `schematic.png`（由 schematic.mmd 渲染）。预埋问题：
  - 组件遗漏：文档五部分组成含"电源模块"，图中没有；
  - 连接关系不一致：文档为"MCU 驱动散热风扇"，图中风扇由温度传感器直连。

## ui/ 界面截图检查

- `ui.txt` + `settings.png`（合成的假界面）。预埋问题：
  - 敏感信息：表单与状态栏含公网 IP（47.98.123.45）与真实账号（admin_zhangsan）；
  - 界面词不一致：文档写点击【保存设置】，截图按钮是【保存配置】。
