import os
import re
import time
import math
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from neo4j import GraphDatabase
from neo4j.graph import Node, Relationship

load_dotenv()

# =========================
# 基础配置
# =========================

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687").strip()
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j").strip()
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4j1127").strip()
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j").strip()

CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "10"))

# 前端打包后的 dist 目录
FRONTEND_DIST = Path(
    os.getenv(
        "FRONTEND_DIST",
        r"D:\pyproject\graph-mindmap\dist",
    )
)

# 可选：真正向量语义搜索
# 默认 false，不装 sentence-transformers 也能运行
USE_EMBEDDING_SEARCH = os.getenv("USE_EMBEDDING_SEARCH", "false").lower() == "true"
SEMANTIC_MODEL_PATH = os.getenv("SEMANTIC_MODEL_PATH", "").strip()

_semantic_model = None

driver = GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USER, NEO4J_PASSWORD),
)

_cache = {
    "time": 0.0,
    "key": None,
    "data": None,
}


# =========================
# 文本清洗
# =========================

SEP_PATTERN = re.compile(
    r"\s*(?:<\s*(?:sep|sev)\s*>|&lt;\s*(?:sep|sev)\s*&gt;)\s*",
    flags=re.IGNORECASE,
)


def clean_text(value: Any, replacement: str = "；") -> str:
    """
    清洗 RAGFlow / Neo4j 文本中的 <SEP> / <sep> / <SEV> / <sev>。
    """
    if value is None:
        return ""

    text = str(value).strip()

    if len(text) >= 2:
        if (text.startswith('"') and text.endswith('"')) or (
            text.startswith("'") and text.endswith("'")
        ):
            text = text[1:-1].strip()

    text = SEP_PATTERN.sub(replacement, text)
    text = text.replace("\\n", " ")
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"；{2,}", "；", text)

    return text.strip(" ；\n\t\r")


def clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)

    if isinstance(value, list):
        return [
            clean_text(item) if isinstance(item, str) else item
            for item in value
        ]

    return value


def clean_props(props: dict) -> dict:
    return {key: clean_value(value) for key, value in props.items()}


# =========================
# FastAPI 生命周期
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    driver.close()


