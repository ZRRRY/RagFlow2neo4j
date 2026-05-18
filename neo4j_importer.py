# neo4j_writer.py
import logging
import re
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase

import config

logger = logging.getLogger(__name__)

# 关系类型合法性校验（Cypher 标识符规则）
_REL_TYPE_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _sanitize_rel_type(name):
    """校验并清洗关系类型名称，不合法时回退为 RELATED_TO。"""
    if name and _REL_TYPE_PATTERN.match(str(name)):
        return str(name)
    return "RELATED_TO"


class Neo4jWriter:
    """封装 Neo4j 的批量写入操作。支持上下文管理器。"""

    def __init__(self, uri=None, user=None, password=None, database=None):
        self.uri = uri or config.NEO4J_URI
        self.user = user or config.NEO4J_USER
        self.password = password or config.NEO4J_PASSWORD
        self.database = database or config.NEO4J_DATABASE
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def close(self):
        self.driver.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def test_connection(self):
        """测试与 Neo4j 的连接是否可用。"""
        try:
            with self.driver.session(database=self.database) as session:
                session.run("RETURN 1 AS n")
            return True
        except Exception as exc:
            logger.error("Neo4j 连接测试失败: %s", exc)
            return False

    def clear_database(self):
        """清空当前数据库中的所有节点和关系（不可逆）。"""
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.info("已清空 Neo4j 数据库")

    def import_nodes(self, csv_path):
        """从节点 CSV 批量导入到 Neo4j。"""
        path = Path(csv_path)
        if not path.exists():
            logger.error("节点 CSV 不存在: %s", csv_path)
            return

        import pandas as pd
        df = pd.read_csv(csv_path, encoding='utf-8-sig', dtype=str)
        df = df.fillna("")

        if "id" not in df.columns:
            logger.error("节点 CSV 缺少 id 列")
            return

        records = df.to_dict('records')
        total = len(records)
        batch_size = 1000

        with self.driver.session(database=self.database) as session:
            for i in range(0, total, batch_size):
                batch = records[i:i + batch_size]
                rows = []
                for r in batch:
                    node_id = r.pop("id", "")
                    rows.append({"id": node_id, "props": r})
                session.run("""
                    UNWIND $rows AS row
                    MERGE (n:Entity {id: row.id})
                    SET n += row.props
                """, rows=rows)
                logger.info("节点写入进度: %s/%s", min(i + batch_size, total), total)

        logger.info("节点导入完成，共 %s 条", total)

    def import_edges(self, csv_path):
        """从边 CSV 批量导入到 Neo4j，支持按关系类型分组。"""
        path = Path(csv_path)
        if not path.exists():
            logger.error("边 CSV 不存在: %s", csv_path)
            return

        import pandas as pd
        df = pd.read_csv(csv_path, encoding='utf-8-sig', dtype=str)
        df = df.fillna("")

        required = {"source", "target"}
        if not required.issubset(df.columns):
            logger.error("边 CSV 缺少 source/target 列")
            return

        records = df.to_dict('records')
        groups = defaultdict(list)
        for r in records:
            rel_type = _sanitize_rel_type(r.pop("relation", None))
            src = r.pop("source")
            tgt = r.pop("target")
            groups[rel_type].append({"source": src, "target": tgt, "props": r})

        with self.driver.session(database=self.database) as session:
            for rel_type, rows in groups.items():
                total = len(rows)
                batch_size = 1000
                for i in range(0, total, batch_size):
                    batch = rows[i:i + batch_size]
                    session.run(f"""
                        UNWIND $rows AS row
                        MATCH (a:Entity {{id: row.source}})
                        MATCH (b:Entity {{id: row.target}})
                        MERGE (a)-[r:{rel_type}]->(b)
                        SET r += row.props
                    """, rows=batch)
                    logger.info("关系 [%s] 写入进度: %s/%s", rel_type,
                                min(i + batch_size, total), total)

        logger.info("边导入完成，共 %s 条", len(records))
