# config.example.py
# 配置模板 —— 复制为 config.py 并填写实际值
#
# 使用说明：
#   1. 复制本文件为 config.py：cp config.example.py config.py
#   2. 在下方 "4. 各环境具体配置" 中填写 local / remote 环境的实际值。
#   3. 修改 "1. 当前激活的配置集" 中的 _ACTIVE_PROFILE 切换环境。
#   4. 未在 profile 中填写的项会自动继承 "2. 全局默认配置" 中的值。
#   5. 启动时 CLI 会自动检查 "3. 必填配置项声明" 中的项是否已填写。


# ==================== 1. 当前激活的配置集 ====================
# 可选值: "local" | "remote"
# 对应 _PROFILES 中定义的键名，修改此处即可切换环境。
_ACTIVE_PROFILE = "local"
# ===========================================================


# ==================== 2. 全局默认配置 ====================
# 集中管理所有配置项的默认值。
# 各 profile 可以只填写与环境相关的差异项，未填写的项将自动继承以下默认值。
DEFAULTS = {
    # RAGFlow 通用配置
    # 仅用于调用 Dataset API 获取 tenant_id，以构造搜索引擎索引名。
    "ragflow": {
        # Dataset API 请求超时时间（秒）
        "request_timeout": 120,
    },

    # CSV 导出配置
    "output": {
        "dir": "output",      # 输出目录
        "prefix": "output",   # 输出文件名前缀
    },

    # Neo4j 连接配置
    "neo4j": {
        "uri": "bolt://localhost:7687",
        "user": "neo4j",
        "database": "neo4j",
    },

    # OpenSearch 直连配置
    # 图谱数据通过 OpenSearch 的 _count + scroll API 流式拉取。
    "opensearch": {
        "host": "localhost",
        "port": 9201,
        "user": "admin",
        "password": "",
        "use_ssl": False,
    },

    # Elasticsearch 直连配置
    # 图谱数据通过 Elasticsearch 的 _count + scroll API 流式拉取。
    "elasticsearch": {
        "host": "localhost",
        "port": 9200,
        "user": "",
        "password": "",
        "use_ssl": False,
    },
}
# =======================================================


# ==================== 3. 必填配置项声明 ====================
# 运行前会检查以下配置项是否已填写。
# 若任一必填项为空或缺失，CLI 将拒绝启动并列出所有缺失项。
REQUIRED_CONFIG = {
    "ragflow": [
        "api_key",   # RAGFlow API 密钥（仅用于获取 tenant_id）
        "kb_id",     # 知识库（Dataset）ID
        "base_url",  # RAGFlow 服务地址
    ],
    "neo4j": [
        "uri",       # Neo4j Bolt 地址
        "user",      # Neo4j 用户名
        "password",  # Neo4j 密码
    ],
}
# =========================================================


# ==================== 4. 各环境具体配置 ====================
# 请在此处填写不同环境的实际值。
# 注释掉的项表示继承 DEFAULTS 中的默认值，无需重复填写。
_PROFILES = {
    # 本地环境
    "local": {
        "ragflow": {
            # 必填：从 RAGFlow 个人设置中获取 API Key
            "api_key": "",
            # 必填：目标知识库的 ID（Dataset ID）
            "kb_id": "",
            # 必填：RAGFlow 服务地址
            "base_url": "http://localhost:9380",
            # 可选：Dataset API 请求超时（秒）
            # "request_timeout": 120,
        },
        "output": {
            # "dir": "output",
            # "prefix": "output",
        },
        "neo4j": {
            # "uri": "bolt://localhost:7687",
            # "user": "neo4j",
            # 必填：Neo4j 密码
            "password": "",
            # "database": "neo4j",
        },
        "opensearch": {
            # 以下配置用于从 OpenSearch 流式导出图谱数据
            # 需要覆盖 DEFAULTS 时取消对应项的注释并填写实际值
            # "host": "localhost",
            # "port": 9201,
            # "user": "admin",
            # "password": "",
            # "use_ssl": False,
        },
        "elasticsearch": {
            # 以下配置用于从 Elasticsearch 流式导出图谱数据
            # 需要覆盖 DEFAULTS 时取消对应项的注释并填写实际值
            # "host": "localhost",
            # "port": 9200,
            # "user": "",
            # "password": "",
            # "use_ssl": False,
        },
    },

    # 远程环境
    "remote": {
        "ragflow": {
            "api_key": "",
            "kb_id": "",
            "base_url": "https://your-remote-ragflow.com",
            # "request_timeout": 120,
        },
        "output": {
            # "dir": "output",
            # "prefix": "output",
        },
        "neo4j": {
            # "uri": "bolt://localhost:7687",
            # "user": "neo4j",
            "password": "",
            # "database": "neo4j",
        },
        "opensearch": {
            # 需要覆盖 DEFAULTS 时取消对应项的注释并填写实际值
            # "host": "localhost",
            # "port": 9201,
            # "user": "admin",
            # "password": "",
            # "use_ssl": False,
        },
        "elasticsearch": {
            # 需要覆盖 DEFAULTS 时取消对应项的注释并填写实际值
            # "host": "localhost",
            # "port": 9200,
            # "user": "",
            # "password": "",
            # "use_ssl": False,
        },
    },
}
# =========================================================


