"""Hybrid SQLite FTS5 + BM25 + lightweight local vector retriever for engineering SOPs."""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

from data.seed_db import DB_PATH as DEFAULT_DB_PATH
from data.seed_db import RUNBOOKS_DIR as DEFAULT_RUNBOOKS_DIR
from data.seed_db import RunbookChunk, load_all_runbook_chunks
from src.resilience import DB_TIMEOUT_SECONDS, execute_with_timeout

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", re.IGNORECASE)


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    expanded: list[str] = []
    for tok in tokens:
        expanded.append(tok)
        if "-" in tok or "_" in tok:
            for part in re.split(r"[-_]", tok):
                if part:
                    expanded.append(part)
    return expanded


def _char_trigram_vector(text: str) -> dict[str, float]:
    """Compute L2-normalized character 3-gram + word TF vector without heavy ML deps."""
    cleaned = re.sub(r"\s+", " ", text.lower()).strip()
    if not cleaned:
        return {}
    counts: Counter[str] = Counter()
    for tok in _tokenize(cleaned):
        counts[f"w:{tok}"] += 1.5
    padded = f" {cleaned} "
    for i in range(max(0, len(padded) - 2)):
        counts[f"c:{padded[i:i+3]}"] += 1.0
    norm = math.sqrt(sum(v * v for v in counts.values()))
    if norm == 0.0:
        return {}
    return {k: v / norm for k, v in counts.items()}


