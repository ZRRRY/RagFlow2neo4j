import os
import sys
from unittest import mock

# 在导入 exporter / config 之前注入 mock config，避免硬编码校验失败
mock_config = mock.MagicMock()
mock_config.RAGFLOW_API_KEY = "test_api_key"
mock_config.KB_ID = "test_kb_id"
mock_config.RAGFLOW_BASE_URL = "http://localhost:9380"
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
sys.modules["config"] = mock_config

import json
import math

import networkx as nx
import pandas as pd
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import exporter


class TestSanitizeAttrs:
    def test_none_becomes_empty_string(self):
        G = nx.Graph()
        G.add_node("a", foo=None)
        exporter.sanitize_attrs(G)
        assert G.nodes["a"]["foo"] == ""

    def test_nan_and_inf_become_empty_string(self):
        G = nx.Graph()
        G.add_node("a", x=float("nan"), y=float("inf"), z=float("-inf"))
        exporter.sanitize_attrs(G)
        assert G.nodes["a"]["x"] == ""
        assert G.nodes["a"]["y"] == ""
        assert G.nodes["a"]["z"] == ""

    def test_scalar_types_unchanged(self):
        G = nx.Graph()
        G.add_node("a", s="hello", i=42, b=True)
        exporter.sanitize_attrs(G)
        assert G.nodes["a"]["s"] == "hello"
        assert G.nodes["a"]["i"] == 42
        assert G.nodes["a"]["b"] is True

    def test_list_and_dict_become_json(self):
        G = nx.Graph()
        G.add_node("a", lst=[1, 2, 3], dct={"k": "v"})
        exporter.sanitize_attrs(G)
        assert json.loads(G.nodes["a"]["lst"]) == [1, 2, 3]
        assert json.loads(G.nodes["a"]["dct"]) == {"k": "v"}

    def test_edge_attrs_are_sanitized(self):
        G = nx.Graph()
        G.add_edge("a", "b", weight=1.5, tag=None)
        exporter.sanitize_attrs(G)
        assert G.edges["a", "b"]["weight"] == 1.5
        assert G.edges["a", "b"]["tag"] == ""

    def test_csv_injection_escaped(self):
        G = nx.Graph()
        G.add_node("a", formula="=SUM(A1:A10)", plus="+1", minus="-1", at="@test")
        exporter.sanitize_attrs(G)
        assert G.nodes["a"]["formula"] == "'=SUM(A1:A10)"
        assert G.nodes["a"]["plus"] == "'+1"
        assert G.nodes["a"]["minus"] == "'-1"
        assert G.nodes["a"]["at"] == "'@test"


