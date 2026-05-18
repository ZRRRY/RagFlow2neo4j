# main.py
import logging
import os
import sys

import config
from exporter import fetch_knowledge_graph, export_graph
from neo4j_importer import Neo4jWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _default_csv_paths():
    """根据 config 中的 OUTPUT_PREFIX 和 KB_ID 生成默认 CSV 路径。"""
    output_dir = getattr(config, "OUTPUT_DIR", "") or os.path.dirname(config.OUTPUT_PREFIX)
    base_name = os.path.basename(config.OUTPUT_PREFIX) or "output"
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    nodes = os.path.join(output_dir, f"{base_name}_{config.KB_ID}_nodes.csv")
    edges = os.path.join(output_dir, f"{base_name}_{config.KB_ID}_edges.csv")
    return nodes, edges


def menu():
    print("\n" + "=" * 40)
    print(" RagFlow → Neo4j 数据迁移工具 ")
    print("=" * 40)
    print("当前知识库 ID:", config.KB_ID)
    print("-" * 40)
    print("1. 仅从 RagFlow 导出 CSV")
    print("2. 仅从 CSV 导入 Neo4j")
    print("3. 自动执行导出 + 导入 Neo4j（保留 CSV）")
    print("4. 退出")
    print("-" * 40)
    choice = input("请输入选项 [1-4]: ").strip()
    return choice


def action_export_only():
    logger.info("开始从 RagFlow 导出 CSV...")
    data = fetch_knowledge_graph()
    if data:
        export_graph(data)
        nodes, edges = _default_csv_paths()
        logger.info("CSV 导出完成: %s, %s", nodes, edges)
    else:
        logger.error("从 RagFlow 获取数据失败，导出终止。")


def action_import_only():
    nodes_default, edges_default = _default_csv_paths()
    nodes_path = input(f"节点 CSV 路径 [{nodes_default}]: ").strip() or nodes_default
    edges_path = input(f"边 CSV 路径 [{edges_default}]: ").strip() or edges_default

    if not os.path.exists(nodes_path):
        logger.error("节点 CSV 不存在: %s", nodes_path)
        return
    if not os.path.exists(edges_path):
        logger.error("边 CSV 不存在: %s", edges_path)
        return

    with Neo4jWriter() as writer:
        if not writer.test_connection():
            logger.error("无法连接到 Neo4j，请检查配置。")
            return

        clear = input("是否先清空 Neo4j 数据库？数据将不可恢复！ [y/N]: ").strip().lower()
        if clear == 'y':
            writer.clear_database()

        writer.import_nodes(nodes_path)
        writer.import_edges(edges_path)

    logger.info("Neo4j 导入完成！")


def action_auto():
    logger.info("步骤 1/2: 从 RagFlow 导出 CSV...")
    data = fetch_knowledge_graph()
    if not data:
        logger.error("从 RagFlow 获取数据失败。")
        return

    export_graph(data)
    nodes, edges = _default_csv_paths()
    logger.info("CSV 导出完成: %s, %s", nodes, edges)

    logger.info("步骤 2/2: 导入 Neo4j...")
    with Neo4jWriter() as writer:
        if not writer.test_connection():
            logger.error("无法连接到 Neo4j，请检查配置。")
            return

        clear = input("是否先清空 Neo4j 数据库？数据将不可恢复！ [y/N]: ").strip().lower()
        if clear == 'y':
            writer.clear_database()

        writer.import_nodes(nodes)
        writer.import_edges(edges)

    logger.info("自动流程执行完毕！")


def main():
    while True:
        try:
            choice = menu()
            if choice == "1":
                action_export_only()
            elif choice == "2":
                action_import_only()
            elif choice == "3":
                action_auto()
            elif choice == "4":
                print("再见！")
                sys.exit(0)
            else:
                print("无效选项，请重新输入。")
        except KeyboardInterrupt:
            print("\n操作已取消。")
        except Exception as exc:
            logger.exception("发生未预期错误: %s", exc)


if __name__ == "__main__":
    main()
