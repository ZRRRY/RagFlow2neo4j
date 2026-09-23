import argparse
import ast
import codecs
import csv
import json
import logging
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

try:
    import config
except Exception:
    config = None


logger = logging.getLogger(__name__)

DOCUMENT_ID_COLUMNS = [
    "document_id",
    "doc_id",
    "source_id",
    "ragflow_document_id",
    "ragflow_doc_id",
    "id",
]
DOCUMENT_NAME_COLUMNS = [
    "document_name",
    "doc_name",
    "book_name",
    "book",
    "title",
    "name",
    "filename",
    "file_name",
    "文件名",
    "书名",
]
CATEGORY_CODE_COLUMNS = [
    "category_code",
    "category_codes",
    "clc_code",
    "class_code",
    "code",
    "clc",
    "中图分类号",
    "分类号",
]
CATEGORY_LABEL_COLUMNS = [
    "category_label",
    "category_name",
    "label",
    "class_name",
    "name",
    "分类",
    "类别",
]

CATEGORY_LABELS = {
    "J0": "艺术理论",
    "J1": "世界各国艺术概况",
    "J2": "绘画",
    "J29": "书法、篆刻",
    "J3": "雕塑",
    "J4": "摄影艺术",
    "J5": "工艺美术",
    "J59": "建筑艺术",
    "J6": "音乐",
    "J7": "舞蹈",
    "J8": "戏剧艺术",
    "J9": "电影、电视艺术",
}


def get_config_value(name: str, default: str = "") -> str:
    value = getattr(config, name, None) if config else None
    return str(value if value not in [None, ""] else default)


def first_existing_column(row: dict[str, str], candidates: list[str]) -> str:
    lower_keys = {key.lower(): key for key in row.keys()}

    for candidate in candidates:
        real_key = lower_keys.get(candidate.lower())
        if real_key and row.get(real_key, "").strip():
            return real_key

    return ""


def split_values(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    text = str(value).strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if parsed not in [None, ""]:
            return [str(parsed).strip()]
    except Exception:
        pass

    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if parsed not in [None, ""]:
            return [str(parsed).strip()]
    except Exception:
        pass

    return [
        item.strip()
        for item in re.split(r"[,;，；\s]+", text)
        if item.strip()
    ]


def unique_keep_order(values: list[str]) -> list[str]:
    result = []
    seen = set()

    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)

    return result


def normalize_title(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"\.(md|pdf|txt|docx?|epub)$", "", text)
    text = re.sub(r"^\d+[\s.．、_-]+", "", text)
    text = re.sub(r"\s+\d+(?:_\d+)?$", "", text)
    text = re.sub(r"[\(（]\d+[\)）]$", "", text)
    text = text.replace("_", " ")
    text = re.sub(r"isbn(?:10|13)?\s+\S+", " ", text)
    text = re.sub(r"[0-9a-f]{24,}", " ", text)
    text = text.replace("anna's archive", " ")
    text = text.replace("anna�s archive", " ")
    text = re.sub(r"[\s\-—_·:：,，;；()（）《》【】\[\]]+", "", text)
    return text


def title_matches(document_title: str, category_title: str) -> bool:
    if not document_title or not category_title:
        return False

    if document_title == category_title:
        return True

    # Too-short titles such as "艺术" are ambiguous and should only match exactly.
    if len(category_title) < 4 or len(document_title) < 4:
        return False

    if category_title in document_title:
        return len(category_title) >= 6

    if document_title in category_title:
        return len(document_title) >= 6

    return False


def read_csv_rows(path: str) -> list[dict[str, str]]:
    data = Path(path).read_bytes()

    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")

    sample = text[:4096]

    if Path(path).suffix.lower() == ".tsv":
        dialect = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel

    return list(csv.DictReader(text.splitlines(), dialect=dialect))


def read_document_names(path: str | None) -> dict[str, str]:
    if not path:
        return {}

    documents = {}

    for row in read_csv_rows(path):
        id_col = first_existing_column(row, DOCUMENT_ID_COLUMNS)
        name_col = first_existing_column(row, DOCUMENT_NAME_COLUMNS)

        if not id_col or not name_col:
            continue

        documents[row[id_col].strip()] = row[name_col].strip()

    return documents


def add_category(
    mapping: dict[str, list[dict[str, str]]],
    key: str,
    code: str,
    label: str,
):
    if not key or not code:
        return

    mapping[key].append(
        {
            "code": code.strip(),
            "label": label.strip() or CATEGORY_LABELS.get(code.strip(), ""),
        }
    )


