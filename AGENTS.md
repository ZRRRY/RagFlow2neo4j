<!-- 本文档供 AI 编程助手阅读，用于快速了解本项目的结构、技术栈与开发约定。 -->

# RagFlow2neo4j 项目说明

---

## 项目概览

本项目是一个轻量级 Python 脚本工具，用于将 [RAGFlow](https://github.com/infiniflow/ragflow) 知识库中的知识图谱数据导出为 CSV 文件（节点表与边表），并进一步批量导入到 Neo4j 图数据库中。

核心流程：
1. 调用 RAGFlow 开放 API，获取指定知识库的知识图谱 JSON 数据。
2. 当 RAGFlow API 因数据量过大触发内部错误时，支持绕过 API，直接从底层 OpenSearch 读取完整的图谱数据。
   - 方式 A（内存建图）：`fetch_knowledge_graph_direct()` 一次性读取实体/关系文档并构建 NetworkX 图对象，再导出 CSV。
   - 方式 B（流式导出）：`export_graph_direct()` 先通过 `_count` 统计总数，再以 scroll 批次为单位边读边写 CSV，内存占用更低，适合超大数据量。
3. 使用 NetworkX 将 JSON 数据构建为图对象（方式 A / API 导出时）。
4. 递归清洗节点、边以及图级别的属性，将非标量值（列表、字典等）序列化为 JSON 字符串，并将 `None` / `NaN` / `Infinity` 替换为空字符串，同时对可能触发 CSV 公式注入的字符串前缀单引号进行转义。
5. 通过 pandas 导出为两个 CSV 文件：`{OUTPUT_PREFIX}_{KB_ID}_nodes.csv` 与 `{OUTPUT_PREFIX}_{KB_ID}_edges.csv`（默认存放在 `OUTPUT_DIR` 目录下）。
6. （可选）通过 `neo4j` Python 驱动将 CSV 中的节点和边批量写入 Neo4j，支持按关系类型分组和每批 1000 条的事务控制。

---

## 技术栈

- **语言**：Python 3
- **主要依赖**：
  - `requests>=2.28.0` —— HTTP 请求（RAGFlow API、OpenSearch 直连）
  - `networkx>=3.0` —— 图数据结构的构建与转换
  - `pandas>=1.5.0` —— CSV 读写
  - `neo4j>=5.0` —— Neo4j Bolt 驱动
  - `pytest>=7.0.0` —— 测试框架
  - `json`, `math`, `logging`, `os`, `sys`, `re`, `collections`, `pathlib`, `unittest.mock` —— 标准库
- **构建工具**：无。项目未配置 `pyproject.toml`、`setup.py`、`Pipfile` 或 `poetry.lock`，仅提供 `requirements.txt`。
- **容器化**：未提供 Dockerfile 或 docker-compose。
- **启动脚本**：提供 `start.bat`（Windows），用于自动检测并激活虚拟环境、检查依赖并启动 CLI。

---

## 项目结构

```
RagFlow2neo4j/
├── config.py              # 配置文件：Python 格式，支持多 profile 切换（gitignored）
├── config.example.py      # 配置示例模板
├── cli.py                 # 交互式 CLI 入口（菜单驱动）
├── exporter.py            # RAGFlow API 请求、OpenSearch 直连、图转换、CSV 导出
├── neo4j_importer.py      # Neo4j 批量写入封装（节点 + 边）
├── requirements.txt       # 依赖清单
├── start.bat              # Windows 一键启动脚本（检测 venv、检查依赖、启动 CLI）
├── tests/                 # 单元测试
│   ├── __init__.py
│   ├── test_exporter.py   # exporter 模块的单元测试
│   └── test_neo4j_importer.py  # neo4j_importer 模块的单元测试
├── docs/                  # 文档
│   └── ragflow_api_data_format.md  # RAGFlow 接口返回数据格式详细说明
├── .gitignore             # 忽略规则（含 CSV 输出、venv、IDE 配置、本地配置等）
└── AGENTS.md              # 本文件
```

---

## 配置与运行方式

### 1. 配置

`config.py` 为纯 Python 配置文件，内部定义了 `_PROFILES` 字典（支持 `"local"`、`"remote"` 等任意键名），并通过 `_ACTIVE_PROFILE` 变量指定当前激活的配置集。各模块通过 `import config` 访问以下变量：

| 变量名 | 说明 |
|--------|------|
| `RAGFLOW_API_KEY` | RAGFlow 的 API 访问密钥 |
| `KB_ID` | 目标知识库（Dataset）的 ID |
| `RAGFLOW_BASE_URL` | RAGFlow 服务的基础 URL |
| `RAGFLOW_REQUEST_TIMEOUT` | 请求 RAGFlow API 的超时时间（秒），默认 120 |
| `OUTPUT_DIR` | CSV 输出文件夹（默认 `"output"`） |
| `OUTPUT_PREFIX` | 导出 CSV 文件的文件名前缀（默认 `"output"`） |
| `NEO4J_URI` | Neo4j Bolt 连接地址 |
| `NEO4J_USER` | Neo4j 用户名 |
| `NEO4J_PASSWORD` | Neo4j 密码 |
| `NEO4J_DATABASE` | 数据库名称（Neo4j 4.x+ 支持多数据库） |
| `OPENSEARCH_HOST` | OpenSearch 主机地址（直连绕过 API 时使用，默认 `localhost`） |
| `OPENSEARCH_PORT` | OpenSearch 端口（默认 `9201`） |
| `OPENSEARCH_USER` | OpenSearch 用户名（默认 `admin`） |
| `OPENSEARCH_PASSWORD` | OpenSearch 密码（默认空字符串） |
| `OPENSEARCH_USE_SSL` | 是否对 OpenSearch 使用 HTTPS（默认 `False`） |

首次使用时，复制 `config.example.py` 为 `config.py` 并填写实际值。修改 `_ACTIVE_PROFILE` 的值即可切换配置集（例如 `"local"` ↔ `"remote"`）。`config.py` 在导入时会执行非空校验：若当前 profile 的 `ragflow.api_key`、`ragflow.kb_id`、`neo4j.password` 为空，会直接抛出 `ValueError`。

### 2. 运行

安装依赖后，直接执行交互式 CLI：

```bash
pip install -r requirements.txt
python cli.py
```

或在 Windows 环境下双击运行 `start.bat`，该脚本会自动：
1. 检测并激活 `venv` 或 `.venv` 虚拟环境；
2. 检查 Python 可用性；
3. 检查核心依赖是否已安装，缺失时自动执行 `pip install -r requirements.txt`；
4. 启动 `python cli.py`。

CLI 菜单选项：
- **1**：从 RagFlow API 导出 CSV
- **2**：从 OpenSearch 直连导出 CSV（绕过 API，适合大数据量）
- **3**：仅从 CSV 导入 Neo4j（支持自定义路径、连接测试、是否清空数据库）
- **4**：自动执行导出 + 导入 Neo4j（保留 CSV，导出时可选择 API 或 OpenSearch 方式）
- **5**：退出

模块也支持作为库直接导入使用：

```python
from exporter import fetch_knowledge_graph, fetch_knowledge_graph_direct, export_graph, export_graph_direct
from neo4j_importer import Neo4jWriter
```

---

## 代码组织与模块划分

### `exporter.py`
- `_escape_csv_injection(value)` —— 对以 `=`、`+`、`-`、`@`、制表符、回车或换行开头的字符串前缀单引号，防止 Excel/LibreOffice 公式注入。
- `sanitize_attrs(G)` —— 递归处理图中所有节点、边及图级别的属性，确保 CSV 兼容性（非标量值 → JSON 字符串；`None` / `NaN` / `Infinity` → 空字符串）。
- `_safe_serialize(value)` —— 将非标量值序列化为 JSON 字符串，处理 `None` / `NaN` / `Infinity`，供 `sanitize_attrs` 与流式导出共用。
- `_escape_csv_injection(value)` —— 对以 `=`、`+`、`-`、`@`、制表符、回车或换行开头的字符串前缀单引号，防止 Excel/LibreOffice 公式注入。
- `sanitize_attrs(G)` —— 递归处理图中所有节点、边及图级别的属性，确保 CSV 兼容性（非标量值 → JSON 字符串；`None` / `NaN` / `Infinity` → 空字符串）。
- `_get_session()` —— 创建带重试机制的 `requests.Session`，对 500/502/503/504 的 GET 请求最多重试 3 次，退避因子为 1。
- `_get_tenant_id()` —— 通过 RAGFlow Dataset API 获取 `tenant_id`，供 OpenSearch 直连使用。
- `fetch_knowledge_graph()` —— HTTP GET 请求 RAGFlow API（`/api/v1/datasets/{KB_ID}/graph/export`），返回解析后的 `data` 字典。
- `_count_os_docs(count_url, query, auth)` —— 使用 OpenSearch `_count` API 获取符合条件的文档总数。
- `_scroll_search_batches(search_url, query, auth, scheme, os_host, os_port, batch_size)` —— 生成器：使用 OpenSearch scroll API 逐批次 yield hits 列表，自动处理分页并清理 scroll 上下文。
- `fetch_knowledge_graph_direct()` —— 绕过 RAGFlow API，直接从底层 OpenSearch 读取实体和关系文档构建完整图谱。实现步骤：
  1. 通过 RAGFlow Dataset API 获取 `tenant_id`；
  2. 构造 OpenSearch 索引名 `ragflow_{tenant_id}`，使用 scroll API 分别查询 `knowledge_graph_kwd="entity"` 的实体文档和 `knowledge_graph_kwd="relation"` 的关系文档；
  3. 解析实体文档构建节点（以 `entity_kwd` 为节点 ID），解析关系文档构建边（以 `from_entity_kwd` / `to_entity_kwd` 为起止节点），组装为 NetworkX 图对象。
  返回格式与 `fetch_knowledge_graph()` 保持一致：`{"graph": <dict>}`。
- `export_graph_direct(kb_id, output_dir, output_prefix, batch_size)` —— 流式导出：先 `_count` 统计实体/关系总数，再分两个阶段 scroll 拉取并追加写入 CSV。每批次打印 `当前/总数` 分数进度日志；若关系引用缺失节点，自动补录到 nodes.csv。
- `export_graph(data)` —— 将 API 数据转为 NetworkX 图对象，清洗属性后导出为两个 CSV 文件。

### `neo4j_importer.py`
- `_sanitize_rel_type(name)` —— 校验关系类型名称是否符合 Cypher 标识符规则（`^[A-Za-z_][A-Za-z0-9_]*$`），不合法时回退为 `RELATED_TO`。
- `Neo4jWriter` —— 封装 Neo4j 批量写入操作的类：
  - `__init__` 支持显式传参或回退到 `config` 默认值。
  - 支持上下文管理器（`with Neo4jWriter() as writer:`）。
  - `test_connection()` —— 测试连接可用性。
  - `clear_database()` —— 执行 `MATCH (n) DETACH DELETE n` 清空数据库（不可逆）。
  - `import_nodes(csv_path)` —— 从节点 CSV 批量 `MERGE` 到 `:Entity` 标签节点，按 `id` 去重，每批 1000 条。
  - `import_edges(csv_path)` —— 从边 CSV 按 `relation` 列分组，批量 `MERGE` 关系，每批 1000 条。

### `cli.py`
- `menu()` / `main()` —— 交互式命令循环，捕获 `KeyboardInterrupt` 和未预期异常。
- `action_export_api()` —— 调用 `fetch_knowledge_graph()` 并导出 CSV。
- `action_export_direct()` —— 调用 `export_graph_direct()` 进行流式导出 CSV。
- `action_import_only()` —— 从自定义 CSV 路径导入 Neo4j。
- `action_auto()` —— 自动执行导出 + 导入，支持选择 API 或 OpenSearch 导出方式。
- `_default_csv_paths()` —— 根据 `config.OUTPUT_DIR` 和 `config.OUTPUT_PREFIX` 生成默认 CSV 文件路径。
- `_run_export_import_api(data_fetcher, export_label)` —— API 导出 + 导入的统一执行逻辑。
- `_run_export_import_direct()` —— OpenSearch 流式导出 + 导入的统一执行逻辑。

---

## 开发约定与代码风格

- **注释语言**：代码中的注释和 docstring 使用中文。
- **函数文档**：使用三引号 docstring 简要说明函数用途。
- **字符串格式化**：混用 f-string 与 `%` 格式化（日志中偏好 `%` 以避免提前计算）。
- **日志**：使用标准库 `logging` 模块，格式为 `%(asctime)s [%(levelname)s] %(message)s`。业务逻辑和错误使用日志记录；CLI 菜单和用户交互输入提示仍使用 `print()`。
- **编码**：CSV 导出与读取均使用 `utf-8-sig`，以兼容 Excel 等工具。
- **无类型注解**：当前未使用 Python 类型提示；若后续扩展，建议遵循 PEP 484 逐步补充。
- **无代码格式化配置**：未提供 `.editorconfig`、`.pre-commit-config.yaml`、`black.toml` 等；建议保持与现有代码一致的简洁风格。
- **延迟导入**：`neo4j_importer.py` 中的 `import pandas as pd` 放在方法内部执行，避免在仅导入模块时产生不必要的依赖加载。

---

## 测试说明

项目已引入 `pytest` 并包含 `tests/` 目录，使用 `unittest.mock` 进行依赖隔离。

### 运行测试

```bash
pytest tests/
```

### 测试覆盖

- `tests/test_exporter.py`
  - `TestSanitizeAttrs`：验证 `None`、`NaN`、`Infinity`、标量类型、列表/字典 JSON 序列化、边属性清洗、CSV 注入转义。
  - `TestFetchKnowledgeGraph`：使用 `mock.patch("exporter._get_session")` 覆盖成功、HTTP 错误、API 错误码、JSON 解析异常、网络异常。
  - `TestFetchKnowledgeGraphDirect`：覆盖 OpenSearch 实体/关系文档读取成功、Dataset API HTTP 错误、Dataset API 错误码、OpenSearch 空结果、跳过无效记录、scroll 分页拉取逻辑等场景。
  - `TestCountOsDocs`：覆盖 OpenSearch `_count` 成功与异常场景。
  - `TestExportGraphDirect`：覆盖流式导出成功、计数失败、空结果、缺失节点自动补录等场景。
  - `TestExportGraph`：覆盖空图导出、带数据图导出、异常数据类型、缺少 `graph` 字段。
- `tests/test_neo4j_importer.py`
  - `TestSanitizeRelType`：验证合法/非法关系类型名称的回退行为。
  - `TestNeo4jWriter`：使用 `mock.patch("neo4j_importer.GraphDatabase.driver")` 覆盖初始化、连接测试成功/失败、清空数据库、节点导入 Cypher 断言、边按关系类型分组导入。

**Mock 策略**：由于 `config.py` 包含硬编码校验，测试文件在顶部通过 `sys.modules["config"] = mock.MagicMock(...)` 提前注入 mock 配置，避免导入时失败。

---

## 安全注意事项

- **API 密钥与密码硬编码风险**：敏感信息集中在 `config.py` 中，该文件已被 `.gitignore` 排除，不会被意外提交。示例配置请使用 `config.example.py`（不含真实密钥）。
  - **建议**：生产环境优先通过环境变量读取，或确保 `config.py` 仅在本地保留。
- **CSV 注入**：已在 `exporter.py` 的 `_escape_csv_injection` 中对危险前缀字符进行单引号转义，但仍需注意知识图谱中是否包含其他 Office 公式触发字符。
- **数据库清空不可逆**：`Neo4jWriter.clear_database()` 执行 `MATCH (n) DETACH DELETE n`，CLI 在执行前会要求用户输入 `y` 确认，但仍需谨慎操作。
- **无输入校验**：`KB_ID`、`RAGFLOW_BASE_URL`、`NEO4J_URI` 等配置未做格式校验，传入异常值可能导致请求失败或连接错误。
- **OpenSearch 直连暴露凭据**：`fetch_knowledge_graph_direct()` 通过 `requests.post` 直接访问 OpenSearch，若配置文件中保存了明文密码，请确保该文件不会被提交或泄露。

---

## 部署与构建

- **无需构建步骤**：纯 Python 脚本，安装依赖后直接运行。
- **无容器化配置**：未提供 Dockerfile 或 docker-compose。
- **依赖安装**：
  ```bash
  pip install -r requirements.txt
  ```
