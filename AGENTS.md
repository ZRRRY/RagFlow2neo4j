<!-- 本文档供 AI 编程助手阅读，用于快速了解本项目的结构、技术栈与开发约定。 -->

# RagFlow2neo4j 项目说明

---

## 项目概览

本项目是一个轻量级 Python 脚本工具，用于将 [RAGFlow](https://github.com/infiniflow/ragflow) 知识库中的知识图谱数据导出为 CSV 文件（节点表与边表），并进一步批量导入到 Neo4j 图数据库中。

核心流程：
1. 调用 RAGFlow 开放 API，获取指定知识库的知识图谱 JSON 数据。
2. 使用 NetworkX 将 JSON 数据构建为图对象。
3. 递归清洗节点、边以及图级别的属性，将非标量值（列表、字典等）序列化为 JSON 字符串，并将 `None` / `NaN` / `Infinity` 替换为空字符串，同时对可能触发 CSV 公式注入的字符串前缀单引号进行转义。
4. 通过 pandas 导出为两个 CSV 文件：`{OUTPUT_PREFIX}_{KB_ID}_nodes.csv` 与 `{OUTPUT_PREFIX}_{KB_ID}_edges.csv`（默认存放在 `OUTPUT_DIR` 目录下）。
5. （可选）通过 `neo4j` Python 驱动将 CSV 中的节点和边批量写入 Neo4j，支持按关系类型分组和每批 1000 条的事务控制。

---

## 技术栈

- **语言**：Python 3
- **主要依赖**：
  - `requests>=2.28.0` —— HTTP 请求
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
├── config.json            # 配置文件：RAGFlow、Neo4j、CSV 输出目录及前缀等常量（gitignored）
├── config.example.json    # 配置示例模板
├── config.py              # 配置加载器：读取 config.json 并暴露为模块变量
├── cli.py                 # 交互式 CLI 入口（菜单驱动）
├── exporter.py            # RAGFlow API 请求、图转换、CSV 导出
├── neo4j_importer.py      # Neo4j 批量写入封装（节点 + 边）
├── requirements.txt       # 依赖清单
├── start.bat              # Windows 一键启动脚本（检测 venv、检查依赖、启动 CLI）
├── tests/
│   ├── __init__.py
│   ├── test_exporter.py   # exporter 模块的单元测试
│   └── test_neo4j_importer.py  # neo4j_importer 模块的单元测试
├── .gitignore             # 忽略规则（含 CSV 输出、venv、IDE 配置、本地配置等）
└── AGENTS.md              # 本文件
```

---

## 配置与运行方式

### 1. 配置

脚本通过 `config.py` 读取同级目录下的 `config.json`，并暴露为模块变量。各模块仍通过 `import config` 访问以下变量：

| 变量名             | 对应 JSON 路径                              | 说明                                      |
|--------------------|---------------------------------------------|-------------------------------------------|
| `RAGFLOW_API_KEY`  | `ragflow.api_key`                           | RAGFlow 的 API 访问密钥                   |
| `KB_ID`            | `ragflow.kb_id`                             | 目标知识库（Dataset）的 ID                |
| `RAGFLOW_BASE_URL` | `ragflow.base_url`                          | RAGFlow 服务的基础 URL                    |
| `OUTPUT_DIR`       | `output.dir`                                | CSV 输出文件夹（默认 `"output"`）         |
| `OUTPUT_PREFIX`    | `output.prefix`                             | 导出 CSV 文件的文件名前缀（默认 `"output"`） |
| `NEO4J_URI`        | `neo4j.uri`                                 | Neo4j Bolt 连接地址                       |
| `NEO4J_USER`       | `neo4j.user`                                | Neo4j 用户名                              |
| `NEO4J_PASSWORD`   | `neo4j.password`                            | Neo4j 密码                                |
| `NEO4J_DATABASE`   | `neo4j.database`                            | 数据库名称（Neo4j 4.x+ 支持多数据库）     |

首次使用时，复制 `config.example.json` 为 `config.json` 并填写实际值。`config.py` 在导入时会执行非空校验：若 `ragflow.api_key`、`ragflow.kb_id`、`neo4j.password` 为空，会直接抛出 `ValueError`。

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
- **1**：仅从 RagFlow 导出 CSV
- **2**：仅从 CSV 导入 Neo4j（支持自定义路径、连接测试、是否清空数据库）
- **3**：自动执行导出 + 导入 Neo4j（保留 CSV）
- **4**：退出

模块也支持作为库直接导入使用：

```python
from exporter import fetch_knowledge_graph, export_graph
from neo4j_importer import Neo4jWriter
```

---

## 代码组织与模块划分

### `exporter.py`
- `_escape_csv_injection(value)` —— 对以 `=`, `+`, `-`, `@`, 制表符、回车或换行开头的字符串前缀单引号，防止 Excel/LibreOffice 公式注入。
- `sanitize_attrs(G)` —— 递归处理图中所有节点、边及图级别的属性，确保 CSV 兼容性。
- `fetch_knowledge_graph()` —— HTTP GET 请求 RAGFlow API（`/api/v1/datasets/{KB_ID}/graph/export`），返回解析后的 `data` 字典。
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
- `action_export_only()` / `action_import_only()` / `action_auto()` —— 对应菜单选项的具体业务逻辑。
- `_default_csv_paths()` —— 根据 `config.OUTPUT_DIR` 和 `config.OUTPUT_PREFIX` 生成默认 CSV 文件路径。

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
  - `TestFetchKnowledgeGraph`：使用 `mock.patch("exporter.requests.get")` 覆盖成功、HTTP 错误、API 错误码、JSON 解析异常、网络异常。
  - `TestExportGraph`：覆盖空图导出、带数据图导出、异常数据类型、缺少 `graph` 字段。
- `tests/test_neo4j_importer.py`
  - `TestSanitizeRelType`：验证合法/非法关系类型名称的回退行为。
  - `TestNeo4jWriter`：使用 `mock.patch("neo4j_importer.GraphDatabase.driver")` 覆盖初始化、连接测试成功/失败、清空数据库、节点导入 Cypher 断言、边按关系类型分组导入。

**Mock 策略**：由于 `config.py` 包含硬编码校验，测试文件在顶部通过 `sys.modules["config"] = mock.MagicMock(...)` 提前注入 mock 配置，避免导入时失败。

---

## 安全注意事项

- **API 密钥与密码硬编码风险**：敏感信息集中在 `config.json` 中，该文件已被 `.gitignore` 排除，不会被意外提交。示例配置请使用 `config.example.json`（不含真实密钥）。
  - **建议**：生产环境优先通过环境变量读取，或确保 `config.json` 仅在本地保留。
- **CSV 注入**：已在 `exporter.py` 的 `_escape_csv_injection` 中对危险前缀字符进行单引号转义，但仍需注意知识图谱中是否包含其他 Office 公式触发字符。
- **数据库清空不可逆**：`Neo4jWriter.clear_database()` 执行 `MATCH (n) DETACH DELETE n`，CLI 在执行前会要求用户输入 `y` 确认，但仍需谨慎操作。
- **无输入校验**：`KB_ID`、`RAGFLOW_BASE_URL`、`NEO4J_URI` 等配置未做格式校验，传入异常值可能导致请求失败或连接错误。

---

## 部署与构建

- **无需构建步骤**：纯 Python 脚本，安装依赖后直接运行。
- **无容器化配置**：未提供 Dockerfile 或 docker-compose。
- **依赖安装**：
  ```bash
  pip install -r requirements.txt
  ```
