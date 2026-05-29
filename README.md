# RagFlow2neo4j

轻量级 Python 工具，用于将 [RAGFlow](https://github.com/infiniflow/ragflow) 知识库中的知识图谱数据导出为 CSV（节点表 + 边表），并批量导入到 Neo4j 图数据库。

---

## 功能特性

- **一键导出**：调用 RAGFlow 开放 API，自动获取指定知识库的知识图谱 JSON 数据
- **图转换**：使用 NetworkX 构建图对象，递归清洗节点与边的属性
- **CSV 导出**：自动序列化复杂类型、处理空值、转义 CSV 公式注入，生成标准 CSV 文件
- **批量导入 Neo4j**：支持节点 `MERGE` 去重、按关系类型分组导入、每批 1000 条事务控制
- **交互式 CLI**：菜单驱动的命令行界面，支持单独导出、单独导入、自动全流程
- **安全**：配置文件使用 JSON 格式，真实配置被 `.gitignore` 隔离，避免敏感信息泄露

---

## 安装

### 1. 克隆仓库

```bash
git clone <仓库地址>
cd RagFlow2neo4j
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

依赖清单：
- `requests>=2.28.0`
- `networkx>=3.0`
- `pandas>=1.5.0`
- `neo4j>=5.0`
- `pytest>=7.0.0`（仅开发测试需要）

---

## 配置

复制示例配置文件并填写实际参数：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
    "ragflow": {
        "api_key": "your-ragflow-api-key",
        "kb_id": "your-knowledge-base-id",
        "base_url": "http://localhost:9380"
    },
    "output": {
        "dir": "output",
        "prefix": "output"
    },
    "neo4j": {
        "uri": "bolt://localhost:7687",
        "user": "neo4j",
        "password": "your-neo4j-password",
        "database": "neo4j"
    }
}
```

| 字段 | 说明 |
|------|------|
| `ragflow.api_key` | RAGFlow API 密钥 |
| `ragflow.kb_id` | 目标知识库（Dataset）ID |
| `ragflow.base_url` | RAGFlow 服务地址 |
| `output.dir` | CSV 输出文件夹 |
| `output.prefix` | CSV 文件名前缀 |
| `neo4j.uri` | Neo4j Bolt 地址 |
| `neo4j.user` | Neo4j 用户名 |
| `neo4j.password` | Neo4j 密码 |
| `neo4j.database` | Neo4j 数据库名（4.x+ 支持多数据库） |

> **注意**：`config.json` 已被 `.gitignore` 排除，不会被提交到版本控制，请放心填写真实密钥。

---

## 使用方式

### 交互式 CLI（推荐）

```bash
python cli.py
```

菜单选项：
- **1**：仅从 RagFlow 导出 CSV
- **2**：仅从 CSV 导入 Neo4j（支持自定义路径、连接测试、可选清空数据库）
- **3**：自动执行导出 + 导入 Neo4j（保留 CSV）
- **4**：退出

Windows 用户也可以直接双击运行 `start.bat`，脚本会自动检测虚拟环境并启动 CLI。

### 作为 Python 库导入

```python
from exporter import fetch_knowledge_graph, export_graph
from neo4j_importer import Neo4jWriter

# 导出 CSV
data = fetch_knowledge_graph()
export_graph(data)

# 导入 Neo4j
with Neo4jWriter() as writer:
    writer.test_connection()
    writer.import_nodes("output/output_kb_id_nodes.csv")
    writer.import_edges("output/output_kb_id_edges.csv")
```

---

## 项目结构

```
RagFlow2neo4j/
├── config.json              # 真实配置文件（gitignored）
├── config.example.json      # 配置示例模板
├── config.py                # 配置加载器（读取 config.json）
├── cli.py                   # 交互式 CLI 入口
├── exporter.py              # RAGFlow API 请求、图转换、CSV 导出
├── neo4j_importer.py        # Neo4j 批量写入封装
├── requirements.txt         # 依赖清单
├── start.bat                # Windows 一键启动脚本
├── tests/                   # 单元测试
│   ├── test_exporter.py
│   └── test_neo4j_importer.py
├── .gitignore
├── AGENTS.md                # AI 编程助手参考文档
└── README.md                # 本文件
```

---

## 数据格式说明

RAGFlow `/api/v1/datasets/{KB_ID}/graph/export` 接口返回的 JSON 结构详细说明，请参阅 [docs/ragflow_api_data_format.md](docs/ragflow_api_data_format.md)。

---

## 运行测试

```bash
pytest tests/ -v
```

当前覆盖：
- 属性清洗（None/NaN/Infinity、JSON 序列化、CSV 注入转义）
- RAGFlow API 请求（成功、HTTP 错误、API 错误码、网络异常）
- CSV 导出（空图、带数据图、异常数据）
- Neo4j 导入（关系类型校验、连接测试、节点/边批量导入）

---

## 安全提示

1. **敏感信息保护**：`config.json` 包含 API 密钥和密码，该文件已被 `.gitignore` 排除，请勿手动将其加入版本控制。
2. **CSV 注入防护**：导出时已对 `=`, `+`, `-`, `@` 等危险前缀进行单引号转义，但在 Office 中打开 CSV 仍需保持警惕。
3. **数据库清空不可逆**：CLI 在执行 `清空数据库` 前会要求输入 `y` 确认，操作前请确保已备份重要数据。

---

## 许可证

MIT License