class TestFetchKnowledgeGraph:
    def _mock_session(self, status_code=200, text="", json=None, side_effect=None):
        mock_resp = mock.Mock()
        mock_resp.status_code = status_code
        mock_resp.text = text
        if json is not None:
            mock_resp.json = json
        mock_session = mock.Mock()
        mock_session.get = mock.Mock(return_value=mock_resp, side_effect=side_effect)
        return mock_session

    @mock.patch("exporter._get_session")
    def test_success(self, mock_get_session):
        mock_get_session.return_value = self._mock_session(
            status_code=200,
            text='{"code":0,"data":{"graph":{"nodes":[],"edges":[]}}}',
            json=lambda: {"code": 0, "data": {"graph": {"nodes": [], "edges": []}}},
        )
        result = exporter.fetch_knowledge_graph()
        assert result == {"graph": {"nodes": [], "edges": []}}
        mock_get_session.return_value.get.assert_called_once()

    @mock.patch("exporter._get_session")
    def test_http_error(self, mock_get_session):
        mock_get_session.return_value = self._mock_session(
            status_code=500, text="Internal Server Error"
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None

    @mock.patch("exporter._get_session")
    def test_api_error_code(self, mock_get_session):
        mock_get_session.return_value = self._mock_session(
            status_code=200,
            text='{"code":-1,"message":"auth failed"}',
            json=lambda: {"code": -1, "message": "auth failed"},
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None

    @mock.patch("exporter._get_session")
    def test_json_decode_error(self, mock_get_session):
        mock_get_session.return_value = self._mock_session(
            status_code=200,
            text="not json",
            json=lambda: json.loads("not json"),
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None

    @mock.patch("exporter._get_session")
    def test_request_exception(self, mock_get_session):
        mock_get_session.return_value = self._mock_session(
            side_effect=requests.exceptions.ConnectionError("Connection refused")
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None


class TestFetchKnowledgeGraphDirect:
    def _mock_dataset_resp(self, tenant_id="test-tenant", code=0, message="", status_code=200, side_effect=None):
        """构造 RAGFlow Dataset API 的 mock 响应"""
        mock_resp = mock.Mock()
        mock_resp.status_code = status_code
        mock_resp.text = ""
        if side_effect:
            mock_resp.json = mock.Mock(side_effect=side_effect)
        else:
            mock_resp.json = mock.Mock(return_value={
                "code": code,
                "message": message,
                "data": {"tenant_id": tenant_id} if tenant_id else {}
            })
        mock_session = mock.Mock()
        mock_session.get = mock.Mock(return_value=mock_resp, side_effect=side_effect)
        return mock_session

    def _entity_hit(self, entity_kwd, entity_type_kwd="", content=None, source_id=None, doc_id=""):
        return {
            "_source": {
                "entity_kwd": entity_kwd,
                "entity_type_kwd": entity_type_kwd,
                "content_with_weight": json.dumps(content) if content else "{}",
                "source_id": source_id or [],
                "id": doc_id,
            }
        }

    def _relation_hit(self, from_entity, to_entity, content=None, source_id=None, doc_id="", weight_int=None):
        src = {
            "from_entity_kwd": from_entity,
            "to_entity_kwd": to_entity,
            "content_with_weight": json.dumps(content) if content else "{}",
            "source_id": source_id or [],
            "id": doc_id,
        }
        if weight_int is not None:
            src["weight_int"] = weight_int
        return {"_source": src}

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._get_session")
    def test_success(self, mock_get_session, mock_scroll):
        mock_get_session.return_value = self._mock_dataset_resp(tenant_id="t-123")

        def scroll_side_effect(search_url, query, auth, scheme, host, port, batch_size=1000):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            if kg_kwd == "entity":
                return [
                    [
                        self._entity_hit("ENTITY_A", "PERSON", {"description": "desc A", "rank": 3}, ["doc1"], "e1"),
                        self._entity_hit("ENTITY_B", "COMPANY", {"description": "desc B"}, ["doc2"], "e2"),
                    ]
                ]
            else:
                return [
                    [
                        self._relation_hit("ENTITY_A", "ENTITY_B", {"description": "rel desc", "weight": 1.5, "keywords": ["k1"]}, ["doc1"], "r1", 1),
                    ]
                ]

        mock_scroll.side_effect = scroll_side_effect
        result = exporter.fetch_knowledge_graph_direct()
        assert result is not None
        graph = result["graph"]
        node_ids = {n["id"] for n in graph["nodes"]}
        assert node_ids == {"ENTITY_A", "ENTITY_B"}
        assert len(graph["edges"]) == 1
        assert graph["edges"][0]["source"] == "ENTITY_A"
        assert graph["edges"][0]["target"] == "ENTITY_B"
        assert graph["edges"][0]["weight"] == 1.5

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._get_session")
    def test_dataset_http_error(self, mock_get_session, mock_scroll):
        mock_get_session.return_value = self._mock_dataset_resp(status_code=500)
        result = exporter.fetch_knowledge_graph_direct()
        assert result is None
        mock_scroll.assert_not_called()

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._get_session")
    def test_dataset_api_error_code(self, mock_get_session, mock_scroll):
        mock_get_session.return_value = self._mock_dataset_resp(code=102, message="error")
        result = exporter.fetch_knowledge_graph_direct()
        assert result is None
        mock_scroll.assert_not_called()

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._get_session")
    def test_opensearch_empty_results(self, mock_get_session, mock_scroll):
        mock_get_session.return_value = self._mock_dataset_resp(tenant_id="t-123")
        mock_scroll.return_value = []
        result = exporter.fetch_knowledge_graph_direct()
        assert result is None

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._get_session")
    def test_skip_invalid_records(self, mock_get_session, mock_scroll):
        """content_with_weight 解析失败或缺少关键字段时应跳过，不影响其他记录"""
        mock_get_session.return_value = self._mock_dataset_resp(tenant_id="t-123")

        def scroll_side_effect(search_url, query, auth, scheme, host, port, batch_size=1000):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            if kg_kwd == "entity":
                return [
                    [
                        {"_source": {"entity_kwd": "VALID_ENTITY", "content_with_weight": "bad json"}},
                        self._entity_hit("GOOD_ENTITY", "TYPE", {"description": "ok"}, [], "e2"),
                    ]
                ]
            else:
                return [
                    [
                        {"_source": {"from_entity_kwd": "", "to_entity_kwd": "GOOD_ENTITY", "content_with_weight": "{}"}},
                        self._relation_hit("VALID_ENTITY", "GOOD_ENTITY", {"weight": 2.0}, [], "r1"),
                    ]
                ]

        mock_scroll.side_effect = scroll_side_effect
        result = exporter.fetch_knowledge_graph_direct()
        assert result is not None
        graph = result["graph"]
        node_ids = {n["id"] for n in graph["nodes"]}
        assert node_ids == {"VALID_ENTITY", "GOOD_ENTITY"}
        assert len(graph["edges"]) == 1
        assert graph["edges"][0]["source"] == "VALID_ENTITY"
        assert graph["edges"][0]["target"] == "GOOD_ENTITY"

    @mock.patch("exporter.requests.delete")
    @mock.patch("exporter.requests.post")
    def test_scroll_search_batches(self, mock_post, mock_delete):
        """验证 _scroll_search_batches 的生成器逻辑"""
        from exporter import _scroll_search_batches

        init_resp = mock.Mock()
        init_resp.raise_for_status = mock.Mock()
        init_resp.json.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {"hits": [{"_id": "1"}, {"_id": "2"}]},
        }

        scroll_resp = mock.Mock()
        scroll_resp.raise_for_status = mock.Mock()
        scroll_resp.json.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {"hits": [{"_id": "3"}]},
        }

        empty_resp = mock.Mock()
        empty_resp.raise_for_status = mock.Mock()
        empty_resp.json.return_value = {
            "_scroll_id": "scroll-1",
            "hits": {"hits": []},
        }

        mock_post.side_effect = [init_resp, scroll_resp, empty_resp]

        batches = list(_scroll_search_batches(
            "http://localhost:9200/index/_search",
            {"query": {"match_all": {}}},
            ("admin", "pass"),
            "http",
            "localhost",
            9200,
        ))
        all_hits = [hit for batch in batches for hit in batch]
        assert len(all_hits) == 3
        assert [h["_id"] for h in all_hits] == ["1", "2", "3"]
        assert mock_post.call_count == 3
        mock_delete.assert_called_once()


class TestCountOsDocs:
    @mock.patch("exporter.requests.post")
    def test_success(self, mock_post):
        mock_post.return_value = mock.Mock(
            status_code=200,
            json=mock.Mock(return_value={"count": 42}),
            raise_for_status=mock.Mock(),
        )
        result = exporter._count_os_docs("http://os:9200/i/_count", {"query": {"match_all": {}}}, None)
        assert result == 42

    @mock.patch("exporter.requests.post")
    def test_exception(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")
        result = exporter._count_os_docs("http://os:9200/i/_count", {"query": {"match_all": {}}}, None)
        assert result is None


class TestExportGraphDirect:
    def _csv_paths(self, prefix, kb_id="test_kb"):
        return f"{prefix}_{kb_id}_nodes.csv", f"{prefix}_{kb_id}_edges.csv"

    def _make_scroll_mock(self, entity_batches, relation_batches):
        """构造 _scroll_search_batches 的 side_effect"""
        calls = []
        def side_effect(search_url, query, auth, scheme, host, port, batch_size=1000):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            if kg_kwd == "entity":
                return entity_batches
            else:
                return relation_batches
        return side_effect

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._count_os_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_success(self, mock_tenant, mock_count, mock_scroll, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth: (
            2 if query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"] == "entity" else 1
        )
        mock_scroll.side_effect = self._make_scroll_mock(
            entity_batches=[
                [
                    {"_source": {"entity_kwd": "A", "entity_type_kwd": "T", "content_with_weight": json.dumps({"description": "dA"}), "source_id": ["doc1"], "id": "e1"}},
                    {"_source": {"entity_kwd": "B", "entity_type_kwd": "T", "content_with_weight": json.dumps({"description": "dB"}), "source_id": ["doc2"], "id": "e2"}},
                ]
            ],
            relation_batches=[
                [
                    {"_source": {"from_entity_kwd": "A", "to_entity_kwd": "B", "content_with_weight": json.dumps({"weight": 1.0, "keywords": ["k1"]}), "source_id": ["doc1"], "id": "r1"}},
                ]
            ],
        )

        prefix = str(tmp_path / "direct")
        old_prefix = exporter.OUTPUT_PREFIX
        exporter.OUTPUT_PREFIX = prefix
        try:
            result = exporter.export_graph_direct(kb_id="test_kb", output_prefix=prefix)
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
        finally:
            exporter.OUTPUT_PREFIX = old_prefix

    @mock.patch("exporter._count_os_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_count_failure(self, mock_tenant, mock_count, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.return_value = None
        result = exporter.export_graph_direct(kb_id="test_kb")
        assert result is False

    @mock.patch("exporter._count_os_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_empty_results(self, mock_tenant, mock_count, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.return_value = 0
        result = exporter.export_graph_direct(kb_id="test_kb")
        assert result is False

    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._count_os_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_missing_node_backfill(self, mock_tenant, mock_count, mock_scroll, tmp_path):
        """关系引用了不在实体文档中的节点时，应自动补录到 nodes.csv"""
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth: (
            1 if query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"] == "entity" else 1
        )
        mock_scroll.side_effect = self._make_scroll_mock(
            entity_batches=[
                [
                    {"_source": {"entity_kwd": "A", "entity_type_kwd": "T", "content_with_weight": "{}", "source_id": [], "id": "e1"}},
                ]
            ],
            relation_batches=[
                [
                    {"_source": {"from_entity_kwd": "A", "to_entity_kwd": "C", "content_with_weight": "{}", "source_id": [], "id": "r1"}},
                ]
            ],
        )

        prefix = str(tmp_path / "backfill")
        old_prefix = exporter.OUTPUT_PREFIX
        exporter.OUTPUT_PREFIX = prefix
        try:
            result = exporter.export_graph_direct(kb_id="test_kb", output_prefix=prefix)
            assert result is True
            nodes_file, edges_file = self._csv_paths(prefix)
            nodes_df = pd.read_csv(nodes_file)
            edges_df = pd.read_csv(edges_file)
            assert len(nodes_df) == 2  # A + 补录的 C
            assert len(edges_df) == 1
            assert set(nodes_df["id"].tolist()) == {"A", "C"}
        finally:
            exporter.OUTPUT_PREFIX = old_prefix


class TestExportGraph:
    def _csv_paths(self, prefix):
        # export_graph 现在生成 {prefix}_{KB_ID}_nodes.csv
        return f"{prefix}_test_kb_id_nodes.csv", f"{prefix}_test_kb_id_edges.csv"

    def test_export_empty_graph(self, tmp_path):
        prefix = str(tmp_path / "empty")
        old_prefix = exporter.OUTPUT_PREFIX
        exporter.OUTPUT_PREFIX = prefix
        try:
            exporter.export_graph({"graph": {"nodes": [], "edges": []}})
            nodes_file, edges_file = self._csv_paths(prefix)
            assert os.path.exists(nodes_file)
            assert os.path.exists(edges_file)
            pd.read_csv(nodes_file)
        finally:
            exporter.OUTPUT_PREFIX = old_prefix

    def test_export_with_data(self, tmp_path):
        prefix = str(tmp_path / "graph")
        old_prefix = exporter.OUTPUT_PREFIX
        exporter.OUTPUT_PREFIX = prefix
        try:
            data = {
                "graph": {
                    "directed": False,
                    "multigraph": False,
                    "graph": {},
                    "nodes": [
                        {"id": "n1", "label": "Person"},
                        {"id": "n2", "label": "Company"},
                    ],
                    "edges": [
                        {"source": "n1", "target": "n2", "relation": "works_for"}
                    ],
                }
            }
            exporter.export_graph(data)
            nodes_file, edges_file = self._csv_paths(prefix)
            nodes_df = pd.read_csv(nodes_file)
            edges_df = pd.read_csv(edges_file)
            assert len(nodes_df) == 2
            assert len(edges_df) == 1
            assert "works_for" in edges_df["relation"].values
        finally:
            exporter.OUTPUT_PREFIX = old_prefix

    def test_invalid_data_type(self):
        with mock.patch("exporter.logger") as mock_logger:
            exporter.export_graph("bad data")
            mock_logger.error.assert_called()

    def test_missing_graph_field(self):
        with mock.patch("exporter.logger") as mock_logger:
            exporter.export_graph({})
            mock_logger.error.assert_called()