app = FastAPI(
    title="Neo4j Knowledge Graph API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# 通用工具函数
# =========================

def normalize_keywords(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [clean_text(x) for x in value if clean_text(x)]

    if isinstance(value, str):
        text = clean_text(value)
        return [
            item.strip()
            for item in text.replace("，", ",")
            .replace("、", ",")
            .replace("；", ",")
            .replace(";", ",")
            .split(",")
            if item.strip()
        ]

    return [clean_text(value)]


def first_non_empty(props: dict, keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = props.get(key)
        if value not in [None, ""]:
            return value
    return default


def safe_float(value: Any, default: float = 1.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 2) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def node_name_id(node: Node) -> str:
    """
    当前数据库规则：每个节点的 id 就是节点名。
    """
    props = clean_props(dict(node))

    node_id = first_non_empty(
        props,
        ["id", "entity_name", "name", "label", "title"],
        default=None,
    )

    if node_id is not None:
        return clean_text(node_id)

    return str(node.element_id)


def serialize_node(node: Node) -> dict:
    props = clean_props(dict(node))
    labels = list(node.labels)

    node_id = node_name_id(node)

    label = clean_text(
        first_non_empty(
            props,
            ["entity_name", "name", "label", "title", "id"],
            default=node_id,
        )
    )

    node_type = clean_text(
        first_non_empty(
            props,
            ["entity_type", "type", "category", "node_type", "label_type"],
            default=labels[0] if labels else "Entity",
        )
    )

    description = clean_text(
        first_non_empty(
            props,
            ["description", "desc", "summary", "definition", "explanation"],
            default="",
        )
    )

    teaching_role = clean_text(
        first_non_empty(
            props,
            ["teaching_role", "teachingRole", "teaching_value", "pedagogical_value"],
            default="",
        )
    )

    keywords = normalize_keywords(
        first_non_empty(
            props,
            ["keywords", "keyword", "tags", "aliases"],
            default=[],
        )
    )

    level = safe_int(
        first_non_empty(props, ["level", "depth"], default=2),
        default=2,
    )

    return {
        "id": node_id,
        "label": label,
        "type": node_type,
        "description": description,
        "teaching_role": teaching_role,
        "keywords": keywords,
        "level": level,
        "pagerank": props.get("pagerank"),
        "source_id": props.get("source_id"),
        "raw": {
            **props,
            "neo4j_element_id": node.element_id,
            "neo4j_labels": labels,
        },
    }


def serialize_relationship(rel: Relationship, source: Node, target: Node) -> dict:
    props = clean_props(dict(rel))

    relation_label = clean_text(
        first_non_empty(
            props,
            ["label", "relation", "relationship", "relation_type", "predicate", "type"],
            default=rel.type,
        )
    )

    weight = safe_float(
        first_non_empty(
            props,
            ["weight", "score", "confidence", "pagerank"],
            default=1.0,
        ),
        default=1.0,
    )

    description = clean_text(
        first_non_empty(
            props,
            ["description", "desc", "evidence", "summary", "explanation"],
            default="",
        )
    )

    return {
        "id": str(rel.element_id),
        "source": node_name_id(source),
        "target": node_name_id(target),
        "label": relation_label,
        "weight": weight,
        "description": description,
        "raw": {
            **props,
            "neo4j_element_id": rel.element_id,
            "neo4j_type": rel.type,
        },
    }


# =========================
# 搜索相关
# =========================

def normalize_text(text: Any) -> str:
    return clean_text(text).lower().strip()


def char_ngrams(text: str, n: int = 2) -> set[str]:
    text = normalize_text(text).replace(" ", "")
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def lexical_semantic_score(query: str, node: dict) -> float:
    """
    无向量模型时的综合相关性排序。
    """
    q = normalize_text(query)
    label = normalize_text(node.get("label"))
    node_id = normalize_text(node.get("id"))
    node_type = normalize_text(node.get("type"))
    desc = normalize_text(node.get("description"))
    keywords = [normalize_text(x) for x in node.get("keywords", [])]

    all_text = " ".join([label, node_id, node_type, desc, " ".join(keywords)])

    if not q:
        return 0.0

    score = 0.0

    if q == label or q == node_id:
        score += 1.0
    elif label.startswith(q) or node_id.startswith(q):
        score += 0.82
    elif q in label or q in node_id:
        score += 0.68

    if any(q == k for k in keywords):
        score += 0.55
    elif any(q in k for k in keywords):
        score += 0.42

    if q in desc:
        score += 0.28

    q_grams = char_ngrams(q, 2)
    t_grams = char_ngrams(all_text, 2)

    if q_grams and t_grams:
        overlap = len(q_grams & t_grams) / max(1, len(q_grams | t_grams))
        score += overlap * 0.45

    fuzzy = SequenceMatcher(None, q, label).ratio()
    score += fuzzy * 0.25

    pagerank = safe_float(node.get("pagerank"), 0.0)
    if pagerank > 0:
        score += min(math.log1p(pagerank) * 0.03, 0.08)

    return round(score, 6)


def get_semantic_model():
    global _semantic_model

    if not USE_EMBEDDING_SEARCH:
        return None

    if _semantic_model is not None:
        return _semantic_model

    try:
        from sentence_transformers import SentenceTransformer

        if SEMANTIC_MODEL_PATH:
            _semantic_model = SentenceTransformer(SEMANTIC_MODEL_PATH)
        else:
            _semantic_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

        print("[semantic-search] embedding model loaded.")
        return _semantic_model

    except Exception as exc:
        print("[semantic-search] embedding model unavailable, fallback to lexical score:", exc)
        return None


def node_search_text(node: dict) -> str:
    return "。".join(
        [
            str(node.get("label", "")),
            str(node.get("type", "")),
            str(node.get("description", "")),
            "，".join(node.get("keywords", []) or []),
        ]
    )


def rank_nodes(query: str, nodes: list[dict], limit: int) -> list[dict]:
    model = get_semantic_model()
    lexical_scores = [lexical_semantic_score(query, node) for node in nodes]

    if model is not None and nodes:
        try:
            from sentence_transformers import util

            texts = [node_search_text(node) for node in nodes]
            query_emb = model.encode(
                query,
                convert_to_tensor=True,
                normalize_embeddings=True,
            )
            text_embs = model.encode(
                texts,
                convert_to_tensor=True,
                normalize_embeddings=True,
            )

            cosine_scores = util.cos_sim(query_emb, text_embs)[0].cpu().tolist()

            ranked = []

            for node, lex_score, emb_score in zip(nodes, lexical_scores, cosine_scores):
                final_score = 0.7 * float(emb_score) + 0.3 * float(lex_score)
                ranked.append(
                    {
                        **node,
                        "score": round(final_score, 6),
                        "score_detail": {
                            "embedding": round(float(emb_score), 6),
                            "lexical": round(float(lex_score), 6),
                        },
                    }
                )

            ranked.sort(key=lambda x: (
                bool(x.get("_search_exact_name")),
                bool(x.get("_search_name_match")),
                x["score"],
            ), reverse=True)
            return ranked[:limit]

        except Exception as exc:
            print("[semantic-search] embedding ranking failed, fallback:", exc)

    ranked = []

    for node, score in zip(nodes, lexical_scores):
        ranked.append(
            {
                **node,
                "score": score,
                "score_detail": {
                    "embedding": None,
                    "lexical": score,
                },
            }
        )

    ranked.sort(key=lambda x: (
        bool(x.get("_search_exact_name")),
        bool(x.get("_search_name_match")),
        x["score"],
    ), reverse=True)
    return ranked[:limit]


# =========================
# Neo4j 查询函数
# =========================

def check_neo4j_connection() -> dict:
    with driver.session(database=NEO4J_DATABASE) as session:
        node_count = session.run(
            "MATCH (n:Entity) RETURN count(n) AS c"
        ).single()["c"]

        relationship_count = session.run(
            "MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c"
        ).single()["c"]

        return {
            "node_count": int(node_count),
            "relationship_count": int(relationship_count),
        }


def fetch_schema() -> dict:
    with driver.session(database=NEO4J_DATABASE) as session:
        labels = session.run(
            """
            CALL db.labels() YIELD label
            RETURN collect(label) AS labels
            """
        ).single()["labels"]

        relationship_types = session.run(
            """
            CALL db.relationshipTypes() YIELD relationshipType
            RETURN collect(relationshipType) AS relationship_types
            """
        ).single()["relationship_types"]

        property_keys = session.run(
            """
            CALL db.propertyKeys() YIELD propertyKey
            RETURN collect(propertyKey) AS property_keys
            """
        ).single()["property_keys"]

    return {
        "labels": labels,
        "relationship_types": relationship_types,
        "property_keys": property_keys,
    }


def fetch_node_types() -> dict:
    """
    从 Neo4j 数据库读取节点类型。
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        records = session.run(
            """
            MATCH (n:Entity)
            WITH
              coalesce(n.entity_type, n.type, head(labels(n)), 'Entity') AS type,
              count(n) AS count
            RETURN type, count
            ORDER BY count DESC, type ASC
            """
        )

        types = []

        for record in records:
            type_value = clean_text(record["type"])
            count = int(record["count"])

            types.append(
                {
                    "value": type_value,
                    "label": type_value,
                    "count": count,
                }
            )

        return {
            "types": types,
            "count": len(types),
        }


def fetch_all_nodes_for_search(
    keyword: str,
    entity_type: str | None = None,
) -> list[dict]:
    """Match across all Entity nodes before ranking; never truncate by storage order."""
    with driver.session(database=NEO4J_DATABASE, default_access_mode="READ") as session:
        query = """
        MATCH (n:Entity)
        WHERE ($entity_type IS NULL
               OR n.entity_type = $entity_type
               OR n.type = $entity_type)
        WITH n, [v IN [n.id, n.entity_name, n.name, n.label, n.title]
                 WHERE v IS NOT NULL | toLower(toString(v))] AS names
        WHERE any(name IN names WHERE name CONTAINS $keyword)
           OR toLower(coalesce(toString(n.description), '')) CONTAINS $keyword
           OR toLower(coalesce(toString(n.keywords), '')) CONTAINS $keyword
        RETURN n, any(name IN names WHERE name = $keyword) AS exact_name,
               any(name IN names WHERE name CONTAINS $keyword) AS name_match
        ORDER BY elementId(n)
        """
        records = session.run(
            query,
            keyword=keyword.lower(),
            entity_type=entity_type,
        )
        return [
            {
                **serialize_node(record["n"]),
                "_search_exact_name": record["exact_name"],
                "_search_name_match": record["name_match"],
            }
            for record in records
        ]


def fetch_graph_from_neo4j(
    node_limit: int = 100,
    edge_limit: int = 500,
    center_id: str | None = None,
    entity_type: str | None = None,
    depth: int = 2,
) -> dict:
    nodes_map: dict[str, dict] = {}
    edges_map: dict[str, dict] = {}

    depth = max(1, min(int(depth), 4))

    with driver.session(database=NEO4J_DATABASE) as session:

        # ==================================================
        # 情况 1：以中心节点扩展图谱
        # 只返回中心节点 + 直接/间接相关节点
        # ==================================================
        if center_id:
            neighbor_limit = max(1, node_limit - 1)

            expand_query = f"""
            MATCH (center:Entity)
            WHERE
              coalesce(center.id, '') = $center_id
              OR coalesce(center.entity_name, '') = $center_id
              OR coalesce(center.name, '') = $center_id
              OR coalesce(center.label, '') = $center_id
              OR coalesce(center.title, '') = $center_id

            CALL {{
              WITH center
              MATCH path = (center)-[*1..{depth}]-(n:Entity)
              WHERE
                $entity_type IS NULL
                OR n.entity_type = $entity_type
                OR n.type = $entity_type
              WITH n, min(length(path)) AS distance, coalesce(n.pagerank, 0) AS pr
              ORDER BY distance ASC, pr DESC
              LIMIT $neighbor_limit
              RETURN collect(DISTINCT n) AS neighbors
            }}

            WITH [center] + neighbors AS ns

            OPTIONAL MATCH (a:Entity)-[r]-(b:Entity)
            WHERE a IN ns AND b IN ns

            RETURN ns,
              collect(
                DISTINCT CASE
                  WHEN r IS NULL THEN []
                  ELSE [startNode(r), r, endNode(r)]
                END
              ) AS rels
            """

            record = session.run(
                expand_query,
                center_id=center_id,
                entity_type=entity_type,
                neighbor_limit=neighbor_limit,
            ).single()

            if record is None:
                return {
                    "nodes": [],
                    "edges": [],
                    "meta": {
                        "node_count": 0,
                        "edge_count": 0,
                        "center_id": center_id,
                        "depth": depth,
                        "mode": "center_expand",
                        "message": "没有找到中心节点",
                    },
                }

            ns = record["ns"] or []
            rels = record["rels"] or []

            for n in ns:
                sn = serialize_node(n)
                nodes_map[sn["id"]] = sn

            for triple in rels:
                if not triple or len(triple) != 3:
                    continue

                a, r, b = triple

                if r is None or a is None or b is None:
                    continue

                sr = serialize_relationship(r, a, b)

                if sr["source"] in nodes_map and sr["target"] in nodes_map:
                    edges_map[sr["id"]] = sr

            return {
                "nodes": list(nodes_map.values())[:node_limit],
                "edges": list(edges_map.values())[:edge_limit],
                "meta": {
                    "node_count": len(nodes_map),
                    "edge_count": len(edges_map),
                    "center_id": center_id,
                    "depth": depth,
                    "node_limit": node_limit,
                    "edge_limit": edge_limit,
                    "mode": "center_expand",
                },
            }

        # ==================================================
        # 情况 2：默认展示图谱
        # ==================================================
        node_query = """
        MATCH (n:Entity)
        WHERE
          $entity_type IS NULL
          OR n.entity_type = $entity_type
          OR n.type = $entity_type
        RETURN n
        LIMIT $node_limit
        """

        node_records = session.run(
            node_query,
            node_limit=node_limit,
            entity_type=entity_type,
        )

        for record in node_records:
            n = record["n"]
            sn = serialize_node(n)
            nodes_map[sn["id"]] = sn

        rel_query = """
        MATCH (a:Entity)-[r]-(b:Entity)
        RETURN a, r, b
        LIMIT $edge_limit
        """

        rel_records = session.run(
            rel_query,
            edge_limit=edge_limit,
        )

        for record in rel_records:
            a = record["a"]
            r = record["r"]
            b = record["b"]

            sa = serialize_node(a)
            sb = serialize_node(b)

            if sa["id"] in nodes_map and sb["id"] in nodes_map:
                sr = serialize_relationship(r, a, b)
                edges_map[sr["id"]] = sr

    nodes = list(nodes_map.values())[:node_limit]
    allowed_ids = set(node["id"] for node in nodes)

    edges = [
        edge
        for edge in edges_map.values()
        if edge["source"] in allowed_ids and edge["target"] in allowed_ids
    ][:edge_limit]

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "center_id": center_id,
            "entity_type": entity_type,
            "node_limit": node_limit,
            "edge_limit": edge_limit,
            "depth": depth,
            "mode": "default",
        },
    }


# =========================
# API 路由
# =========================

@app.get("/api/health")
def health():
    try:
        stat = check_neo4j_connection()

        return {
            "ok": True,
            "source": "neo4j",
            "neo4j_uri": NEO4J_URI,
            "neo4j_database": NEO4J_DATABASE,
            "neo4j_user": NEO4J_USER,
            "embedding_search": USE_EMBEDDING_SEARCH,
            "frontend_dist": str(FRONTEND_DIST),
            "frontend_dist_exists": FRONTEND_DIST.exists(),
            **stat,
        }

    except Exception as exc:
        return {
            "ok": False,
            "source": "neo4j",
            "neo4j_uri": NEO4J_URI,
            "neo4j_database": NEO4J_DATABASE,
            "neo4j_user": NEO4J_USER,
            "frontend_dist": str(FRONTEND_DIST),
            "frontend_dist_exists": FRONTEND_DIST.exists(),
            "error": str(exc),
        }


@app.get("/api/schema")
def schema():
    try:
        return fetch_schema()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/node-types")
def node_types():
    try:
        return fetch_node_types()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/search")
def search(
    keyword: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    entity_type: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
):
    try:
        keyword = clean_text(keyword)
        if not keyword:
            return {"keyword": "", "count": 0, "results": [],
                    "total": 0, "has_more": False, "offset": offset}
        candidates = fetch_all_nodes_for_search(
            keyword=keyword,
            entity_type=clean_text(entity_type) if entity_type else None,
        )
        ranked = rank_nodes(keyword, candidates, offset + limit)[offset:offset + limit]
        results = [
            {key: value for key, value in node.items()
             if not key.startswith("_search_")}
            for node in ranked
        ]
        return {
            "keyword": keyword,
            "count": len(results),
            "results": results,
            "total": len(candidates),
            "has_more": offset + len(results) < len(candidates),
            "offset": offset,
            "search_mode": "embedding_rerank" if any(
                node.get("score_detail", {}).get("embedding") is not None
                for node in results
            ) else "keyword",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/graph")
def graph(
    refresh: bool = Query(default=False),
    node_limit: int = Query(default=100, ge=1, le=2000),
    edge_limit: int = Query(default=500, ge=1, le=10000),
    center_id: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    depth: int = Query(default=2, ge=1, le=4),
):
    cache_key = {
        "node_limit": node_limit,
        "edge_limit": edge_limit,
        "center_id": center_id,
        "entity_type": entity_type,
        "depth": depth,
    }

    use_cache = not refresh and not center_id and not entity_type

    now = time.time()

    if (
        use_cache
        and _cache["data"] is not None
        and _cache["key"] == cache_key
        and now - _cache["time"] < CACHE_SECONDS
    ):
        return _cache["data"]

    try:
        data = fetch_graph_from_neo4j(
            node_limit=node_limit,
            edge_limit=edge_limit,
            center_id=clean_text(center_id) if center_id else None,
            entity_type=clean_text(entity_type) if entity_type else None,
            depth=depth,
        )

        if use_cache:
            _cache["time"] = now
            _cache["key"] = cache_key
            _cache["data"] = data

        return data

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# =========================
# 托管前端 dist
# =========================

if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"

    if assets_dir.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="assets",
        )


@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    """
    让 FastAPI 同时提供前端页面。
    访问 http://127.0.0.1:8000/ 时返回 React 页面。
    """
    target_file = FRONTEND_DIST / full_path

    if target_file.is_file():
        return FileResponse(target_file)

    index_file = FRONTEND_DIST / "index.html"

    if index_file.exists():
        return FileResponse(index_file)

    raise HTTPException(
        status_code=404,
        detail=f"前端 dist 不存在，请先运行 npm run build。当前路径：{FRONTEND_DIST}",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )

