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
    @mock.patch("exporter.requests.get")
    def test_success(self, mock_get):
        mock_get.return_value = mock.Mock(
            status_code=200,
            text='{"code":0,"data":{"graph":{"nodes":[],"edges":[]}}}',
            json=lambda: {"code": 0, "data": {"graph": {"nodes": [], "edges": []}}},
        )
        result = exporter.fetch_knowledge_graph()
        assert result == {"graph": {"nodes": [], "edges": []}}
        mock_get.assert_called_once()

    @mock.patch("exporter.requests.get")
    def test_http_error(self, mock_get):
        mock_get.return_value = mock.Mock(
            status_code=500, text="Internal Server Error"
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None

    @mock.patch("exporter.requests.get")
    def test_api_error_code(self, mock_get):
        mock_get.return_value = mock.Mock(
            status_code=200,
            text='{"code":-1,"message":"auth failed"}',
            json=lambda: {"code": -1, "message": "auth failed"},
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None

    @mock.patch("exporter.requests.get")
    def test_json_decode_error(self, mock_get):
        mock_get.return_value = mock.Mock(
            status_code=200,
            text="not json",
            json=lambda: json.loads("not json"),
        )
        result = exporter.fetch_knowledge_graph()
        assert result is None

    @mock.patch("exporter.requests.get")
    def test_request_exception(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")
        result = exporter.fetch_knowledge_graph()
        assert result is None


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
