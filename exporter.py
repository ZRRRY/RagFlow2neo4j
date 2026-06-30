# exporter.py
import json
import logging
import math
import os

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

# ----------------- 日志配置 -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
# ------------------------------------------

# 流式导出时，节点与边仅保留 content_with_weight 中的字段
_DIRECT_NODE_COLUMNS = ["id", "entity_type", "description", "source_id", "pagerank", "rank"]
_DIRECT_EDGE_COLUMNS = ["source", "target", "description", "keywords", "weight", "source_id"]

# ----------------- 从 config 读取配置 -----------------
RAGFLOW_API_KEY = config.RAGFLOW_API_KEY
KB_ID = config.KB_ID
RAGFLOW_BASE_URL = config.RAGFLOW_BASE_URL
OUTPUT_DIR = config.OUTPUT_DIR
OUTPUT_PREFIX = config.OUTPUT_PREFIX
RAGFLOW_REQUEST_TIMEOUT = config.RAGFLOW_REQUEST_TIMEOUT
# ----------------------------------------------------


def _read_opensearch_config():
    """统一从 config 读取 OpenSearch 直连配置。"""
    return {
        "host": config.OPENSEARCH_HOST,
        "port": config.OPENSEARCH_PORT,
        "user": config.OPENSEARCH_USER,
        "password": config.OPENSEARCH_PASSWORD,
        "use_ssl": config.OPENSEARCH_USE_SSL,
    }


def _read_elasticsearch_config():
    """统一从 config 读取 Elasticsearch 直连配置。"""
    return {
        "host": config.ELASTICSEARCH_HOST,
        "port": config.ELASTICSEARCH_PORT,
        "user": config.ELASTICSEARCH_USER,
        "password": config.ELASTICSEARCH_PASSWORD,
        "use_ssl": config.ELASTICSEARCH_USE_SSL,
    }


