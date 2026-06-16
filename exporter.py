# ragflow_api.py
import json
import logging
import math
import os

import networkx as nx
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

# OpenSearch 直连导出时，节点与边仅保留 content_with_weight 中的字段
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


def sanitize_attrs(G):
    """递归处理图中所有节点和边的属性，将非标量值转换为 JSON 字符串，
    并处理 None 和 NaN，确保 CSV 导出时的兼容性。
    """
    for node, attrs in list(G.nodes(data=True)):
        for key, value in list(attrs.items()):
            attrs[key] = _escape_csv_injection(_safe_serialize(value))

    for u, v, attrs in list(G.edges(data=True)):
        for key, value in list(attrs.items()):
            attrs[key] = _escape_csv_injection(_safe_serialize(value))

    for key, value in list(G.graph.items()):
        G.graph[key] = _escape_csv_injection(_safe_serialize(value))

    return G


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
    """通过 RAGFlow Dataset API 获取 tenant_id。"""
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
        logger.error("Dataset 响应中缺少 tenant_id，无法确定 OpenSearch 索引名。")
        return None

    return tenant_id


def fetch_knowledge_graph():
    """调用 RAGFlow API 获取知识图谱数据"""
    url = f"{RAGFLOW_BASE_URL}/api/v1/datasets/{KB_ID}/graph/export"
    headers = {
        "Authorization": f"Bearer {RAGFLOW_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "RagFlow2neo4j/1.0 (https://github.com/RagFlow2neo4j)"
    }

    logger.info("正在请求: %s", url)
    logger.info("请求超时设置: %s 秒", RAGFLOW_REQUEST_TIMEOUT)
    try:
        session = _get_session()
        response = session.get(url, headers=headers, timeout=RAGFLOW_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        logger.error("请求异常: %s", exc)
        return None

    if response.status_code != 200:
        logger.error("请求失败: HTTP %s, 响应内容: %s", response.status_code, response.text)
        return None

    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        logger.error("响应 JSON 解析失败: %s", exc)
        return None

    if result.get("code") != 0:
        logger.error("API 返回错误: %s", result.get("message"))
        return None

    return result.get("data")


def _count_os_docs(count_url, query, auth):
    """使用 OpenSearch _count API 获取符合条件的文档总数。"""
    try:
        resp = requests.post(count_url, json=query, auth=auth, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        return result.get("count", 0)
    except Exception as exc:
        logger.error("OpenSearch 计数请求异常: %s", exc)
        return None


def _scroll_search_batches(search_url, query, auth, scheme, os_host, os_port, batch_size=1000):
    """使用 OpenSearch scroll API 逐批次 yield hits 列表。"""
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
                f"{scheme}://{os_host}:{os_port}/_search/scroll",
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
                    f"{scheme}://{os_host}:{os_port}/_search/scroll",
                    json={"scroll_id": [scroll_id]},
                    auth=auth,
                    timeout=30,
                )
            except Exception:
                pass


def fetch_knowledge_graph_direct():
    """绕过 RAGFlow /graph/export API，直接从 OpenSearch 读取实体和关系文档构建知识图谱。

    适用场景：RAGFlow 因数据量过大导致导出接口异常时。

    实现原理：RAGFlow 将最终知识图谱以独立文档形式存入 OpenSearch：
      - 实体文档：knowledge_graph_kwd="entity"
      - 关系文档：knowledge_graph_kwd="relation"
    本方法分别读取这两类文档，解析后构建 NetworkX 图对象。

    返回格式与 fetch_knowledge_graph() 保持一致：{"graph": <dict>}。
    """
    tenant_id = _get_tenant_id()
    if not tenant_id:
        return None

    os_cfg = _read_opensearch_config()
    os_host = os_cfg["host"]
    os_port = os_cfg["port"]
    os_user = os_cfg["user"]
    os_pass = os_cfg["password"]
    scheme = "https" if os_cfg["use_ssl"] else "http"

    index_name = f"ragflow_{tenant_id}"
    search_url = f"{scheme}://{os_host}:{os_port}/{index_name}/_search"
    auth = (os_user, os_pass) if os_user else None

    entity_query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kb_id": KB_ID}},
                    {"term": {"knowledge_graph_kwd": "entity"}},
                ]
            }
        }
    }
    relation_query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kb_id": KB_ID}},
                    {"term": {"knowledge_graph_kwd": "relation"}},
                ]
            }
        }
    }

    logger.info("正在从 OpenSearch 读取实体文档...")
    entity_hits = list(_scroll_search_batches(search_url, entity_query, auth, scheme, os_host, os_port))
    entity_hits = [hit for batch in entity_hits for hit in batch]
    logger.info("共读取 %s 个实体文档", len(entity_hits))

    logger.info("正在从 OpenSearch 读取关系文档...")
    relation_hits = list(_scroll_search_batches(search_url, relation_query, auth, scheme, os_host, os_port))
    relation_hits = [hit for batch in relation_hits for hit in batch]
    logger.info("共读取 %s 个关系文档", len(relation_hits))

    if not entity_hits and not relation_hits:
        logger.error("该知识库在 OpenSearch 中没有任何实体或关系文档，无法构建图谱。")
        return None

    G = nx.Graph()
    G.graph["created_by"] = "RagFlow2neo4j_direct"

    node_count = 0
    for hit in entity_hits:
        source = hit.get("_source", {})
        content = {}
        try:
            content = json.loads(source.get("content_with_weight", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass

        entity_name = content.get("entity_name")
        if not entity_name:
            continue

        attrs = {
            "entity_type": content.get("entity_type", ""),
            "description": content.get("description", ""),
            "source_id": content.get("source_id", []),
            "pagerank": content.get("pagerank", ""),
            "rank": content.get("rank", ""),
        }

        G.add_node(entity_name, **attrs)
        node_count += 1

    edge_count = 0
    for hit in relation_hits:
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

        attrs = {
            "description": content.get("description", ""),
            "keywords": content.get("keywords", []),
            "weight": content.get("weight", ""),
            "source_id": content.get("source_id", []),
        }

        G.add_edge(from_entity, to_entity, **attrs)
        edge_count += 1

    graph_data = nx.node_link_data(G, edges="edges")
    logger.info(
        "成功从 OpenSearch 构建知识图谱: %s 个节点, %s 条边",
        node_count, edge_count,
    )

    return {"graph": graph_data}


def export_graph_direct(kb_id=None, output_dir=None, output_prefix=None, batch_size=1000):
    """绕过 API，直接从 OpenSearch 流式读取并导出 CSV。

    流程：
      1. 先通过 _count API 统计实体总数和关系总数；
      2. 分两个阶段 scroll 拉取：
         - 阶段一：读取实体文档，逐批追加写入 nodes.csv；
         - 阶段二：读取关系文档，逐批追加写入 edges.csv；
      3. 若关系引用了尚未写入的节点，自动在 nodes.csv 中补录空节点。
    每批次均打印分数形式的进度日志（当前/总数）。
    """
    kb_id = kb_id or KB_ID
    output_dir = output_dir or OUTPUT_DIR or os.path.dirname(output_prefix or OUTPUT_PREFIX)
    base_name = os.path.basename(output_prefix or OUTPUT_PREFIX) or "output"

    tenant_id = _get_tenant_id()
    if not tenant_id:
        return False

    os_cfg = _read_opensearch_config()
    os_host = os_cfg["host"]
    os_port = os_cfg["port"]
    os_user = os_cfg["user"]
    os_pass = os_cfg["password"]
    scheme = "https" if os_cfg["use_ssl"] else "http"

    index_name = f"ragflow_{tenant_id}"
    search_url = f"{scheme}://{os_host}:{os_port}/{index_name}/_search"
    count_url = f"{scheme}://{os_host}:{os_port}/{index_name}/_count"
    auth = (os_user, os_pass) if os_user else None

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
    logger.info("正在统计 OpenSearch 中实体与关系文档数量...")
    entity_total = _count_os_docs(count_url, entity_query, auth)
    relation_total = _count_os_docs(count_url, relation_query, auth)
    if entity_total is None or relation_total is None:
        logger.error("统计文档数量失败，导出终止。")
        return False

    logger.info("OpenSearch 统计结果: 实体 %s 个, 关系 %s 个", entity_total, relation_total)

    if entity_total == 0 and relation_total == 0:
        logger.error("该知识库在 OpenSearch 中没有任何实体或关系文档，导出终止。")
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

    for batch in _scroll_search_batches(search_url, entity_query, auth, scheme, os_host, os_port, batch_size):
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

    for batch in _scroll_search_batches(search_url, relation_query, auth, scheme, os_host, os_port, batch_size):
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
        "流式导出完成: 节点 %s 个，关系 %s 条。CSV: %s, %s",
        total_nodes_written,
        total_edges_written,
        nodes_csv,
        edges_csv,
    )
    return True


def export_graph(data):
    """将 API 数据转为图对象并导出 CSV 文件（节点和边）"""
    if not isinstance(data, dict):
        logger.error("API 数据格式异常，期望 dict，实际为 %s", type(data).__name__)
        return

    graph_info = data.get("graph")
    if not isinstance(graph_info, dict):
        logger.error("API 数据中缺少 graph 字段或格式异常")
        return

    nodes = graph_info.get("nodes", [])
    edges = graph_info.get("edges", [])
    logger.info("成功获取图谱: %s 个节点, %s 条边", len(nodes), len(edges))

    # 构建 NetworkX 图（使用 edges="edges" 以兼容 NetworkX 3.x+ 的默认行为）
    try:
        G = nx.node_link_graph(graph_info, edges="edges")
    except Exception as exc:
        logger.error("NetworkX 建图失败: %s", exc)
        return

    # 清洗属性以保证 CSV 兼容
    G = sanitize_attrs(G)

    # 构建 CSV 路径（含 KB_ID，用于区分不同知识库）
    output_dir = OUTPUT_DIR or os.path.dirname(OUTPUT_PREFIX)
    base_name = os.path.basename(OUTPUT_PREFIX) or "output"
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    nodes_csv = os.path.join(output_dir, f"{base_name}_{KB_ID}_nodes.csv")
    edges_csv = os.path.join(output_dir, f"{base_name}_{KB_ID}_edges.csv")

    # 导出节点 CSV（第一列为 id，带列名）
    nodes_data = dict(G.nodes(data=True))
    if nodes_data:
        nodes_df = pd.DataFrame.from_dict(nodes_data, orient="index")
    else:
        nodes_df = pd.DataFrame()
    nodes_df.to_csv(
        nodes_csv,
        encoding="utf-8-sig",
        index_label="id"
    )

    # 导出边 CSV
    if G.number_of_edges() > 0:
        edges_df = nx.to_pandas_edgelist(G)
    else:
        edges_df = pd.DataFrame()
    edges_df.to_csv(
        edges_csv,
        index=False,
        encoding="utf-8-sig"
    )

    logger.info(
        "已导出节点和边的 CSV 文件: %s, %s",
        nodes_csv, edges_csv
    )
