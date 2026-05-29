# config.example.py
# 配置模板 —— 复制为 config.py 并填写实际值
# 两套配置的区别仅在 ragflow 部分：local（本地）/ remote（远程）

# ==================== 配置切换开关 ====================
# 可选值: "local" | "remote"
_ACTIVE_PROFILE = "local"
# ====================================================

# 公共配置（两套环境共用）
_OUTPUT = {
    "dir": "output",
    "prefix": "output",
}

_NEO4J = {
    "uri": "bolt://localhost:7687",
    "user": "neo4j",
    "password": "",
    "database": "neo4j",
}

_PROFILES = {
    "local": {
        "ragflow": {
            "api_key": "",
            "kb_id": "",
            "base_url": "http://localhost:9380",
            "request_timeout": 120,
        },
        "output": _OUTPUT,
        "neo4j": _NEO4J,
    },
    "remote": {
        "ragflow": {
            "api_key": "",
            "kb_id": "",
            "base_url": "https://your-remote-ragflow.com",
            "request_timeout": 120,
        },
        "output": _OUTPUT,
        "neo4j": _NEO4J,
    },
}


def _load_config():
    """根据 _ACTIVE_PROFILE 加载对应的配置集"""
    if _ACTIVE_PROFILE not in _PROFILES:
        raise ValueError(
            "配置错误：_ACTIVE_PROFILE='%s' 不存在。可选值: %s"
            % (_ACTIVE_PROFILE, list(_PROFILES.keys()))
        )
    return _PROFILES[_ACTIVE_PROFILE]


_cfg = _load_config()

# ----------------- RAGFlow 配置 -----------------
RAGFLOW_API_KEY = _cfg["ragflow"]["api_key"]
KB_ID = _cfg["ragflow"]["kb_id"]
RAGFLOW_BASE_URL = _cfg["ragflow"]["base_url"]
RAGFLOW_REQUEST_TIMEOUT = _cfg["ragflow"].get("request_timeout", 120)
# -----------------------------------------------

# ----------------- CSV 导出配置 -----------------
_output_cfg = _cfg["output"]
OUTPUT_DIR = _output_cfg["dir"]
OUTPUT_PREFIX = _output_cfg["prefix"]
# -----------------------------------------------

# ----------------- Neo4j 配置 ------------------
_neo4j_cfg = _cfg["neo4j"]
NEO4J_URI = _neo4j_cfg["uri"]
NEO4J_USER = _neo4j_cfg["user"]
NEO4J_PASSWORD = _neo4j_cfg["password"]
NEO4J_DATABASE = _neo4j_cfg["database"]
# -----------------------------------------------

# 简单校验
if not RAGFLOW_API_KEY:
    raise ValueError(
        "配置错误：ragflow.api_key 不能为空。请在 config.py 的 _PROFILES['%s'] 中填写。"
        % _ACTIVE_PROFILE
    )
if not KB_ID:
    raise ValueError(
        "配置错误：ragflow.kb_id 不能为空。请在 config.py 的 _PROFILES['%s'] 中填写。"
        % _ACTIVE_PROFILE
    )
if not NEO4J_PASSWORD:
    raise ValueError(
        "配置错误：neo4j.password 不能为空。请在 config.py 的 _PROFILES['%s'] 中填写。"
        % _ACTIVE_PROFILE
    )
