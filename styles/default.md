---
name: default
description: 统一技术图表规范：清晰、克制，统一色彩、线条、箭头、色块与背景
background: "#F8F8F8"
init: "%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#F5F8FF', 'primaryTextColor': '#3E4144', 'primaryBorderColor': 'transparent', 'lineColor': '#666666', 'secondaryColor': '#D2E6FA', 'tertiaryColor': '#F5F8FF', 'clusterBkg': '#D2E6FA', 'clusterBorder': '#666666', 'edgeLabelBackground': '#F8F8F8'}}}%%"
---

# 统一技术图表规范

生成流程图、架构图和技术示意图时严格遵守本规范；未明确说明时不要自行增加颜色、边框或装饰。

## 通用视觉规则

- 画布：`#F8F8F8`。
- 主色：`#2ECBFF`；辅助色：`#D2E6FA`、`#D0F0F5`、`#B4DCE6`、`#5AC3A0`、`#F5F8FF`、`#E1E4E6`。
- 文字：浅色背景用 `#3E4144`，次要文字用 `#666666`；主色或深色背景必须用 `#FFFFFF`。
- 线条与箭头：`#666666`、约 `0.75pt`。实线表示明确关系；虚线仅表示可选、弱关联或不可见关系。
- 色块默认无边框。除本规范色板外不要使用其他颜色。
- 优先保证层级、对齐、留白和可读性，避免阴影、渐变及无意义装饰。

## Mermaid 流程图

### 形状

- 开始/结束：体育场形 `id([文字])`。
- 处理步骤：圆角矩形 `id(文字)`，禁止使用直角矩形。
- 判断：菱形 `id{文字}`。

### 配色

在 `flowchart` 声明后定义并为每个节点分配类别：

```mermaid
classDef terminal fill:#E1E4E6,color:#3E4144,stroke-width:0
classDef required fill:#2ECBFF,color:#FFFFFF,stroke-width:0
classDef optional fill:#D2E6FA,color:#3E4144,stroke-width:0
classDef optional2 fill:#D0F0F5,color:#3E4144,stroke-width:0
```

- `terminal`：开始与结束。
- `required`：从开始到结束的必经步骤及判断节点。
- `optional` / `optional2`：判断引出的不同分支或可跳过步骤；分支不得使用主色。
- 分支汇合后的必经步骤恢复 `required`。
- 连线沿用 `#666666`，分支标注“是/否”等短标签；不要额外写 `linkStyle`。

## Mermaid 架构图

- 组件使用直角矩形 `id[文字]`；用 `subgraph` 表示层或分组，不使用流程图的判断、起止与分支语义。
- 每个组件必须分配以下类别之一；同层组件同色，相邻层换色：

```mermaid
classDef arc1 fill:#F5F8FF,color:#3E4144,stroke-width:0
classDef arc2 fill:#D0F0F5,color:#3E4144,stroke-width:0
classDef arc3 fill:#D2E6FA,color:#3E4144,stroke-width:0
classDef arc4 fill:#E1E4E6,color:#3E4144,stroke-width:0
classDef arcCore fill:#2ECBFF,color:#FFFFFF,stroke-width:0
```

- 最外层容器可用 `#B4DCE6` 或 `#5AC3A0` 交替填充，并保留 `#000000` 边框；嵌套子容器用浅色且无边框。
- 子容器与内部组件必须使用明显不同的填充色，避免融为一体。
- 仅核心组件可用 `arcCore`；其他组件使用浅色。
- 整体使用 `flowchart TD` 纵向分层；每层内部使用 `direction LR`，并用 `~~~` 串联组件保持横排。单层超过 4 个组件时拆成多行。
- 默认不画层间箭头，用 `L1 ~~~ L2` 保持层序；仅当需求明确表达调用或数据流时使用可见箭头。

## [drawio:flowchart] draw.io 流程图规则

- 开始/结束：`rounded=1;arcSize=50`；处理步骤：`rounded=1`；判断：`rhombus`。
- 所有节点 `strokeColor=none`。
- 开始/结束：`fillColor=#E1E4E6;fontColor=#3E4144`。
- 必经步骤和判断：`fillColor=#2ECBFF;fontColor=#FFFFFF`。
- 不同分支：分别使用 `#D2E6FA`、`#D0F0F5`，文字 `#3E4144`；汇合后恢复主色。
- 连线采用 `#666666` 的直角正交实线和实心箭头；判断分支必须带简短标签。

## [drawio:architecture] draw.io 架构图规则

- 层容器使用纯色矩形，不使用 swimlane 标题栏；层名置顶居中。
- 相邻层在 `#B4DCE6`（深灰字）与 `#5AC3A0`（白字）间交替；仅最外层容器使用 `strokeColor=#000000`。
- 子容器最多一层，使用 `#E1E4E6`、深灰字、`strokeColor=none`。
- 同层组件必须同色，相邻层换色；从 `#F5F8FF`、`#D0F0F5`、`#D2E6FA`、`#E1E4E6` 中选择，统一深灰字且无边框。
- 组件不得使用主色或其他强调色；子容器与内部组件不得同色。
- 默认不画连线或箭头，仅靠容器堆叠表达层级；需求明确存在调用或数据流时才连线。