# ==================== 5. 配置加载与校验函数 ====================
# 以下内容通常无需手动修改。


def _deep_merge(base, override):
    """递归合并两个字典，override 优先级更高。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_config():
    """根据 _ACTIVE_PROFILE 加载对应的配置集，并合并默认值。"""
    if _ACTIVE_PROFILE not in _PROFILES:
        raise ValueError(
            "配置错误：_ACTIVE_PROFILE='%s' 不存在。可选值: %s"
            % (_ACTIVE_PROFILE, list(_PROFILES.keys()))
        )
    return _deep_merge(DEFAULTS, _PROFILES[_ACTIVE_PROFILE])


def validate_config():
    """运行前检查：校验当前激活的配置集是否包含所有必需项。

    返回 (is_valid, missing) 元组，便于 CLI 做友好提示。
    校验时会自动合并 DEFAULTS，因此未在 profile 中填写但存在于 DEFAULTS 中的项不会被判定为缺失。
    """
    if _ACTIVE_PROFILE not in _PROFILES:
        return False, ["_ACTIVE_PROFILE='%s' 不存在" % _ACTIVE_PROFILE]

    cfg = _load_config()
    missing = []
    for section, keys in REQUIRED_CONFIG.items():
        section_cfg = cfg.get(section, {})
        for key in keys:
            value = section_cfg.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append("%s.%s" % (section, key))

    return not missing, missing


# ==================== 6. 导出配置变量 ====================
# 业务模块通过 import config 使用以下变量。

_cfg = _load_config()

# RAGFlow 配置（仅用于获取 tenant_id）
RAGFLOW_API_KEY = _cfg["ragflow"]["api_key"]
KB_ID = _cfg["ragflow"]["kb_id"]
RAGFLOW_BASE_URL = _cfg["ragflow"]["base_url"]
RAGFLOW_REQUEST_TIMEOUT = _cfg["ragflow"]["request_timeout"]

# CSV 导出配置
_output_cfg = _cfg["output"]
OUTPUT_DIR = _output_cfg["dir"]
OUTPUT_PREFIX = _output_cfg["prefix"]

# Neo4j 配置
_neo4j_cfg = _cfg["neo4j"]
NEO4J_URI = _neo4j_cfg["uri"]
NEO4J_USER = _neo4j_cfg["user"]
NEO4J_PASSWORD = _neo4j_cfg["password"]
NEO4J_DATABASE = _neo4j_cfg["database"]

# OpenSearch 直连配置
_opensearch_cfg = _cfg["opensearch"]
OPENSEARCH_HOST = _opensearch_cfg["host"]
OPENSEARCH_PORT = _opensearch_cfg["port"]
OPENSEARCH_USER = _opensearch_cfg["user"]
OPENSEARCH_PASSWORD = _opensearch_cfg["password"]
OPENSEARCH_USE_SSL = _opensearch_cfg["use_ssl"]

# Elasticsearch 直连配置
_elasticsearch_cfg = _cfg["elasticsearch"]
ELASTICSEARCH_HOST = _elasticsearch_cfg["host"]
ELASTICSEARCH_PORT = _elasticsearch_cfg["port"]
ELASTICSEARCH_USER = _elasticsearch_cfg["user"]
ELASTICSEARCH_PASSWORD = _elasticsearch_cfg["password"]
ELASTICSEARCH_USE_SSL = _elasticsearch_cfg["use_ssl"]

# 模块导入时执行一次校验，确保配置完整
_is_valid, _missing = validate_config()
if not _is_valid:
    raise ValueError(
        "配置错误：以下必需项为空或缺失，请在 config.py 的 _PROFILES['%s'] 中填写：\n  - %s"
        % (_ACTIVE_PROFILE, "\n  - ".join(_missing))
    )
# =======================================================
