import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在导入 neo4j_writer 之前注入 mock config
mock_config = mock.MagicMock()
mock_config.NEO4J_URI = "bolt://localhost:7687"
mock_config.NEO4J_USER = "neo4j"
mock_config.NEO4J_PASSWORD = "test"
mock_config.NEO4J_DATABASE = "neo4j"
sys.modules["config"] = mock_config

import pandas as pd
import pytest

from neo4j_importer import _sanitize_rel_type, Neo4jWriter


class TestSanitizeRelType:
    def test_valid_types(self):
        assert _sanitize_rel_type("WORKS_FOR") == "WORKS_FOR"
        assert _sanitize_rel_type("hasPart") == "hasPart"
        assert _sanitize_rel_type("_internal") == "_internal"

    def test_invalid_types(self):
        assert _sanitize_rel_type("123abc") == "RELATED_TO"
        assert _sanitize_rel_type("has-space") == "RELATED_TO"
        assert _sanitize_rel_type("") == "RELATED_TO"
        assert _sanitize_rel_type(None) == "RELATED_TO"


class TestNeo4jWriter:
    @mock.patch("neo4j_importer.GraphDatabase.driver")
    def test_init_uses_config(self, mock_driver):
        Neo4jWriter()
        mock_driver.assert_called_once_with(
            "bolt://localhost:7687", auth=("neo4j", "test")
        )

    @mock.patch("neo4j_importer.GraphDatabase.driver")
    def test_test_connection_success(self, mock_driver):
        mock_session = mock.Mock()
        mock_ctx = mock.Mock()
        mock_ctx.__enter__ = mock.Mock(return_value=mock_session)
        mock_ctx.__exit__ = mock.Mock(return_value=False)
        mock_driver.return_value.session.return_value = mock_ctx
        writer = Neo4jWriter()
        assert writer.test_connection() is True

    @mock.patch("neo4j_importer.GraphDatabase.driver")
    def test_test_connection_failure(self, mock_driver):
        mock_driver.return_value.session.side_effect = Exception("Connection refused")
        writer = Neo4jWriter()
        assert writer.test_connection() is False

    @mock.patch("neo4j_importer.GraphDatabase.driver")
    def test_clear_database(self, mock_driver):
        mock_session = mock.Mock()
        mock_ctx = mock.Mock()
        mock_ctx.__enter__ = mock.Mock(return_value=mock_session)
        mock_ctx.__exit__ = mock.Mock(return_value=False)
        mock_driver.return_value.session.return_value = mock_ctx
        writer = Neo4jWriter()
        writer.clear_database()
        mock_session.run.assert_called_once_with("MATCH (n) DETACH DELETE n")

    @mock.patch("neo4j_importer.GraphDatabase.driver")
    def test_import_nodes(self, mock_driver, tmp_path):
        csv_path = tmp_path / "nodes.csv"
        df = pd.DataFrame({"id": ["n1", "n2"], "label": ["Person", "Company"]})
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

        mock_session = mock.Mock()
        mock_ctx = mock.Mock()
        mock_ctx.__enter__ = mock.Mock(return_value=mock_session)
        mock_ctx.__exit__ = mock.Mock(return_value=False)
        mock_driver.return_value.session.return_value = mock_ctx

        writer = Neo4jWriter()
        writer.import_nodes(str(csv_path))
        assert mock_session.run.call_count == 1
        cypher = mock_session.run.call_args[0][0]
        assert "MERGE (n:Entity {id: row.id})" in cypher

    @mock.patch("neo4j_importer.GraphDatabase.driver")
    def test_import_edges_groups_by_relation(self, mock_driver, tmp_path):
        csv_path = tmp_path / "edges.csv"
        df = pd.DataFrame({
            "source": ["n1", "n1", "n2"],
            "target": ["n2", "n3", "n3"],
            "relation": ["WORKS_FOR", "KNOWS", "WORKS_FOR"],
            "since": ["2020", "2021", "2022"],
        })
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

        mock_session = mock.Mock()
        mock_ctx = mock.Mock()
        mock_ctx.__enter__ = mock.Mock(return_value=mock_session)
        mock_ctx.__exit__ = mock.Mock(return_value=False)
        mock_driver.return_value.session.return_value = mock_ctx

        writer = Neo4jWriter()
        writer.import_edges(str(csv_path))

        calls = mock_session.run.call_args_list
        assert len(calls) == 2
        cyphers = [call[0][0] for call in calls]
        assert any("WORKS_FOR" in c for c in cyphers)
        assert any("KNOWS" in c for c in cyphers)