def read_category_mapping(
    categories_csv: str,
    documents_by_id: dict[str, str],
) -> tuple[dict[str, list[dict[str, str]]], list[tuple[str, list[dict[str, str]]]]]:
    by_document_id = defaultdict(list)
    by_title = defaultdict(list)

    for row in read_csv_rows(categories_csv):
        id_col = first_existing_column(row, DOCUMENT_ID_COLUMNS)
        title_col = first_existing_column(row, DOCUMENT_NAME_COLUMNS)
        code_col = first_existing_column(row, CATEGORY_CODE_COLUMNS)
        label_col = first_existing_column(row, CATEGORY_LABEL_COLUMNS)

        if not code_col:
            continue

        labels = split_values(row.get(label_col, "")) if label_col else []
        codes = split_values(row.get(code_col, ""))
        doc_ids = split_values(row.get(id_col, "")) if id_col else []
        title = row.get(title_col, "").strip() if title_col else ""

        for index, code in enumerate(codes):
            label = labels[index] if index < len(labels) else CATEGORY_LABELS.get(code, "")

            for doc_id in doc_ids:
                add_category(by_document_id, doc_id, code, label)

                doc_title = documents_by_id.get(doc_id, "")
                if doc_title:
                    add_category(by_title, normalize_title(doc_title), code, label)

            if title:
                add_category(by_title, normalize_title(title), code, label)

    title_items = sorted(
        by_title.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    return dict(by_document_id), title_items


def categories_for_source_ids(
    source_ids: list[str],
    by_document_id: dict[str, list[dict[str, str]]],
    title_items: list[tuple[str, list[dict[str, str]]]],
    documents_by_id: dict[str, str],
) -> tuple[list[str], list[str], str, list[str]]:
    codes = []
    labels = []
    matched_source_ids = []

    for source_id in source_ids:
        categories = list(by_document_id.get(source_id, []))

        title = documents_by_id.get(source_id, "")
        normalized_title = normalize_title(title) if title else ""

        if normalized_title:
            for category_title, title_categories in title_items:
                if not category_title:
                    continue
                if title_matches(normalized_title, category_title):
                    categories.extend(title_categories)
                    break

        if not categories:
            continue

        matched_source_ids.append(source_id)
        for category in categories:
            codes.append(category["code"])
            if category["label"]:
                labels.append(category["label"])

    codes = unique_keep_order(codes)
    labels = unique_keep_order(labels)

    counter = Counter()
    for source_id in source_ids:
        source_codes = {
            item["code"]
            for item in by_document_id.get(source_id, [])
        }
        title = documents_by_id.get(source_id, "")
        normalized_title = normalize_title(title) if title else ""

        for category_title, title_categories in title_items:
            if normalized_title and title_matches(normalized_title, category_title):
                source_codes.update(item["code"] for item in title_categories)
                break

        for code in source_codes:
            counter[code] += 1

    primary = counter.most_common(1)[0][0] if counter else (codes[0] if codes else "")
    return codes, labels, primary, unique_keep_order(matched_source_ids)


def iter_nodes(session, batch_size: int):
    offset = 0

    while True:
        records = session.run(
            """
            MATCH (n:Entity)
            WHERE n.source_id IS NOT NULL
            RETURN elementId(n) AS element_id, n.id AS id, n.source_id AS source_id
            SKIP $offset
            LIMIT $limit
            """,
            offset=offset,
            limit=batch_size,
        ).data()

        if not records:
            return

        yield records
        offset += len(records)


def update_neo4j(args):
    documents_by_id = read_document_names(args.documents)
    by_document_id, title_items = read_category_mapping(
        args.categories,
        documents_by_id,
    )

    if not by_document_id and not title_items:
        raise ValueError("No category mapping was loaded from the category CSV.")

    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    scanned = 0
    matched = 0
    updated = 0
    samples = []

    try:
        with driver.session(database=args.neo4j_database) as session:
            for nodes in iter_nodes(session, args.batch_size):
                rows = []

                for node in nodes:
                    scanned += 1
                    source_ids = split_values(node.get("source_id"))
                    codes, labels, primary, matched_source_ids = categories_for_source_ids(
                        source_ids,
                        by_document_id,
                        title_items,
                        documents_by_id,
                    )

                    if not codes:
                        continue

                    matched += 1
                    row = {
                        "element_id": node["element_id"],
                        "category_codes": codes,
                        "category_labels": labels,
                        "primary_category": primary,
                        "category_source_ids": matched_source_ids,
                    }
                    rows.append(row)

                    if len(samples) < args.sample_limit:
                        samples.append(
                            {
                                "id": node.get("id"),
                                "source_ids": source_ids,
                                "category_codes": codes,
                                "primary_category": primary,
                            }
                        )

                if args.apply and rows:
                    session.run(
                        """
                        UNWIND $rows AS row
                        MATCH (n:Entity)
                        WHERE elementId(n) = row.element_id
                        SET n.category_codes = row.category_codes,
                            n.category_labels = row.category_labels,
                            n.primary_category = row.primary_category,
                            n.category_source_ids = row.category_source_ids
                        """,
                        rows=rows,
                    )
                    updated += len(rows)

    finally:
        driver.close()

    logger.info("Scanned nodes: %s", scanned)
    logger.info("Matched nodes: %s", matched)
    logger.info("Updated nodes: %s", updated if args.apply else 0)

    if samples:
        logger.info("Samples:")
        for sample in samples:
            logger.info(json.dumps(sample, ensure_ascii=False))

    if not args.apply:
        logger.info("Dry run only. Add --apply to write properties into Neo4j.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Add category properties to existing Neo4j Entity nodes.",
    )
    parser.add_argument(
        "--categories",
        required=True,
        help="CSV file containing book/document category mappings.",
    )
    parser.add_argument(
        "--documents",
        default="",
        help="Optional CSV exported from RagFlow document table with id and name columns.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to Neo4j. Without this flag the script only prints a dry run.",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument(
        "--neo4j-uri",
        default=os.getenv("NEO4J_URI") or get_config_value("NEO4J_URI", "bolt://localhost:7687"),
    )
    parser.add_argument(
        "--neo4j-user",
        default=os.getenv("NEO4J_USER") or get_config_value("NEO4J_USER", "neo4j"),
    )
    parser.add_argument(
        "--neo4j-password",
        default=os.getenv("NEO4J_PASSWORD") or get_config_value("NEO4J_PASSWORD", ""),
    )
    parser.add_argument(
        "--neo4j-database",
        default=os.getenv("NEO4J_DATABASE") or get_config_value("NEO4J_DATABASE", "neo4j"),
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    update_neo4j(parse_args())