def _escape_csv_injection(value):
    """对可能触发 Excel/LibreOffice 公式注入的字符串进行转义。

    如果字符串以 =, +, -, @, 制表符、回车或换行开头，
    在前面加上单引号 '，使其被当作纯文本处理。
    """
    if isinstance(value, str) and value:
        if value[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
            return "'" + value
    return value


def _safe_serialize(value):
    """将任何复杂值转换为 CSV 安全字符串。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
    if isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, (list, dict, tuple, set)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _get_session():
    """创建带重试机制的 requests Session"""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session


def _get_tenant_id():
    """通过 RAGFlow Dataset API 获取 tenant_id。

    tenant_id 用于构造搜索引擎索引名 ragflow_{tenant_id}。
    """
    dataset_url = f"{RAGFLOW_BASE_URL}/api/v1/datasets/{KB_ID}"
    headers = {
        "Authorization": f"Bearer {RAGFLOW_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "RagFlow2neo4j/1.0 (https://github.com/RagFlow2neo4j)",
    }

    logger.info("正在获取 Dataset 信息以确定 tenant_id: %s", dataset_url)
    try:
        session = _get_session()
        resp = session.get(dataset_url, headers=headers, timeout=30)
    except requests.exceptions.RequestException as exc:
        logger.error("获取 Dataset 信息请求异常: %s", exc)
        return None

    if resp.status_code != 200:
        logger.error("获取 Dataset 信息失败: HTTP %s, %s", resp.status_code, resp.text)
        return None

    try:
        dataset_result = resp.json()
    except json.JSONDecodeError as exc:
        logger.error("Dataset 响应 JSON 解析失败: %s", exc)
        return None

    if dataset_result.get("code") != 0:
        logger.error("Dataset API 返回错误: %s", dataset_result.get("message"))
        return None

    tenant_id = dataset_result.get("data", {}).get("tenant_id")
    if not tenant_id:
        logger.error("Dataset 响应中缺少 tenant_id，无法确定搜索引擎索引名。")
        return None

    return tenant_id


def _count_es_docs(count_url, query, auth):
    """使用搜索引擎的 _count API 获取符合条件的文档总数。

    OpenSearch 与 Elasticsearch 的 _count 接口兼容，因此本函数通用。
    """
    try:
        resp = requests.post(count_url, json=query, auth=auth, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        return result.get("count", 0)
    except Exception as exc:
        logger.error("搜索引擎计数请求异常: %s", exc)
        return None


def _scroll_search_batches(search_url, query, auth, scheme, host, port, batch_size=1000):
    """使用搜索引擎 scroll API 逐批次 yield hits 列表。

    OpenSearch 与 Elasticsearch 的 scroll 接口兼容，因此本函数通用。
    """
    scroll_id = None
    scroll_search_url = f"{search_url}?scroll=2m"

    try:
        init_resp = requests.post(
            scroll_search_url,
            json={**query, "size": batch_size},
            auth=auth,
            timeout=60,
        )
        init_resp.raise_for_status()
        init_result = init_resp.json()
        scroll_id = init_result.get("_scroll_id")
        hits = init_result.get("hits", {}).get("hits", [])
        if hits:
            yield hits

        while len(hits) > 0:
            scroll_resp = requests.post(
                f"{scheme}://{host}:{port}/_search/scroll",
                json={"scroll": "2m", "scroll_id": scroll_id},
                auth=auth,
                timeout=60,
            )
            scroll_resp.raise_for_status()
            scroll_result = scroll_resp.json()
            scroll_id = scroll_result.get("_scroll_id")
            hits = scroll_result.get("hits", {}).get("hits", [])
            if hits:
                yield hits
    finally:
        if scroll_id:
            try:
                requests.delete(
                    f"{scheme}://{host}:{port}/_search/scroll",
                    json={"scroll_id": [scroll_id]},
                    auth=auth,
                    timeout=30,
                )
            except Exception:
                pass


def export_graph_direct(kb_id=None, output_dir=None, output_prefix=None, batch_size=1000, engine="opensearch"):
    """绕过 /graph/export API，直接从搜索引擎流式读取并导出 CSV。

    参数:
        engine: "opensearch" 或 "elasticsearch"，决定读取哪个配置。

    流程：
      1. 通过 RAGFlow Dataset API 获取 tenant_id；
      2. 使用搜索引擎 _count API 统计实体总数和关系总数；
      3. 分两个阶段 scroll 拉取：
         - 阶段一：读取实体文档，逐批追加写入 nodes.csv；
         - 阶段二：读取关系文档，逐批追加写入 edges.csv。
    每批次均打印分数形式的进度日志（当前/总数）。
    """
    if engine not in ("opensearch", "elasticsearch"):
        logger.error("不支持的搜索引擎类型: %s，仅支持 opensearch 或 elasticsearch", engine)
        return False

    kb_id = kb_id or KB_ID
    output_dir = output_dir or OUTPUT_DIR or os.path.dirname(output_prefix or OUTPUT_PREFIX)
    base_name = os.path.basename(output_prefix or OUTPUT_PREFIX) or "output"

    engine_label = "OpenSearch" if engine == "opensearch" else "Elasticsearch"
    read_config_fn = _read_opensearch_config if engine == "opensearch" else _read_elasticsearch_config

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return False

    search_cfg = read_config_fn()
    host = search_cfg["host"]
    port = search_cfg["port"]
    user = search_cfg["user"]
    password = search_cfg["password"]
    scheme = "https" if search_cfg["use_ssl"] else "http"

    index_name = f"ragflow_{tenant_id}"
    search_url = f"{scheme}://{host}:{port}/{index_name}/_search"
    count_url = f"{scheme}://{host}:{port}/{index_name}/_count"
    auth = (user, password) if user else None

    entity_query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kb_id": kb_id}},
                    {"term": {"knowledge_graph_kwd": "entity"}},
                ]
            }
        }
    }
    relation_query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kb_id": kb_id}},
                    {"term": {"knowledge_graph_kwd": "relation"}},
                ]
            }
        }
    }

    # ---- 先统计数量 ----
    logger.info("正在统计 %s 中实体与关系文档数量...", engine_label)
    entity_total = _count_es_docs(count_url, entity_query, auth)
    relation_total = _count_es_docs(count_url, relation_query, auth)
    if entity_total is None or relation_total is None:
        logger.error("统计文档数量失败，导出终止。")
        return False

    logger.info("%s 统计结果: 实体 %s 个, 关系 %s 个", engine_label, entity_total, relation_total)

    if entity_total == 0 and relation_total == 0:
        logger.error("该知识库在 %s 中没有任何实体或关系文档，导出终止。", engine_label)
        return False

    # ---- 准备 CSV 路径 ----
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    nodes_csv = os.path.join(output_dir, f"{base_name}_{kb_id}_nodes.csv")
    edges_csv = os.path.join(output_dir, f"{base_name}_{kb_id}_edges.csv")

    # 若文件已存在则先删除，避免追加到旧数据
    for f in (nodes_csv, edges_csv):
        if os.path.exists(f):
            os.remove(f)

    nodes_header_written = False
    edges_header_written = False

    # ---- 阶段一：流式导出实体 ----
    logger.info("开始流式导出节点 CSV (总数 %s)...", entity_total)
    total_nodes_written = 0
    batch_idx = 0

    for batch in _scroll_search_batches(search_url, entity_query, auth, scheme, host, port, batch_size):
        batch_idx += 1
        rows = []
        for hit in batch:
            source = hit.get("_source", {})
            content = {}
            try:
                content = json.loads(source.get("content_with_weight", "{}"))
            except (json.JSONDecodeError, TypeError):
                pass

            entity_name = content.get("entity_name")
            if not entity_name:
                continue

            row = {
                "id": entity_name,
                "entity_type": content.get("entity_type", ""),
                "description": content.get("description", ""),
                "source_id": content.get("source_id", []),
                "pagerank": content.get("pagerank", ""),
                "rank": content.get("rank", ""),
            }

            for k, v in list(row.items()):
                row[k] = _escape_csv_injection(_safe_serialize(v))

            rows.append(row)

        if rows:
            df = pd.DataFrame(rows, columns=_DIRECT_NODE_COLUMNS)
            df.to_csv(
                nodes_csv,
                mode="a",
                header=not nodes_header_written,
                index=False,
                encoding="utf-8-sig",
            )
            nodes_header_written = True
            total_nodes_written += len(rows)
            logger.info(
                "节点写入批次 %s: 本批 %s 条，累计 %s/%s 条",
                batch_idx, len(rows), total_nodes_written, entity_total,
            )

    # ---- 阶段二：流式导出关系 ----
    logger.info("开始流式导出边 CSV (总数 %s)...", relation_total)
    total_edges_written = 0
    batch_idx = 0

    for batch in _scroll_search_batches(search_url, relation_query, auth, scheme, host, port, batch_size):
        batch_idx += 1
        rows = []
        for hit in batch:
            source = hit.get("_source", {})
            content = {}
            try:
                content = json.loads(source.get("content_with_weight", "{}"))
            except (json.JSONDecodeError, TypeError):
                pass

            from_entity = content.get("src_id")
            to_entity = content.get("tgt_id")
            if not from_entity or not to_entity:
                continue

            row = {
                "source": from_entity,
                "target": to_entity,
                "description": content.get("description", ""),
                "keywords": content.get("keywords", []),
                "weight": content.get("weight", ""),
                "source_id": content.get("source_id", []),
            }

            for k, v in list(row.items()):
                row[k] = _escape_csv_injection(_safe_serialize(v))

            rows.append(row)

        if rows:
            df = pd.DataFrame(rows, columns=_DIRECT_EDGE_COLUMNS)
            df.to_csv(
                edges_csv,
                mode="a",
                header=not edges_header_written,
                index=False,
                encoding="utf-8-sig",
            )
            edges_header_written = True
            total_edges_written += len(rows)
            logger.info(
                "关系写入批次 %s: 本批 %s 条，累计 %s/%s 条",
                batch_idx, len(rows), total_edges_written, relation_total,
            )

    logger.info(
        "%s 流式导出完成: 节点 %s 个，关系 %s 条。CSV: %s, %s",
        engine_label,
        total_nodes_written,
        total_edges_written,
        nodes_csv,
        edges_csv,
    )
    return True


def export_graph_direct_elasticsearch(kb_id=None, output_dir=None, output_prefix=None, batch_size=1000):
    """从 Elasticsearch 流式导出 CSV 的便捷函数。"""
    return export_graph_direct(
        kb_id=kb_id,
        output_dir=output_dir,
        output_prefix=output_prefix,
        batch_size=batch_size,
        engine="elasticsearch",
    )
