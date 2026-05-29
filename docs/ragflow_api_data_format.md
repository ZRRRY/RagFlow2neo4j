# RAGFlow 知识图谱导出接口数据格式说明

本文档描述 RAGFlow `/api/v1/datasets/{KB_ID}/graph/export` 接口返回的 JSON 数据结构，以及本工具（RagFlow2neo4j）如何处理该数据。

---

## 1. 接口概述

| 项目 | 说明 |
|------|------|
| **请求方法** | `GET` |
| **请求路径** | `/api/v1/datasets/{KB_ID}/graph/export` |
| **认证方式** | `Authorization: Bearer {RAGFLOW_API_KEY}` |
| **Content-Type** | `application/json` |

---

## 2. 响应结构（外层）

接口返回标准的 JSON 响应体，外层结构如下：

```json
{
  "code": 0,
  "data": {
    "graph": { ... }
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | `integer` | 业务状态码，`0` 表示成功，非零值表示各类错误 |
| `data` | `object` | 实际业务数据，仅当 `code == 0` 时存在 |

### 2.1 异常响应示例

当认证失败或参数错误时：

```json
{
  "code": -1,
  "message": "auth failed"
}
```

> 工具会在 `fetch_knowledge_graph()` 中检查 `code`，非零时记录错误日志并返回 `None`。

---

## 3. `data.graph` 结构（核心）

`data.graph` 采用 **NetworkX node-link 数据格式**，可直接被 `networkx.node_link_graph()` 解析为图对象。

### 3.1 顶层字段

```json
{
  "directed": false,
  "multigraph": false,
  "graph": {},
  "nodes": [ ... ],
  "edges": [ ... ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `directed` | `boolean` | 是 | 是否为有向图。影响 NetworkX 建图时使用的图类（`Graph` vs `DiGraph`） |
| `multigraph` | `boolean` | 是 | 是否支持同一对节点间的多条边（多重图） |
| `graph` | `object` | 是 | 图级别的全局属性字典，通常为空 `{}`，也可存放图谱元数据 |
| `nodes` | `array` | 是 | 节点列表，每个元素为一个节点对象 |
| `edges` | `array` | 是 | 边列表，每个元素为一条边对象 |

### 3.2 节点（`nodes`）

每个节点是一个字典，**`id` 为唯一必填字段**，其余字段为节点的业务属性。

```json
{
  "id": "n1",
  "label": "Person",
  "name": "张三",
  "age": 30
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `string` | 节点唯一标识，整图内不可重复 |
| `label` | `string` | 节点类型标签（如 `Person`、`Company`、`Entity`） |
| `name` | `string` | 节点显示名称 |
| *其他字段* | `any` | 由 RAGFlow 知识图谱抽取得到的任意属性，如 `age`、`description`、列表、字典等 |

#### 节点属性值类型说明

| 原始类型 | 导出 CSV 前的处理方式 |
|----------|----------------------|
| `string` | 原样保留，对危险前缀（`=`, `+`, `-`, `@` 等）加单引号转义 |
| `integer` / `float` | 原样保留，但 `NaN` / `Infinity` / `-Infinity` 转为空字符串 `""` |
| `boolean` | 原样保留（`True` / `False`） |
| `null` | 转为空字符串 `""` |
| `array` / `object` | 通过 `json.dumps()` 序列化为 JSON 字符串 |
| 其他 | 转为 `str()` |

### 3.3 边（`edges`）

每条边是一个字典，**`source` 和 `target` 为必填字段**，分别对应两端节点的 `id`。

```json
{
  "source": "n1",
  "target": "n2",
  "relation": "works_for",
  "weight": 1.0
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | `string` | 起始节点的 `id` |
| `target` | `string` | 目标节点的 `id` |
| `relation` | `string` | 关系类型名称（如 `works_for`、`located_in`）。导入 Neo4j 时会根据此字段分组，每种关系类型执行一次批量导入 |
| *其他字段* | `any` | 边的附加属性，处理方式与节点属性相同 |

#### 关于 `relation` 字段的约束

在导入 Neo4j 时，关系类型会被 `_sanitize_rel_type()` 校验：

- 合法格式：以字母或下划线开头，仅包含字母、数字、下划线（正则：`^[A-Za-z_][A-Za-z0-9_]*$`）
- 若格式不合法，会自动回退为 `RELATED_TO`

---

## 4. 完整数据示例

以下是一个包含 2 个节点、1 条边的完整响应示例：

```json
{
  "code": 0,
  "data": {
    "graph": {
      "directed": false,
      "multigraph": false,
      "graph": {
        "created_by": "RAGFlow",
        "version": "1.0"
      },
      "nodes": [
        {
          "id": "n1",
          "label": "Person",
          "name": "张三",
          "age": 30,
          "aliases": ["张三", "张三三"]
        },
        {
          "id": "n2",
          "label": "Company",
          "name": "某科技公司",
          "founded": 2010
        }
      ],
      "edges": [
        {
          "source": "n1",
          "target": "n2",
          "relation": "works_for",
          "weight": 1.0,
          "since": "2020-01"
        }
      ]
    }
  }
}
```

---

## 5. 数据流与处理流程

RAGFlow 原始数据在本项目中的处理步骤如下：

```
RAGFlow API
    │
    ▼
JSON 响应（外层 code + data.graph）
    │
    ▼
fetch_knowledge_graph() ──► 提取 data.graph
    │
    ▼
networkx.node_link_graph() ──► NetworkX 图对象 G
    │
    ▼
sanitize_attrs(G) ──► 递归清洗所有节点、边、图属性
    │
    ├─ 非标量值（list / dict / tuple / set）→ JSON 字符串
    ├─ None / NaN / Infinity → 空字符串 ""
    └─ CSV 公式注入字符前缀 → 加单引号转义
    │
    ▼
pandas.DataFrame ──► 导出为 CSV
    │
    ├─ {prefix}_{KB_ID}_nodes.csv（节点表，索引列为 id）
    └─ {prefix}_{KB_ID}_edges.csv（边表，含 source / target / relation 等）
```

---

## 6. 注意事项

1. **节点 ID 唯一性**：`id` 字段在整图中必须唯一，否则 NetworkX 建图时会发生覆盖。
2. **空图处理**：`nodes` 和 `edges` 为空数组时，工具仍会正常导出两个空结构的 CSV 文件。
3. **复杂属性**：RAGFlow 可能抽取到嵌套的列表或字典属性（如 `aliases`、`metadata`），这些会被序列化为 JSON 字符串存入 CSV，导入 Neo4j 后也是以字符串形式存储。如需在 Cypher 中解析，可使用 `apoc.convert.fromJsonMap()` 或 `apoc.convert.fromJsonList()`。
4. **属性名冲突**：若节点或边属性名与 CSV 导出保留字冲突（如 pandas 的 `index`），目前由 `pandas.to_csv()` 自动处理，通常不会影响正确性。

---

## 7. 相关文件

- 数据获取与转换逻辑：`exporter.py`（`fetch_knowledge_graph()`、`export_graph()`、`sanitize_attrs()`）
- Neo4j 导入逻辑：`neo4j_importer.py`（`Neo4jWriter.import_nodes()`、`Neo4jWriter.import_edges()`）
- 单元测试：`tests/test_exporter.py`、`tests/test_neo4j_importer.py`
