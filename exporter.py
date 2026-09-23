# exporter.py
import csv
import json
import logging
import math
import os
import queue
import shutil
import threading
import time

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

# scroll 只需 content_with_weight 字段，避免传输向量等大字段浪费带宽
_SCROLL_SOURCE_FILTER = ["content_with_weight"]

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


def _get_scroll_session():
    """创建用于 scroll/计数等长连接的 Session。

    在 _get_session() 的重试机制之上，显式声明 gzip 压缩支持；
    每个工作线程持有自己的 Session（requests.Session 非线程安全），
    以实现 keep-alive 连接复用。
    """
    session = _get_session()
    session.headers.update({"Accept-Encoding": "gzip"})
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


def _count_es_docs(count_url, query, auth, session=None):
    """使用搜索引擎的 _count API 获取符合条件的文档总数。

    OpenSearch 与 Elasticsearch 的 _count 接口兼容，因此本函数通用。
    可传入 session 复用 keep-alive 连接；未传入时内部创建。
    """
    session = session or _get_scroll_session()
    try:
        resp = session.post(count_url, json=query, auth=auth, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        return result.get("count", 0)
    except Exception as exc:
        logger.error("搜索引擎计数请求异常: %s", exc)
        return None


def _scroll_search_batches(search_url, query, auth, scheme, host, port,
                           batch_size=5000, session=None, slice_id=0, slice_max=1,
                           scroll_timeout=300):
    """使用搜索引擎 scroll API 逐批次 yield hits 列表。

    OpenSearch 与 Elasticsearch 的 scroll 接口兼容，因此本函数通用。
    初始请求只取 content_with_weight 字段并按 _doc 排序（scroll 场景最快方式）；
    slice_max > 1 时注入 slice 参数实现 sliced scroll 水平并行。
    初始请求（尚无游标，幂等安全）对连接/超时错误最多重试 2 次，间隔 2 秒；
    翻页请求绝不重试——服务端游标已前进，用旧 scroll_id 重试会静默丢失一批数据。
    无论正常结束还是提前关闭，都会清理服务端的 scroll 上下文。
    """
    session = session or _get_scroll_session()
    scroll_id = None
    scroll_search_url = f"{search_url}?scroll=2m"

    init_query = {
        **query,
        "size": batch_size,
        "_source": _SCROLL_SOURCE_FILTER,
        "sort": ["_doc"],
    }
    if slice_max > 1:
        init_query["slice"] = {"id": slice_id, "max": slice_max}

    try:
        init_resp = None
        for attempt in range(3):
            try:
                init_resp = session.post(
                    scroll_search_url,
                    json=init_query,
                    auth=auth,
                    timeout=scroll_timeout,
                )
                init_resp.raise_for_status()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt >= 2:
                    raise
                logger.warning("scroll 初始请求失败（第 %s/3 次尝试）: %s，2 秒后重试...", attempt + 1, exc)
                time.sleep(2)
        init_result = init_resp.json()
        scroll_id = init_result.get("_scroll_id")
        hits = init_result.get("hits", {}).get("hits", [])
        if hits:
            yield hits

        while len(hits) > 0:
            scroll_resp = session.post(
                f"{scheme}://{host}:{port}/_search/scroll",
                json={"scroll": "2m", "scroll_id": scroll_id},
                auth=auth,
                timeout=scroll_timeout,
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
                session.delete(
                    f"{scheme}://{host}:{port}/_search/scroll",
                    json={"scroll_id": [scroll_id]},
                    auth=auth,
                    timeout=30,
                )
            except Exception:
                pass


def _prefetch_batches(batch_iter, max_queue=2):
    """包装批次生成器，后台线程预取后续批次，消费端从队列取。

    - 生产者异常通过队列传递，消费端取到时重新抛出；
    - 消费端提前退出（异常/GeneratorExit）时通知生产者停止，
      并关闭底层生成器以触发其 scroll 上下文清理；
    - 生产者线程为 daemon，避免进程退出时悬挂。
    """
    batch_queue = queue.Queue(maxsize=max_queue)
    stop_event = threading.Event()
    done_sentinel = object()

    def producer():
        def put(item):
            # 带超时的 put，消费端退出后能及时响应 stop_event
            while not stop_event.is_set():
                try:
                    batch_queue.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        try:
            for batch in batch_iter:
                if stop_event.is_set():
                    break
                if not put(batch):
                    break
        except Exception as exc:
            put(exc)
        finally:
            if stop_event.is_set():
                # 消费端已提前退出：关闭底层生成器，触发其 finally（清理 scroll）
                close = getattr(batch_iter, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
            put(done_sentinel)

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()

    try:
        while True:
            item = batch_queue.get()
            if item is done_sentinel:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stop_event.set()
        thread.join(timeout=5)


def _get_slice_count(base_url, index_name, auth, session, max_workers):
    """根据索引分片数决定 sliced scroll 的并行度。

    GET {base_url}/{index_name}/_search_shards 取分片数，
    返回 max(1, min(分片数, max_workers))；请求失败时回退为 1。
    """
    try:
        resp = session.get(f"{base_url}/{index_name}/_search_shards", auth=auth, timeout=30)
        resp.raise_for_status()
        shard_count = len(resp.json().get("shards", []))
        if shard_count < 1:
            shard_count = 1
        return max(1, min(shard_count, max_workers))
    except Exception as exc:
        logger.warning("获取索引分片数失败，回退为单线程 scroll: %s", exc)
        return 1


def _hit_to_node_row(hit):
    """将实体文档 hit 转换为节点 CSV 行；缺少 entity_name 时返回 None。"""
    source = hit.get("_source", {})
    content = {}
    try:
        content = json.loads(source.get("content_with_weight", "{}"))
    except (json.JSONDecodeError, TypeError):
        pass

    entity_name = content.get("entity_name")
    if not entity_name:
        return None

    row = {
        "id": entity_name,
        "entity_type": content.get("entity_type", ""),
        "description": content.get("description", ""),
        "source_id": content.get("source_id", []),
        "pagerank": content.get("pagerank", ""),
        "rank": content.get("rank", ""),
    }
    return {k: _escape_csv_injection(_safe_serialize(v)) for k, v in row.items()}


def _hit_to_edge_row(hit):
    """将关系文档 hit 转换为边 CSV 行；缺少 src_id/tgt_id 时返回 None。"""
    source = hit.get("_source", {})
    content = {}
    try:
        content = json.loads(source.get("content_with_weight", "{}"))
    except (json.JSONDecodeError, TypeError):
        pass

    from_entity = content.get("src_id")
    to_entity = content.get("tgt_id")
    if not from_entity or not to_entity:
        return None

    row = {
        "source": from_entity,
        "target": to_entity,
        "description": content.get("description", ""),
        "keywords": content.get("keywords", []),
        "weight": content.get("weight", ""),
        "source_id": content.get("source_id", []),
    }
    return {k: _escape_csv_injection(_safe_serialize(v)) for k, v in row.items()}


def _export_phase(phase_label, search_url, base_url, index_name, query,
                  columns, hit_to_row, total, auth, scheme, host, port,
                  final_path, batch_size, slice_workers, scroll_timeout=300):
    """执行一个导出阶段（节点或边），返回 (是否成功, 写入行数)。

    启动 num_slices 个工作线程，每个线程持有自己的 Session，
    通过 sliced scroll + 预取流水线拉取数据并写入独立临时文件
    （{final_path}.slice{i}.tmp，纯 utf-8、无表头）；
    全部成功后合并为 utf-8-sig 的正式文件（BOM 与表头只出现一次），
    并删除临时文件。任一线程失败则清理临时文件并返回失败。
    注意：多 slice 并行时行顺序不再保证有序。
    """
    ctrl_session = _get_scroll_session()
    num_slices = _get_slice_count(base_url, index_name, auth, ctrl_session, slice_workers)
    logger.info("开始流式导出%s CSV (总数 %s, slice 数 %s)...", phase_label, total, num_slices)

    temp_paths = [f"{final_path}.slice{i}.tmp" for i in range(num_slices)]
    counter_lock = threading.Lock()
    written_counter = [0]
    errors = []

    def worker(slice_id):
        session = _get_scroll_session()
        try:
            batches = _scroll_search_batches(
                search_url, query, auth, scheme, host, port,
                batch_size=batch_size, session=session,
                slice_id=slice_id, slice_max=num_slices,
                scroll_timeout=scroll_timeout,
            )
            with open(temp_paths[slice_id], "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for batch in _prefetch_batches(batches):
                    rows = []
                    for hit in batch:
                        row = hit_to_row(hit)
                        if row is not None:
                            rows.append([row[c] for c in columns])
                    if rows:
                        writer.writerows(rows)
                        with counter_lock:
                            written_counter[0] += len(rows)
                            logger.info(
                                "%s写入进度: 累计 %s/%s 条",
                                phase_label, written_counter[0], total,
                            )
        except Exception as exc:
            logger.error("%s导出线程 (slice %s) 异常: %s", phase_label, slice_id, exc)
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(num_slices)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        logger.error("%s导出阶段失败 (%s 个线程出错)，清理临时文件。", phase_label, len(errors))
        for tmp in temp_paths:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return False, written_counter[0]

    # 合并临时文件：先以 utf-8-sig 写表头（BOM 仅此处出现一次），
    # 再以二进制方式追加各临时文件的 utf-8 字节流
    with open(final_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(columns)
    with open(final_path, "ab") as out:
        for tmp in temp_paths:
            with open(tmp, "rb") as src:
                shutil.copyfileobj(src, out)
            os.remove(tmp)

    logger.info("%s导出完成: 共 %s 条，文件: %s", phase_label, written_counter[0], final_path)
    return True, written_counter[0]


def export_graph_direct(kb_id=None, output_dir=None, output_prefix=None, batch_size=5000,
                        engine="opensearch", slice_workers=4, parallel_phases=True,
                        scroll_timeout=300, phases=("nodes", "edges")):
    """绕过 /graph/export API，直接从搜索引擎流式读取并导出 CSV。

    参数:
        batch_size: scroll 每批次文档数，默认 5000。
        engine: "opensearch" 或 "elasticsearch"，决定读取哪个配置。
        slice_workers: sliced scroll 的最大并行线程数（实际取 min(分片数, 该值)）。
        parallel_phases: 为 True 时节点与边两个阶段并行执行。
        scroll_timeout: scroll 单次请求（含初始与翻页）的读超时秒数，默认 300。
        phases: 要导出的阶段，"nodes" / "edges" 的任意非空子集，默认两者都导出。
                未选中的阶段完全跳过：不统计、不删除对应旧 CSV、不导出。
                例如上次关系导出成功而节点失败时，可用 phases=("nodes",) 重跑，
                已有的 edges CSV 不会被删除。

    流程：
      1. 通过 RAGFlow Dataset API 获取 tenant_id；
      2. 使用搜索引擎 _count API 统计选中阶段的实体/关系总数；
      3. 各选中阶段（数量为 0 的阶段自动跳过）执行 sliced scroll 并行导出，
         每个 slice 线程拉取数据写入临时文件，最后合并为带 BOM 的 CSV。
    进度日志以「累计/总数」形式打印。
    注意：多 slice 并行导出时，CSV 中的行顺序不再保证有序。
    提示：遇到 scroll 读超时（常见于资源受限的内置 ES）时，可增大 scroll_timeout，
    或调小 batch_size / slice_workers 以降低服务端压力。
    """
    if engine not in ("opensearch", "elasticsearch"):
        logger.error("不支持的搜索引擎类型: %s，仅支持 opensearch 或 elasticsearch", engine)
        return False

    _VALID_PHASES = ("nodes", "edges")
    if not phases or any(p not in _VALID_PHASES for p in phases):
        logger.error("非法的 phases 参数: %s，仅支持 'nodes' 与 'edges' 的非空子集", phases)
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
    base_url = f"{scheme}://{host}:{port}"
    search_url = f"{base_url}/{index_name}/_search"
    count_url = f"{base_url}/{index_name}/_count"
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

    # ---- 先统计选中阶段的文档数量 ----
    logger.info("正在统计 %s 中文档数量 (阶段: %s)...", engine_label, ", ".join(phases))
    count_session = _get_scroll_session()
    entity_total = relation_total = None
    if "nodes" in phases:
        entity_total = _count_es_docs(count_url, entity_query, auth, session=count_session)
        if entity_total is None:
            logger.error("统计实体文档数量失败，导出终止。")
            return False
    if "edges" in phases:
        relation_total = _count_es_docs(count_url, relation_query, auth, session=count_session)
        if relation_total is None:
            logger.error("统计关系文档数量失败，导出终止。")
            return False

    stats = []
    if entity_total is not None:
        stats.append(f"实体 {entity_total} 个")
    if relation_total is not None:
        stats.append(f"关系 {relation_total} 个")
    logger.info("%s 统计结果: %s", engine_label, ", ".join(stats))

    if (entity_total or 0) == 0 and (relation_total or 0) == 0:
        logger.error("该知识库在 %s 中没有任何选中的实体或关系文档，导出终止。", engine_label)
        return False

    # ---- 准备 CSV 路径 ----
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    nodes_csv = os.path.join(output_dir, f"{base_name}_{kb_id}_nodes.csv")
    edges_csv = os.path.join(output_dir, f"{base_name}_{kb_id}_edges.csv")

    # 仅删除选中阶段对应的旧文件，避免误删未选中阶段的已有数据
    if "nodes" in phases and os.path.exists(nodes_csv):
        os.remove(nodes_csv)
    if "edges" in phases and os.path.exists(edges_csv):
        os.remove(edges_csv)

    # ---- 分阶段导出（数量为 0 的阶段跳过） ----
    phase_results = {}

    def run_phase(key, label, query, columns, hit_to_row, total, final_path):
        phase_results[key] = _export_phase(
            label, search_url, base_url, index_name, query,
            columns, hit_to_row, total, auth, scheme, host, port,
            final_path, batch_size, slice_workers, scroll_timeout,
        )

    tasks = []
    if entity_total:
        tasks.append(("节点", entity_query, _DIRECT_NODE_COLUMNS, _hit_to_node_row, entity_total, nodes_csv))
    if relation_total:
        tasks.append(("关系", relation_query, _DIRECT_EDGE_COLUMNS, _hit_to_edge_row, relation_total, edges_csv))

    if parallel_phases and len(tasks) > 1:
        threads = [
            threading.Thread(
                target=run_phase,
                args=(label, label, query, columns, hit_to_row, total, final_path),
                daemon=True,
            )
            for label, query, columns, hit_to_row, total, final_path in tasks
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for label, query, columns, hit_to_row, total, final_path in tasks:
            run_phase(label, label, query, columns, hit_to_row, total, final_path)

    if not all(ok for ok, _ in phase_results.values()):
        logger.error("%s 流式导出存在失败阶段，导出终止。", engine_label)
        return False

    logger.info(
        "%s 流式导出完成: %s。CSV: %s",
        engine_label,
        ", ".join(f"{label} {phase_results[label][1]} 条" for label, *_ in tasks),
        ", ".join(path for *_, path in tasks),
    )
    return True


def export_graph_direct_elasticsearch(kb_id=None, output_dir=None, output_prefix=None,
                                      batch_size=5000, slice_workers=4, parallel_phases=True,
                                      scroll_timeout=300, phases=("nodes", "edges")):
    """从 Elasticsearch 流式导出 CSV 的便捷函数。"""
    return export_graph_direct(
        kb_id=kb_id,
        output_dir=output_dir,
        output_prefix=output_prefix,
        batch_size=batch_size,
        engine="elasticsearch",
        slice_workers=slice_workers,
        parallel_phases=parallel_phases,
        scroll_timeout=scroll_timeout,
        phases=phases,
    )
