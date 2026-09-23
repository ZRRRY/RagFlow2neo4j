# main.py
import logging
import os
import sys

import config
from exporter import export_graph_direct
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
    print("1. 从 Elasticsearch 流式导出 CSV")
    print("2. 仅从 CSV 导入 Neo4j")
    print("3. 自动执行导出 + 导入 Neo4j")
    print("4. 退出")
    print("-" * 40)
    choice = input("请输入选项 [1-4]: ").strip()
    return choice


def action_export_direct():
    """从 Elasticsearch 流式导出 CSV。"""
    logger.info("开始从 Elasticsearch 流式导出 CSV...")
    success = export_graph_direct(engine="elasticsearch")
    if success:
        nodes, edges = _default_csv_paths()
        logger.info("CSV 导出完成: %s, %s", nodes, edges)
    else:
        logger.error("从 Elasticsearch 导出失败，导出终止。")


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


def _run_export_import():
    """Elasticsearch 流式导出 + 导入执行逻辑"""
    logger.info("步骤 1/2: 从 Elasticsearch 流式导出 CSV...")
    success = export_graph_direct(engine="elasticsearch")
    if not success:
        logger.error("导出失败，自动流程终止。")
        return

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


def action_auto():
    """自动执行导出（固定 Elasticsearch）+ 导入 Neo4j。"""
    _run_export_import()


def main():
    # 运行前配置检查
    try:
        is_valid, missing = config.validate_config()
        if not is_valid:
            print("配置校验失败，请先完善 config.py 中的以下项：")
            for item in missing:
                print("  -", item)
            sys.exit(1)
    except Exception as exc:
        print("配置校验异常:", exc)
        sys.exit(1)

    while True:
        try:
            choice = menu()
            if choice == "1":
                action_export_direct()
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
