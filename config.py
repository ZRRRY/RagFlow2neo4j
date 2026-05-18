# config.py
# 从 config.json 读取配置

import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_config():
    """加载 JSON 配置文件"""
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(
            "找不到配置文件: %s。请复制 config.example.json 为 config.json 并填写配置。" % _CONFIG_PATH
        )
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_cfg = _load_config()

# ----------------- RAGFlow 配置 -----------------
RAGFLOW_API_KEY = _cfg.get("ragflow", {}).get("api_key", "")
KB_ID = _cfg.get("ragflow", {}).get("kb_id", "")
RAGFLOW_BASE_URL = _cfg.get("ragflow", {}).get("base_url", "http://localhost:9380")
# -----------------------------------------------

# ----------------- CSV 导出配置 -----------------
_output_cfg = _cfg.get("output", {})
OUTPUT_DIR = _output_cfg.get("dir", "output")
OUTPUT_PREFIX = _output_cfg.get("prefix", "output")
# -----------------------------------------------

# ----------------- Neo4j 配置 ------------------
_neo4j_cfg = _cfg.get("neo4j", {})
NEO4J_URI = _neo4j_cfg.get("uri", "bolt://localhost:7687")
NEO4J_USER = _neo4j_cfg.get("user", "neo4j")
NEO4J_PASSWORD = _neo4j_cfg.get("password", "")
NEO4J_DATABASE = _neo4j_cfg.get("database", "neo4j")
# -----------------------------------------------

# 简单校验
if not RAGFLOW_API_KEY:
    raise ValueError("配置错误：ragflow.api_key 不能为空。请在 config.json 中填写。")
if not KB_ID:
    raise ValueError("配置错误：ragflow.kb_id 不能为空。请在 config.json 中填写。")
if not NEO4J_PASSWORD:
    raise ValueError("配置错误：neo4j.password 不能为空。请在 config.json 中填写。")
