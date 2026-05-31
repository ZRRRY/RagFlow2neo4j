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

# ----------------- 从 config 读取配置 -----------------
RAGFLOW_API_KEY = config.RAGFLOW_API_KEY
KB_ID = config.KB_ID
RAGFLOW_BASE_URL = config.RAGFLOW_BASE_URL
OUTPUT_DIR = getattr(config, "OUTPUT_DIR", "")
OUTPUT_PREFIX = config.OUTPUT_PREFIX
RAGFLOW_REQUEST_TIMEOUT = getattr(config, "RAGFLOW_REQUEST_TIMEOUT", 120)
# ----------------------------------------------------
#https://rag.artroot.cn

def _escape_csv_injection(value):
    """对可能触发 Excel/LibreOffice 公式注入的字符串进行转义。

    如果字符串以 =, +, -, @, 制表符、回车或换行开头，
    在前面加上单引号 '，使其被当作纯文本处理。
    """
    if isinstance(value, str) and value:
        if value[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
            return "'" + value
    return value


def sanitize_attrs(G):
    """递归处理图中所有节点和边的属性，将非标量值转换为 JSON 字符串，
    并处理 None 和 NaN，确保 CSV 导出时的兼容性。
    """
    def safe_serialize(value):
        """将任何复杂值转换为字符串"""
        # 处理 None
        if value is None:
            return ""
        # 处理 NaN 或 Infinity (float类型)
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return ""
        # 基础类型直接返回（float 中 NaN/Infinity 已在上一步处理）
        if isinstance(value, (str, int, bool, float)):
            return value
        # 列表、字典等复杂类型 -> JSON 字符串
        if isinstance(value, (list, dict, tuple, set)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(value)
        # 其他类型转字符串
        return str(value)

    # 清洗所有节点属性
    for node, attrs in list(G.nodes(data=True)):
        for key, value in list(attrs.items()):
            attrs[key] = _escape_csv_injection(safe_serialize(value))

    # 清洗所有边属性
    for u, v, attrs in list(G.edges(data=True)):
        for key, value in list(attrs.items()):
            attrs[key] = _escape_csv_injection(safe_serialize(value))

    # 同样处理图级别的属性（如果有）
    for key, value in list(G.graph.items()):
        G.graph[key] = _escape_csv_injection(safe_serialize(value))

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


def fetch_knowledge_graph_direct():
    """绕过 RAGFlow /graph/export API，直接从 OpenSearch 读取完整的知识图谱数据。

    适用场景：RAGFlow 因 OpenSearch max_result_window 限制导致导出接口返回
    Internal server error（HTTP 200, code=102）时。

    实现原理：RAGFlow 在构建完知识图谱后，会将完整的 NetworkX node_link_data
    序列化后存入 OpenSearch 中 knowledge_graph_kwd="graph" 的文档。该文档只有
    一条，不受分页窗口限制，因此可直接读取以绕过 RAGFlow 侧有缺陷的分页查询逻辑。

    返回格式与 fetch_knowledge_graph() 保持一致：{"graph": <dict>}。
    """
    # ---- 1. 通过 RAGFlow Dataset API 获取 tenant_id ----
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

    # ---- 2. 直连 OpenSearch 查询 graph 文档 ----
    os_host = getattr(config, "OPENSEARCH_HOST", "localhost")
    os_port = getattr(config, "OPENSEARCH_PORT", 9201)
    os_user = getattr(config, "OPENSEARCH_USER", "admin")
    os_pass = getattr(config, "OPENSEARCH_PASSWORD", "")
    os_ssl = getattr(config, "OPENSEARCH_USE_SSL", False)
    scheme = "https" if os_ssl else "http"

    index_name = f"ragflow_{tenant_id}"
    search_url = f"{scheme}://{os_host}:{os_port}/{index_name}/_search"

    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"kb_id": KB_ID}},
                    {"term": {"knowledge_graph_kwd": "graph"}},
                ]
            }
        },
        "size": 1,
    }

    auth = (os_user, os_pass) if os_user else None
    logger.info("正在直连 OpenSearch 读取 graph 文档: %s", search_url)

    try:
        os_resp = requests.post(search_url, json=query, auth=auth, timeout=60)
    except requests.exceptions.RequestException as exc:
        logger.error("OpenSearch 请求异常: %s", exc)
        return None

    if os_resp.status_code != 200:
        logger.error("OpenSearch 查询失败: HTTP %s, %s", os_resp.status_code, os_resp.text)
        return None

    try:
        os_result = os_resp.json()
    except json.JSONDecodeError as exc:
        logger.error("OpenSearch 响应 JSON 解析失败: %s", exc)
        return None

    hits = os_result.get("hits", {}).get("hits", [])
    if not hits:
        logger.warning(
            "OpenSearch 中未找到 knowledge_graph_kwd='graph' 的汇总文档，"
            "正在执行诊断查询并尝试从 subgraph 重建..."
        )
        # ---- 诊断：查看该 kb_id 下有哪些类型的数据 ----
        diag_query = {
            "query": {"bool": {"filter": [{"term": {"kb_id": KB_ID}}]}},
            "size": 0,
            "aggs": {"kg_types": {"terms": {"field": "knowledge_graph_kwd", "size": 10}}},
        }
        has_subgraph = False
        try:
            diag_resp = requests.post(search_url, json=diag_query, auth=auth, timeout=30)
            if diag_resp.status_code == 200:
                diag_result = diag_resp.json()
                buckets = diag_result.get("aggregations", {}).get("kg_types", {}).get("buckets", [])
                if buckets:
                    logger.info("诊断结果：该知识库在 OpenSearch 中的数据分布如下：")
                    for b in buckets:
                        logger.info("  - %s: %s 条", b["key"], b["doc_count"])
                        if b["key"] == "subgraph":
                            has_subgraph = True
                else:
                    logger.error("诊断结果：该 kb_id 在 OpenSearch 中无任何文档。")
                    return None
            else:
                logger.error("诊断查询失败: HTTP %s, %s", diag_resp.status_code, diag_resp.text)
                return None
        except Exception as exc:
            logger.error("诊断查询异常: %s", exc)
            return None

        # ---- Fallback：从 subgraph 文档重建完整图 ----
        if not has_subgraph:
            logger.error("该知识库既没有 graph 文档，也没有 subgraph 文档，无法重建。")
            return None

        subgraph_query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"kb_id": KB_ID}},
                        {"term": {"knowledge_graph_kwd": "subgraph"}},
                    ]
                }
            },
            "size": 1000,
        }
        logger.info("正在从 subgraph 文档重建完整知识图谱...")
        try:
            sg_resp = requests.post(search_url, json=subgraph_query, auth=auth, timeout=60)
            sg_resp.raise_for_status()
            sg_result = sg_resp.json()
        except Exception as exc:
            logger.error("查询 subgraph 文档失败: %s", exc)
            return None

        sg_hits = sg_result.get("hits", {}).get("hits", [])
        if not sg_hits:
            logger.error("subgraph 查询返回空结果，无法重建。")
            return None

        merged = nx.Graph()
        for idx, hit in enumerate(sg_hits, 1):
            try:
                sg_data = json.loads(hit["_source"]["content_with_weight"])
                source_ids = hit["_source"].get("source_id", [])
                sg_graph = nx.node_link_graph(sg_data, edges="edges")
                logger.info(
                    "  Subgraph %s/%s: source_id=%s, nodes=%s, edges=%s",
                    idx, len(sg_hits), source_ids,
                    sg_graph.number_of_nodes(), sg_graph.number_of_edges(),
                )
                merged = nx.compose(merged, sg_graph)
            except Exception as exc:
                logger.warning("  Subgraph %s/%s 解析失败，已跳过: %s", idx, len(sg_hits), exc)
                continue

        if merged.number_of_nodes() == 0:
            logger.error("所有 subgraph 文档解析后节点数为 0，重建失败。")
            return None

        graph_data = nx.node_link_data(merged, edges="edges")
        node_count = len(graph_data.get("nodes", []))
        edge_count = len(graph_data.get("edges", []))
        logger.info(
            "成功从 %s 条 subgraph 重建完整知识图谱: %s 个节点, %s 条边",
            len(sg_hits), node_count, edge_count,
        )
        return {"graph": graph_data}

    # ---- 3. 解析 content_with_weight 中的图数据 ----
    try:
        source = hits[0]["_source"]
        graph_data = json.loads(source["content_with_weight"])
    except Exception as exc:
        logger.error("解析 graph 文档 content_with_weight 失败: %s", exc)
        return None

    node_count = len(graph_data.get("nodes", []))
    edge_count = len(graph_data.get("edges", []))
    logger.info(
        "成功从 OpenSearch 获取完整知识图谱: %s 个节点, %s 条边",
        node_count, edge_count,
    )

    return {"graph": graph_data}


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

    # 构建 NetworkX 图
    try:
        G = nx.node_link_graph(graph_info)
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