def _cosine_sim(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    return sum(val * vec_b.get(key, 0.0) for key, val in vec_a.items())


def _sanitize_fts5_query(query: str) -> str:
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", query) if len(t) >= 2]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:12])


class KnowledgeBaseConnector:
    """Hybrid retriever combining SQLite FTS5, BM25Okapi, and lightweight local n-gram vectors."""

    def __init__(
        self,
        runbooks_dir: Path | str = DEFAULT_RUNBOOKS_DIR,
        db_path: Path | str = DEFAULT_DB_PATH,
        timeout_seconds: float = DB_TIMEOUT_SECONDS,
    ) -> None:
        self.runbooks_dir = Path(runbooks_dir)
        self.db_path = Path(db_path)
        self.timeout_seconds = float(timeout_seconds)
        self._chunks: list[RunbookChunk] = []
        self._chunk_by_id: dict[str, RunbookChunk] = {}
        self._bm25: BM25Okapi | None = None
        self._vectors: list[dict[str, float]] = []
        self._indexed = False

    def _ensure_index(self) -> None:
        if self._indexed:
            return
        self._chunks = load_all_runbook_chunks(self.runbooks_dir)
        self._chunk_by_id = {c.chunk_id: c for c in self._chunks}
        corpus_tokens = [
            _tokenize(f"{c.doc_title} {c.section_heading} {c.content}") for c in self._chunks
        ]
        if corpus_tokens:
            self._bm25 = BM25Okapi(corpus_tokens)
            self._vectors = [
                _char_trigram_vector(f"{c.doc_title} {c.section_heading} {c.content}")
                for c in self._chunks
            ]
        self._indexed = True

    def list_catalog(self) -> list[dict[str, Any]]:
        """Return metadata catalog of all indexed SOP runbooks."""
        self._ensure_index()
        docs: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks:
            entry = docs.setdefault(
                chunk.doc_id,
                {
                    "doc_id": chunk.doc_id,
                    "doc_title": chunk.doc_title,
                    "file_path": chunk.file_path,
                    "sections": [],
                },
            )
            entry["sections"].append(
                {
                    "section_heading": chunk.section_heading,
                    "line_start": chunk.line_start,
                    "line_end": chunk.line_end,
                    "citation": chunk.citation,
                }
            )
        return list(docs.values())

    def _fts5_scores(self, query: str) -> dict[str, float]:
        fts_query = _sanitize_fts5_query(query)
        if not fts_query or not self.db_path.exists():
            return {}
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runbooks_fts'"
            ).fetchone()
            if not exists:
                return {}
            rows = conn.execute(
                """
                SELECT chunk_id, -bm25(runbooks_fts) AS rank_score
                FROM runbooks_fts
                WHERE runbooks_fts MATCH ?
                ORDER BY rank_score DESC
                LIMIT 25
                """,
                (fts_query,),
            ).fetchall()
            if not rows:
                return {}
            max_score = max(max(float(r[1]), 1e-9) for r in rows)
            return {r[0]: max(0.0, float(r[1])) / max_score for r in rows}
        except sqlite3.OperationalError:
            return {}
        finally:
            conn.close()

    def _search_sync(self, query: str, top_k: int = 5) -> dict[str, Any]:
        self._ensure_index()
        if not query or not query.strip():
            return {"query": query, "total_indexed_chunks": len(self._chunks), "results": []}
        if not self._chunks:
            return {"query": query, "total_indexed_chunks": 0, "results": []}

        top_k = max(1, min(int(top_k), 15))
        q_tokens = _tokenize(query)
        q_vec = _char_trigram_vector(query)

        # 1. BM25Okapi lexical scores
        bm25_raw = self._bm25.get_scores(q_tokens) if (self._bm25 and q_tokens) else [0.0] * len(self._chunks)
        bm25_max = max(float(max(bm25_raw)), 1e-9)
        bm25_norm = [max(0.0, float(s)) / bm25_max for s in bm25_raw]

        # 2. Local character 3-gram + term vector cosine similarity
        vec_raw = [_cosine_sim(q_vec, doc_vec) for doc_vec in self._vectors]
        vec_max = max(float(max(vec_raw)), 1e-9)
        vec_norm = [float(s) / vec_max for s in vec_raw]

        # 3. SQLite FTS5 porter-stemmed rank
        fts_scores = self._fts5_scores(query)

        # Rank lists for Reciprocal Rank Fusion (RRF)
        bm25_ranks = {
            idx: rank
            for rank, idx in enumerate(
                sorted(range(len(self._chunks)), key=lambda i: bm25_norm[i], reverse=True),
                start=1,
            )
            if bm25_norm[idx] > 0
        }
        vec_ranks = {
            idx: rank
            for rank, idx in enumerate(
                sorted(range(len(self._chunks)), key=lambda i: vec_norm[i], reverse=True),
                start=1,
            )
            if vec_norm[idx] > 0
        }
        fts_sorted_ids = [
            cid for cid, _ in sorted(fts_scores.items(), key=lambda kv: kv[1], reverse=True)
        ]
        fts_ranks = {cid: rank for rank, cid in enumerate(fts_sorted_ids, start=1)}

        scored_items: list[tuple[float, int]] = []
        for idx, chunk in enumerate(self._chunks):
            f_score = fts_scores.get(chunk.chunk_id, 0.0)
            b_score = bm25_norm[idx]
            v_score = vec_norm[idx]

            rrf = 0.0
            if idx in bm25_ranks:
                rrf += 1.0 / (20.0 + bm25_ranks[idx])
            if idx in vec_ranks:
                rrf += 1.0 / (20.0 + vec_ranks[idx])
            if chunk.chunk_id in fts_ranks:
                rrf += 1.0 / (20.0 + fts_ranks[chunk.chunk_id])

            hybrid_score = (0.40 * f_score) + (0.35 * b_score) + (0.25 * v_score) + (2.5 * rrf)
            if f_score > 0 or b_score > 0 or v_score > 0.08:
                scored_items.append((hybrid_score, idx))

        scored_items.sort(key=lambda item: item[0], reverse=True)
        hits: list[dict[str, Any]] = []
        for score, idx in scored_items[:top_k]:
            chunk = self._chunks[idx]
            snippet = chunk.content if len(chunk.content) <= 750 else chunk.content[:747] + "..."
            hits.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "doc_title": chunk.doc_title,
                    "section_heading": chunk.section_heading,
                    "line_start": chunk.line_start,
                    "line_end": chunk.line_end,
                    "citation": chunk.citation,
                    "score": round(float(score), 4),
                    "score_breakdown": {
                        "fts5": round(fts_scores.get(chunk.chunk_id, 0.0), 4),
                        "bm25": round(bm25_norm[idx], 4),
                        "local_vector": round(vec_norm[idx], 4),
                    },
                    "snippet": snippet,
                }
            )

        return {
            "query": query,
            "retrieval_mode": "hybrid_fts5_bm25_ngram",
            "total_indexed_chunks": len(self._chunks),
            "result_count": len(hits),
            "results": hits,
        }

    async def search(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """Run hybrid runbook search asynchronously with a strict timeout."""
        return await execute_with_timeout(
            asyncio.to_thread(self._search_sync, query, top_k),
            timeout_seconds=self.timeout_seconds,
            source_name="knowledge_base",
        )
