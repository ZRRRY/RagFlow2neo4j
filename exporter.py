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
