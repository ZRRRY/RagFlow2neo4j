import os
import sys
from unittest import mock

# 在导入 exporter / config 之前注入 mock config，避免硬编码校验失败
mock_config = mock.MagicMock()
mock_config.RAGFLOW_API_KEY = "test_api_key"
mock_config.KB_ID = "test_kb_id"
mock_config.RAGFLOW_BASE_URL = "http://localhost:9380"
mock_config.RAGFLOW_REQUEST_TIMEOUT = 120
mock_config.OUTPUT_PREFIX = "test_output"
mock_config.OUTPUT_DIR = ""
mock_config.NEO4J_URI = "bolt://localhost:7687"
mock_config.NEO4J_USER = "neo4j"
mock_config.NEO4J_PASSWORD = "test"
mock_config.NEO4J_DATABASE = "neo4j"
mock_config.OPENSEARCH_HOST = "localhost"
mock_config.OPENSEARCH_PORT = 9201
mock_config.OPENSEARCH_USER = "admin"
mock_config.OPENSEARCH_PASSWORD = "test"
mock_config.OPENSEARCH_USE_SSL = False
mock_config.ELASTICSEARCH_HOST = "localhost"
mock_config.ELASTICSEARCH_PORT = 9200
mock_config.ELASTICSEARCH_USER = ""
mock_config.ELASTICSEARCH_PASSWORD = ""
mock_config.ELASTICSEARCH_USE_SSL = False
sys.modules["config"] = mock_config

import json
import threading

import pandas as pd
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import exporter


def _make_mock_session(post_side_effect=None, get_return=None):
    """构造一个 mock Session，post 按 side_effect 返回，delete/get 默认成功。"""
    session = mock.Mock()
    if post_side_effect is not None:
        session.post.side_effect = post_side_effect
    session.delete.return_value = mock.Mock()
    if get_return is not None:
        session.get.return_value = get_return
    return session


def _scroll_response(scroll_id, hits):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    resp.json.return_value = {"_scroll_id": scroll_id, "hits": {"hits": hits}}
    return resp


class TestSafeSerialize:
    def test_none_becomes_empty_string(self):
        assert exporter._safe_serialize(None) == ""

    def test_nan_and_inf_become_empty_string(self):
        assert exporter._safe_serialize(float("nan")) == ""
        assert exporter._safe_serialize(float("inf")) == ""
        assert exporter._safe_serialize(float("-inf")) == ""

    def test_scalar_types_unchanged(self):
        assert exporter._safe_serialize("hello") == "hello"
        assert exporter._safe_serialize(42) == 42
        assert exporter._safe_serialize(True) is True

    def test_list_and_dict_become_json(self):
        assert json.loads(exporter._safe_serialize([1, 2, 3])) == [1, 2, 3]
        assert json.loads(exporter._safe_serialize({"k": "v"})) == {"k": "v"}


class TestEscapeCsvInjection:
    def test_escape_dangerous_prefixes(self):
        assert exporter._escape_csv_injection("=SUM(A1:A10)") == "'=SUM(A1:A10)"
        assert exporter._escape_csv_injection("+1") == "'+1"
        assert exporter._escape_csv_injection("-1") == "'-1"
        assert exporter._escape_csv_injection("@test") == "'@test"

    def test_normal_strings_unchanged(self):
        assert exporter._escape_csv_injection("hello") == "hello"
        assert exporter._escape_csv_injection(123) == 123


class TestCountEsDocs:
    def test_success(self):
        session = _make_mock_session()
        session.post.return_value = mock.Mock(
            status_code=200,
            json=mock.Mock(return_value={"count": 42}),
            raise_for_status=mock.Mock(),
        )
        result = exporter._count_es_docs(
            "http://es:9200/i/_count", {"query": {"match_all": {}}}, None, session=session
        )
        assert result == 42
        session.post.assert_called_once()

    def test_exception(self):
        session = _make_mock_session()
        session.post.side_effect = requests.exceptions.ConnectionError("refused")
        result = exporter._count_es_docs(
            "http://es:9200/i/_count", {"query": {"match_all": {}}}, None, session=session
        )
        assert result is None


