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

import pandas as pd
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import exporter


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
    @mock.patch("exporter.requests.post")
    def test_success(self, mock_post):
        mock_post.return_value = mock.Mock(
            status_code=200,
            json=mock.Mock(return_value={"count": 42}),
            raise_for_status=mock.Mock(),
        )
        result = exporter._count_es_docs("http://es:9200/i/_count", {"query": {"match_all": {}}}, None)
        assert result == 42

    @mock.patch("exporter.requests.post")
    def test_exception(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")
        result = exporter._count_es_docs("http://es:9200/i/_count", {"query": {"match_all": {}}}, None)
        assert result is None


class TestScrollSearchBatches:
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
            None,
            "http",
            "localhost",
            9200,
        ))
        all_hits = [hit for batch in batches for hit in batch]
        assert len(all_hits) == 3
        assert [h["_id"] for h in all_hits] == ["1", "2", "3"]
        assert mock_post.call_count == 3
        mock_delete.assert_called_once()


class TestExportGraphDirect:
    """测试 OpenSearch 与 Elasticsearch 的流式导出。"""

    def _csv_paths(self, prefix, kb_id="test_kb"):
        return f"{prefix}_{kb_id}_nodes.csv", f"{prefix}_{kb_id}_edges.csv"

    def _make_scroll_mock(self, entity_batches, relation_batches):
        """构造 _scroll_search_batches 的 side_effect"""
        def side_effect(search_url, query, auth, scheme, host, port, batch_size=1000):
            kg_kwd = query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"]
            if kg_kwd == "entity":
                return entity_batches
            else:
                return relation_batches
        return side_effect

    @pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
    @mock.patch("exporter._scroll_search_batches")
    @mock.patch("exporter._count_es_docs")
    @mock.patch("exporter._get_tenant_id")
    def test_success(self, mock_tenant, mock_count, mock_scroll, engine, tmp_path):
        mock_tenant.return_value = "t-123"
        mock_count.side_effect = lambda count_url, query, auth: (
            2 if query["query"]["bool"]["filter"][1]["term"]["knowledge_graph_kwd"] == "entity" else 1
        )
        mock_scroll.side_effect = self._make_scroll_mock(
            entity_batches=[
                [
                    {"_source": {"content_with_weight": json.dumps({"entity_name": "A", "entity_type": "T", "description": "dA", "source_id": ["doc1"], "pagerank": 0.1, "rank": 1})}},
                    {"_source": {"content_with_weight": json.dumps({"entity_name": "B", "entity_type": "T", "description": "dB", "source_id": ["doc2"], "pagerank": 0.2, "rank": 2})}},
                ]
            ],
            relation_batches=[
                [
                    {"_source": {"content_with_weight": json.dumps({"src_id": "A", "tgt_id": "B", "description": "", "keywords": ["k1"], "weight": 1.0, "source_id": ["doc1"]})}},
                ]
            ],
        )

        prefix = str(tmp_path / engine)
        old_prefix = exporter.OUTPUT_PREFIX
        exporter.OUTPUT_PREFIX = prefix
        try:
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
        finally:
            exporter.OUTPUT_PREFIX = old_prefix

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

    @mock.patch("exporter.export_graph_direct")
    def test_elasticsearch_alias(self, mock_export):
        mock_export.return_value = True
        result = exporter.export_graph_direct_elasticsearch(kb_id="alias_kb", output_prefix="/tmp/alias")
        assert result is True
        mock_export.assert_called_once_with(
            kb_id="alias_kb",
            output_dir=None,
            output_prefix="/tmp/alias",
            batch_size=1000,
            engine="elasticsearch",
        )

    def test_invalid_engine(self):
        with mock.patch("exporter.logger") as mock_logger:
            result = exporter.export_graph_direct(kb_id="test_kb", engine="solr")
            assert result is False
            mock_logger.error.assert_called()