class TestScrollSearchBatches:
    def test_scroll_search_batches(self):
        """验证 _scroll_search_batches 的生成器逻辑与 scroll 上下文清理"""
        session = _make_mock_session(post_side_effect=[
            _scroll_response("scroll-1", [{"_id": "1"}, {"_id": "2"}]),
            _scroll_response("scroll-1", [{"_id": "3"}]),
            _scroll_response("scroll-1", []),
        ])

        batches = list(exporter._scroll_search_batches(
            "http://localhost:9200/index/_search",
            {"query": {"match_all": {}}},
            None,
            "http",
            "localhost",
            9200,
            session=session,
        ))
        all_hits = [hit for batch in batches for hit in batch]
        assert len(all_hits) == 3
        assert [h["_id"] for h in all_hits] == ["1", "2", "3"]
        assert session.post.call_count == 3
        session.delete.assert_called_once()

    def test_init_request_source_filter_and_doc_sort(self):
        """scroll 初始请求应包含 _source 过滤与 sort: ["_doc"]"""
        session = _make_mock_session(post_side_effect=[
            _scroll_response("scroll-1", []),
        ])

        list(exporter._scroll_search_batches(
            "http://localhost:9200/index/_search",
            {"query": {"match_all": {}}},
            None,
            "http",
            "localhost",
            9200,
            session=session,
        ))

        init_body = session.post.call_args_list[0].kwargs["json"]
        assert init_body["_source"] == ["content_with_weight"]
        assert init_body["sort"] == ["_doc"]
        assert "slice" not in init_body

    def test_slice_injection_only_when_max_gt_1(self):
        """slice_max > 1 时注入 slice 参数，等于 1 时不注入"""
        for slice_max, expect_slice in ((1, False), (3, True)):
            session = _make_mock_session(post_side_effect=[
                _scroll_response("scroll-1", []),
            ])
            list(exporter._scroll_search_batches(
                "http://localhost:9200/index/_search",
                {"query": {"match_all": {}}},
                None,
                "http",
                "localhost",
                9200,
                session=session,
                slice_id=1,
                slice_max=slice_max,
            ))
            init_body = session.post.call_args_list[0].kwargs["json"]
            if expect_slice:
                assert init_body["slice"] == {"id": 1, "max": 3}
            else:
                assert "slice" not in init_body

    @mock.patch("exporter.time.sleep")
    def test_init_request_retry_succeeds(self, mock_sleep):
        """初始请求连接/超时错误重试后成功（重试幂等安全）"""
        session = _make_mock_session(post_side_effect=[
            requests.exceptions.ConnectionError("conn reset"),
            requests.exceptions.ReadTimeout("read timed out"),
            _scroll_response("scroll-1", [{"_id": "1"}]),
            _scroll_response("scroll-1", []),
        ])

        batches = list(exporter._scroll_search_batches(
            "http://localhost:9200/index/_search",
            {"query": {"match_all": {}}},
            None, "http", "localhost", 9200,
            session=session,
        ))
        assert [h["_id"] for b in batches for h in b] == ["1"]
        assert session.post.call_count == 4
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(2)

    @mock.patch("exporter.time.sleep")
    def test_init_request_retry_exhausted_raises(self, mock_sleep):
        """初始请求重试耗尽后向上抛错"""
        session = _make_mock_session(post_side_effect=requests.exceptions.ReadTimeout("read timed out"))

        with pytest.raises(requests.exceptions.ReadTimeout):
            list(exporter._scroll_search_batches(
                "http://localhost:9200/index/_search",
                {"query": {"match_all": {}}},
                None, "http", "localhost", 9200,
                session=session,
            ))
        assert session.post.call_count == 3
        assert mock_sleep.call_count == 2
        # 初始请求从未成功，不应产生 scroll 上下文，无需清理
        session.delete.assert_not_called()

    def test_pagination_request_never_retried(self):
        """翻页请求不重试：用旧 scroll_id 重试会静默跳批丢数据"""
        session = _make_mock_session(post_side_effect=[
            _scroll_response("scroll-1", [{"_id": "1"}]),
            requests.exceptions.ReadTimeout("read timed out"),
        ])

        with pytest.raises(requests.exceptions.ReadTimeout):
            list(exporter._scroll_search_batches(
                "http://localhost:9200/index/_search",
                {"query": {"match_all": {}}},
                None, "http", "localhost", 9200,
                session=session,
            ))
        # 仅初始请求 + 一次翻页尝试，不重试
        assert session.post.call_count == 2
        # 已持有 scroll_id，失败后仍应清理 scroll 上下文
        session.delete.assert_called_once()


class TestPrefetchBatches:
    def test_order_preserved(self):
        """预取不应打乱批次顺序"""
        source = iter([[1], [2], [3], [4], [5]])
        result = list(exporter._prefetch_batches(source, max_queue=2))
        assert result == [[1], [2], [3], [4], [5]]

    def test_exception_propagates(self):
        """生产者异常应通过队列传递并在消费端重新抛出"""
        def gen():
            yield [1]
            yield [2]
            raise RuntimeError("scroll failed")

        collected = []
        with pytest.raises(RuntimeError, match="scroll failed"):
            for batch in exporter._prefetch_batches(gen()):
                collected.append(batch)
        assert collected == [[1], [2]]

    def test_early_exit_closes_source_generator(self):
        """消费端提前退出时应关闭底层生成器（触发其 finally 清理）"""
        closed = []

        def gen():
            try:
                for i in range(100):
                    yield [i]
            finally:
                closed.append(True)

        prefetch = exporter._prefetch_batches(gen())
        next(prefetch)
        prefetch.close()  # 模拟消费端 GeneratorExit
        # 等待生产者线程响应停止事件
        for _ in range(100):
            if closed:
                break
            threading.Event().wait(0.05)
        assert closed == [True]


class TestGetSliceCount:
    def test_uses_min_of_shards_and_workers(self):
        session = _make_mock_session()
        session.get.return_value = mock.Mock(
            raise_for_status=mock.Mock(),
            json=mock.Mock(return_value={"shards": [[], [], []]}),
        )
        assert exporter._get_slice_count("http://es:9200", "idx", None, session, 4) == 3
        assert exporter._get_slice_count("http://es:9200", "idx", None, session, 2) == 2

    def test_fallback_on_failure(self):
        session = _make_mock_session()
        session.get.side_effect = requests.exceptions.ConnectionError("refused")
        assert exporter._get_slice_count("http://es:9200", "idx", None, session, 4) == 1


class TestExportGraphDirect:
    """测试 OpenSearch 与 Elasticsearch 的流式导出。"""

    def _csv_paths(self, prefix, kb_id="test_kb"):
        return f"{prefix}_{kb_id}_nodes.csv", f"{prefix}_{kb_id}_edges.csv"

    def _make_scroll_mock(self, entity_batches, relation_batches):
        """构造 _scroll_search_batches 的 side_effect（按查询中的 knowledge_graph_kwd 区分）"""
        def side_effect(search_url, query, auth, scheme, host, port, **kwargs):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            if kg_kwd == "entity":
                return iter([list(b) for b in entity_batches])
            else:
                return iter([list(b) for b in relation_batches])
        return side_effect

    _ENTITY_BATCHES = [
        [
            {"_source": {"content_with_weight": json.dumps({"entity_name": "A", "entity_type": "T", "description": "dA", "source_id": ["doc1"], "pagerank": 0.1, "rank": 1})}},
            {"_source": {"content_with_weight": json.dumps({"entity_name": "B", "entity_type": "T", "description": "dB", "source_id": ["doc2"], "pagerank": 0.2, "rank": 2})}},
        ]
    ]
    _RELATION_BATCHES = [
        [
            {"_source": {"content_with_weight": json.dumps({"src_id": "A", "tgt_id": "B", "description": "", "keywords": ["k1"], "weight": 1.0, "source_id": ["doc1"]})}},
        ]
    ]

    @pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
    @mock.patch("exporter._get_slice_count", return_value=1)
    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_success(self, mock_tenant, mock_count, mock_scroll, mock_slices, engine, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth, session=None: (
            2 if query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"] == "entity" else 1
        )
        mock_scroll.side_effect = self._make_scroll_mock(self._ENTITY_BATCHES, self._RELATION_BATCHES)

        prefix = str(tmp_path / engine)
        result = exporter.export_graph_direct(kb_id="test_kb", output_prefix=prefix, engine=engine)
        assert result is True
        nodes_file, edges_file = self._csv_paths(prefix)
        assert os.path.exists(nodes_file)
        assert os.path.exists(edges_file)
        nodes_df = pd.read_csv(nodes_file)
        edges_df = pd.read_csv(edges_file)
        assert len(nodes_df) == 2
        assert len(edges_df) == 1
        assert set(nodes_df["id"].tolist()) == {"A", "B"}
        assert edges_df.iloc[0]["source"] == "A"
        assert edges_df.iloc[0]["target"] == "B"
        # 临时文件应已被清理
        assert not list(tmp_path.glob("*.tmp"))

    @mock.patch("exporter._get_slice_count", return_value=2)
    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_multi_slice_merge_single_bom_and_header(self, mock_tenant, mock_count, mock_scroll, mock_slices, tmp_path):
        """多 slice 合并后 BOM 与表头只出现一次，且各 slice 数据均保留"""
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth, session=None: 2

        def scroll_side_effect(search_url, query, auth, scheme, host, port, **kwargs):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            slice_id = kwargs.get("slice_id", 0)
            if kg_kwd == "entity":
                name = f"E{slice_id}"
                hit = {"_source": {"content_with_weight": json.dumps({"entity_name": name, "entity_type": "T"})}}
            else:
                hit = {"_source": {"content_with_weight": json.dumps({"src_id": f"S{slice_id}", "tgt_id": f"T{slice_id}"})}}
            return iter([[hit]])

        mock_scroll.side_effect = scroll_side_effect

        prefix = str(tmp_path / "multi")
        result = exporter.export_graph_direct(kb_id="test_kb", output_prefix=prefix, engine="opensearch")
        assert result is True

        nodes_file, edges_file = self._csv_paths(prefix)
        for path in (nodes_file, edges_file):
            data = open(path, "rb").read()
            # BOM 只在文件开头出现一次
            assert data.startswith(b"\xef\xbb\xbf")
            assert data.count(b"\xef\xbb\xbf") == 1
            # 表头只出现一次
            text = data.decode("utf-8-sig")
            assert text.count("description") == 1
            assert not list(tmp_path.glob("*.tmp"))

        nodes_df = pd.read_csv(nodes_file)
        assert set(nodes_df["id"].tolist()) == {"E0", "E1"}
        edges_df = pd.read_csv(edges_file)
        assert set(edges_df["source"].tolist()) == {"S0", "S1"}

    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_count_failure(self, mock_tenant, mock_count, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.return_value = None
        result = exporter.export_graph_direct(kb_id="test_kb")
        assert result is False

    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_empty_results(self, mock_tenant, mock_count, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.return_value = 0
        result = exporter.export_graph_direct(kb_id="test_kb")
        assert result is False

    @mock.patch("exporter._get_tenant_id")
    def test_tenant_failure(self, mock_tenant, tmp_path):
        mock_tenant.return_value = None
        result = exporter.export_graph_direct(kb_id="test_kb")
        assert result is False

    @mock.patch("exporter._export_phase")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_zero_total_phase_skipped(self, mock_tenant, mock_count, mock_phase, tmp_path):
        """关系总数为 0 时跳过边导出阶段，结果取决于节点阶段"""
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth, session=None: (
            2 if query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"] == "entity" else 0
        )
        mock_phase.return_value = (True, 2)
        prefix = str(tmp_path / "skip")
        result = exporter.export_graph_direct(kb_id="test_kb", output_prefix=prefix)
        assert result is True
        assert mock_phase.call_count == 1

    @mock.patch("exporter._export_phase")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_phase_failure_returns_false(self, mock_tenant, mock_count, mock_phase, tmp_path):
        """任一阶段失败则整体返回 False"""
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth, session=None: 2
        mock_phase.return_value = (False, 0)
        prefix = str(tmp_path / "fail")
        result = exporter.export_graph_direct(kb_id="test_kb", output_prefix=prefix)
        assert result is False

    @mock.patch("exporter._export_phase")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_phases_nodes_only_preserves_edges(self, mock_tenant, mock_count, mock_phase, tmp_path):
        """只导 nodes：不统计/不导出 edges，已有的 edges CSV 不被删除"""
        mock_tenant.return_value = "t-123"
        counted = []

        def count_side_effect(count_url, query, auth, session=None):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            counted.append(kg_kwd)
            return 2 if kg_kwd == "entity" else 1

        mock_count.side_effect = count_side_effect
        mock_phase.return_value = (True, 2)

        prefix = str(tmp_path / "phases")
        _, edges_file = self._csv_paths(prefix)
        with open(edges_file, "w", encoding="utf-8") as f:
            f.write("existing,good,data\n")

        result = exporter.export_graph_direct(
            kb_id="test_kb", output_prefix=prefix, phases=("nodes",),
        )
        assert result is True
        # 只统计实体，不统计关系
        assert counted == ["entity"]
        # 只执行节点阶段
        assert mock_phase.call_count == 1
        # 已有 edges CSV 原样保留
        with open(edges_file, "r", encoding="utf-8") as f:
            assert f.read() == "existing,good,data\n"

    @mock.patch("exporter._export_phase")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_phases_edges_only(self, mock_tenant, mock_count, mock_phase, tmp_path):
        """只导 edges：不统计/不导出 nodes，已有的 nodes CSV 不被删除"""
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth, session=None: (
            2 if query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"] == "entity" else 1
        )
        mock_phase.return_value = (True, 1)

        prefix = str(tmp_path / "phases_edges")
        nodes_file, _ = self._csv_paths(prefix)
        with open(nodes_file, "w", encoding="utf-8") as f:
            f.write("existing,nodes\n")

        result = exporter.export_graph_direct(
            kb_id="test_kb", output_prefix=prefix, phases=("edges",),
        )
        assert result is True
        assert mock_phase.call_count == 1
        with open(nodes_file, "r", encoding="utf-8") as f:
            assert f.read() == "existing,nodes\n"

    def test_invalid_phases(self):
        """非法 phases 值直接报错返回 False"""
        with mock.patch("exporter.logger") as mock_logger:
            assert exporter.export_graph_direct(kb_id="test_kb", phases=("bogus",)) is False
            assert exporter.export_graph_direct(kb_id="test_kb", phases=()) is False
            mock_logger.error.assert_called()

    @mock.patch("exporter.export_graph_direct")
    def test_elasticsearch_alias(self, mock_export):
        mock_export.return_value = True
        result = exporter.export_graph_direct_elasticsearch(kb_id="alias_kb", output_prefix="/tmp/alias")
        assert result is True
        mock_export.assert_called_once_with(
            kb_id="alias_kb",
            output_dir=None,
            output_prefix="/tmp/alias",
            batch_size=5000,
            engine="elasticsearch",
            slice_workers=4,
            parallel_phases=True,
            scroll_timeout=300,
            phases=("nodes", "edges"),
        )

    def test_invalid_engine(self):
        with mock.patch("exporter.logger") as mock_logger:
            result = exporter.export_graph_direct(kb_id="test_kb", engine="solr")
            assert result is False
            mock_logger.error.assert_called()
