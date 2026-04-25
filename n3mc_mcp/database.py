import hashlib
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

_CODE_FENCE_RE = re.compile(r"```")


def _contains_code_block(text: str) -> bool:
    """Heuristic: treat triple-backtick fences as a code-block signal."""
    return bool(_CODE_FENCE_RE.search(text))

import redis as redis_lib

from .processor import (
    b_local,
    cjk_bigram_expand,
    chunk_text,
    cosine_sim_from_distance,
    embed,
    final_score,
    keyword_relevance,
    lexical_rerank,
    prepare_query,
    time_decay,
)

INDEX_NAME = "n3mc_idx"

_DOCKER_HINT = (
    "Redis Stack が起動していません。"
    "docker start redis-stack または "
    "docker run -d --name redis-stack -p 6379:6379 redis/redis-stack-server:latest "
    "を実行してください。"
)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _new_id() -> str:
    try:
        from uuid_utils import uuid7
        return str(uuid7())
    except ImportError:
        return str(uuid.uuid4())


def _escape_tag(val: str) -> str:
    # Hyphens in UUID TAG values do not need escaping in Redis Stack 7.x DIALECT 2
    special = set(".,@!(){}|^?~&*+#$%")
    return "".join(("\\" + ch if ch in special else ch) for ch in val)


def _parse_fields(flist) -> dict[str, str]:
    result: dict[str, str] = {}
    if not flist:
        return result
    for j in range(0, len(flist) - 1, 2):
        k = flist[j].decode("utf-8") if isinstance(flist[j], bytes) else str(flist[j])
        v = flist[j + 1].decode("utf-8") if isinstance(flist[j + 1], bytes) else str(flist[j + 1])
        result[k] = v
    return result


def _to_float(v, default: float = 1.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _to_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


class Database:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._client: Optional[redis_lib.Redis] = None
        self._ok = False

    # ── connection ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._client = redis_lib.Redis.from_url(
                self.cfg["redis_url"], decode_responses=False
            )
            self._client.ping()
            self._ok = True
            return True
        except Exception as e:
            print(f"[n3mc] Redis connect failed: {e}", file=sys.stderr)
            print(f"[n3mc] Hint: {_DOCKER_HINT}", file=sys.stderr)
            self._ok = False
            return False

    def enforce_ephemeral(self) -> None:
        if not self._ok:
            return
        try:
            self._client.config_set("appendonly", "no")
            self._client.config_set("save", "")
        except Exception as e:
            print(f"[n3mc] enforce_ephemeral: {e}", file=sys.stderr)

    def ensure_index(self) -> None:
        if not self._ok:
            return
        try:
            self._client.execute_command(
                "FT.CREATE", INDEX_NAME,
                "ON", "HASH", "PREFIX", "1", "mem:",
                "SCHEMA",
                "content", "TEXT", "WEIGHT", "1.0",
                "content_ngram", "TEXT", "WEIGHT", "0.8",
                "timestamp_epoch", "NUMERIC", "SORTABLE",
                "owner_id", "TAG",
                "local_id", "TAG",
                "agent_name", "TAG",
                "session_id", "TAG",
                "importance", "NUMERIC",
                "access_count", "NUMERIC",
                "parent_id", "TAG",
                "embedding", "VECTOR", "FLAT", "6",
                "TYPE", "FLOAT32",
                "DIM", "768",
                "DISTANCE_METRIC", "COSINE",
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise

    # ── save ────────────────────────────────────────────────────────────────

    def save_memory(
        self,
        content: str,
        agent_name: str = "",
        owner_id: str = "",
        importance: float = 1.0,
        session_id: str = "",
    ) -> dict:
        if not self._ok:
            return {"status": "error", "saved": False, "reason": _DOCKER_HINT}

        content = content.strip()
        if not content:
            return {"status": "error", "saved": False, "reason": "empty content"}

        cfg_owner = self.cfg["owner_id"]
        if owner_id and owner_id != cfg_owner:
            return {"status": "error", "saved": False, "reason": "owner_id mismatch"}
        owner_id = cfg_owner

        effective_session = session_id.strip() or self.cfg.get("_session_id", "")

        importance = max(0.5, min(2.0, importance))

        if self.cfg.get("skip_code_blocks", False) and _contains_code_block(content):
            return {
                "status": "skipped_code",
                "saved": False,
                "reason": "content contains a code fence (```); skip_code_blocks=true",
            }

        # No `purify()` step: the spec's §3.11 verbatim-recall contract
        # requires full fidelity of what the user saved — stripping code
        # blocks would break round-tripping of specs/docs/snippets. The
        # BM25 tokenizer tolerates code-punctuation just fine.
        threshold = self.cfg.get("chunk_threshold", 400)

        if len(content) <= threshold:
            return self._save_single(content, agent_name, owner_id, importance, effective_session)
        return self._save_parent_chunks(content, agent_name, owner_id, importance, effective_session)

    def _save_single(
        self, content: str, agent_name: str, owner_id: str, importance: float, session_id: str
    ) -> dict:
        sha = _sha1(content)
        sha_key = f"mem:sha:{sha}"

        if self._client.exists(sha_key):
            return {"status": "duplicate", "saved": False}

        vec_bytes = embed(content, is_query=False)
        if vec_bytes:
            near = self._near_dedup(vec_bytes, owner_id)
            if near is not None:
                return {"status": "near_duplicate", "saved": False, "similarity": near}

        mem_id = _new_id()
        ngram = cjk_bigram_expand(content)
        now_iso = _now_iso()
        now_epoch = _now_epoch()
        ttl = self.cfg["ttl_seconds"]

        fields: dict = {
            b"id": mem_id.encode(),
            b"content": content.encode("utf-8"),
            b"content_ngram": ngram.encode("utf-8"),
            b"timestamp": now_iso.encode(),
            b"timestamp_epoch": str(now_epoch).encode(),
            b"owner_id": owner_id.encode(),
            b"local_id": self.cfg.get("local_id", "").encode(),
            b"agent_name": (agent_name or "").encode(),
            b"session_id": session_id.encode(),
            b"importance": str(importance).encode(),
            b"access_count": b"0",
            b"parent_id": b"",
        }
        if vec_bytes:
            fields[b"embedding"] = vec_bytes

        mem_key = f"mem:{mem_id}"
        pipe = self._client.pipeline()
        pipe.hset(mem_key, mapping=fields)
        pipe.expire(mem_key, ttl)
        pipe.set(sha_key, mem_id.encode(), ex=ttl)
        pipe.execute()

        return {"status": "saved", "saved": True, "id": mem_id, "ttl_seconds": ttl}

    def _save_parent_chunks(
        self, content: str, agent_name: str, owner_id: str, importance: float, session_id: str
    ) -> dict:
        sha = _sha1(content)
        docsha_key = f"docsha:{sha}"

        if self._client.exists(docsha_key):
            existing = self._client.get(docsha_key)
            parent_id = existing.decode() if isinstance(existing, bytes) else str(existing)
            return {"status": "duplicate", "saved": False, "parent_id": parent_id}

        # Semantic near-dedup on the full body (symmetric with _save_single).
        # e5-base-v2 truncates at ~512 tokens — enough to fingerprint the
        # document's opening. KNN against indexed chunks surfaces prior
        # parents whose chunks embed near the new full-content vector.
        full_vec = embed(content, is_query=False)
        if full_vec:
            near = self._near_dedup(full_vec, owner_id)
            if near is not None:
                return {"status": "near_duplicate", "saved": False, "similarity": near}

        threshold = self.cfg.get("chunk_threshold", 400)
        overlap = self.cfg.get("chunk_overlap", 100)
        chunks = chunk_text(content, threshold, overlap)

        parent_id = _new_id()
        ttl = self.cfg["ttl_seconds"]
        now_iso = _now_iso()
        now_epoch = _now_epoch()
        local_id = self.cfg.get("local_id", "")

        doc_key = f"doc:{parent_id}"
        pipe = self._client.pipeline()
        pipe.hset(
            doc_key,
            mapping={
                b"id": parent_id.encode(),
                b"content": content.encode("utf-8"),
                b"timestamp": now_iso.encode(),
                b"timestamp_epoch": str(now_epoch).encode(),
                b"owner_id": owner_id.encode(),
                b"local_id": local_id.encode(),
                b"agent_name": (agent_name or "").encode(),
                b"session_id": session_id.encode(),
                b"chunk_count": str(len(chunks)).encode(),
            },
        )
        pipe.expire(doc_key, ttl)
        pipe.set(docsha_key, parent_id.encode(), ex=ttl)
        pipe.execute()

        # Build all chunk writes in a single pipeline to minimise round-trips
        # and ensure every mem: key gets its TTL set atomically with the hset.
        chunk_ids: list[str] = []
        chunk_pipe = self._client.pipeline()
        for chunk in chunks:
            chunk_id = _new_id()
            ngram = cjk_bigram_expand(chunk)
            vec_bytes = embed(chunk, is_query=False)
            fields: dict = {
                b"id": chunk_id.encode(),
                b"content": chunk.encode("utf-8"),
                b"content_ngram": ngram.encode("utf-8"),
                b"timestamp": now_iso.encode(),
                b"timestamp_epoch": str(now_epoch).encode(),
                b"owner_id": owner_id.encode(),
                b"local_id": local_id.encode(),
                b"agent_name": (agent_name or "").encode(),
                b"session_id": session_id.encode(),
                b"importance": str(importance).encode(),
                b"access_count": b"0",
                b"parent_id": parent_id.encode(),
            }
            if vec_bytes:
                fields[b"embedding"] = vec_bytes
            chunk_pipe.hset(f"mem:{chunk_id}", mapping=fields)
            chunk_pipe.expire(f"mem:{chunk_id}", ttl)
            chunk_ids.append(chunk_id)
        chunk_pipe.execute()

        return {
            "status": "saved",
            "saved": True,
            "parent_id": parent_id,
            "chunks": len(chunks),
            "saved_count": len(chunks),
            "ids": chunk_ids,
            "ttl_seconds": ttl,
        }

    def _near_dedup(self, vec_bytes: bytes, owner_id: str) -> Optional[float]:
        # owner_id TAG filter omitted — UUID hyphens cause parse errors in RediSearch.
        # We fetch KNN=5 and filter by owner_id in Python.
        threshold = self.cfg.get("dedup_threshold", 0.95)
        try:
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                "*=>[KNN 5 @embedding $vec AS __dist]",
                "PARAMS", "2", "vec", vec_bytes,
                "RETURN", "2", "__dist", "owner_id",
                "SORTBY", "__dist",
                "LIMIT", "0", "5",
                "DIALECT", "2",
            )
            if res and res[0] > 0:
                i = 1
                while i + 1 < len(res):
                    fdict = _parse_fields(res[i + 1])
                    if fdict.get("owner_id") != owner_id:
                        i += 2
                        continue
                    dist = _to_float(fdict.get("__dist", "1.0"))
                    sim = cosine_sim_from_distance(dist)
                    if sim >= threshold:
                        return sim
                    i += 2
        except Exception:
            pass
        return None

    # ── search ──────────────────────────────────────────────────────────────

    def search_memory(
        self,
        query: str,
        limit: Optional[int] = None,
        session_id: str = "",
    ) -> list[dict]:
        if not self._ok:
            return []

        if limit is None:
            limit = self.cfg.get("search_result_limit", 20)

        query = query[: self.cfg.get("search_query_max_chars", 2000)]
        owner_id = self.cfg["owner_id"]
        half_life = self.cfg.get("half_life_days", 3)
        min_score_val = self.cfg.get("min_score", 0.2)
        bm25_thr = self.cfg.get("bm25_min_threshold", 0.1)
        # Per-row b_session ranking: rows whose stored session_id matches
        # the request's effective session get b_match (default 1.0); the
        # rest get b_mismatch (default 0.6). Resolution order for the
        # "effective session" matches save_memory and Pro spec §3.1:
        #   (1) per-call argument
        #   (2) N3MC_SESSION_ID env var (resolved into cfg["_session_id"])
        #   (3) per-process UUIDv4 fallback (also in cfg["_session_id"])
        effective_session = session_id.strip() or self.cfg.get("_session_id", "")
        b_sess_match = self.cfg.get("b_session_match", 1.0)
        b_sess_mismatch = self.cfg.get("b_session_mismatch", 0.6)

        vec_results = self._vector_search(query, owner_id, limit)
        bm25_results = self._bm25_search(query, owner_id, limit)

        all_keys = set(vec_results) | set(bm25_results)
        max_bm25 = max((v["bm25_score"] for v in bm25_results.values()), default=1.0)

        candidates: list[dict] = []
        for key in all_keys:
            vr = vec_results.get(key, {})
            br = bm25_results.get(key, {})
            content = vr.get("content") or br.get("content", "")
            timestamp = vr.get("timestamp") or br.get("timestamp", "")
            imp = _to_float(vr.get("importance") or br.get("importance", 1.0))
            acc = _to_int(vr.get("access_count") or br.get("access_count", 0))
            parent_id = vr.get("parent_id") or br.get("parent_id", "")
            mem_id = vr.get("id") or br.get("id", "")

            cos = vr.get("cos_sim", 0.0)
            bm25 = br.get("bm25_score", 0.0)
            kw = keyword_relevance(bm25, max_bm25, bm25_thr)
            decay = time_decay(timestamp, half_life)
            b = b_local(imp, acc, self.cfg)
            row_session = vr.get("session_id") or br.get("session_id", "")
            b_sess = (
                b_sess_match
                if (effective_session and row_session == effective_session)
                else b_sess_mismatch
            )
            score = final_score(cos, kw, decay, b, b_sess)

            if score >= min_score_val:
                candidates.append({
                    "key": key,
                    "id": mem_id,
                    "content": content,
                    "timestamp": timestamp,
                    "score": score,
                    "parent_id": parent_id,
                    "importance": imp,
                    "access_count": acc,
                })

        # Resolve parent documents BEFORE reranking, so lexical rerank
        # (token coverage + phrase bonus) sees the full verbatim body of
        # parent-doc hits rather than just the chunk that happened to match.
        #
        # Spec §3.11 contract:
        #   1. When multiple chunks share a parent_id, keep the
        #      HIGHEST-SCORING chunk's base score on the collapsed parent
        #      ("最高スコアのヒットのみ残す"). The merge above iterates an
        #      unordered set, so we sort by score descending here to make
        #      the first chunk encountered for a given parent the best one.
        #   2. If `doc:<pid>` is missing (parent expired or was deleted),
        #      the orphan chunks must each surface as individual memories
        #      ("孤児チャンクは個別メモリとして表示", graceful degrade).
        #      Do NOT mark `seen_parents` in that case, so siblings of a
        #      dead parent each render on their own.
        candidates.sort(key=lambda x: x["score"], reverse=True)

        seen_parents: dict[str, bool] = {}
        resolved: list[dict] = []
        for c in candidates:
            pid = c.get("parent_id", "")
            if pid:
                if pid in seen_parents:
                    continue
                try:
                    doc_data = self._client.hgetall(f"doc:{pid}")
                except Exception:
                    doc_data = None
                if doc_data:
                    seen_parents[pid] = True
                    chunk_count = doc_data.get(b"chunk_count", b"?").decode()
                    c["content"] = doc_data[b"content"].decode("utf-8")
                    c["id"] = pid
                    c["_tag"] = f"[doc×{chunk_count}]"
                    c["_parent_key"] = f"doc:{pid}"
                # else: parent gone — graceful degrade per §3.11. Leave
                # this chunk untouched, do not flag the parent as seen,
                # so any sibling orphan chunks survive too.
            resolved.append(c)
        candidates = resolved

        if self.cfg.get("lexical_rerank_enabled", True):
            candidates = lexical_rerank(
                candidates,
                query,
                self.cfg.get("rerank_weight", 0.3),
                self.cfg.get("rerank_phrase_weight", 0.2),
            )
        else:
            candidates.sort(key=lambda x: x["score"], reverse=True)

        do_ttl_refresh = self.cfg.get("ttl_refresh_on_search", True)
        ttl = self.cfg["ttl_seconds"]
        if do_ttl_refresh:
            top_k = self.cfg.get("ttl_refresh_top_k", 5)
            for c in candidates[:top_k]:
                try:
                    # Refresh the chunk/standalone key and bump its access_count.
                    self._client.expire(c["key"], ttl)
                    self._client.hincrby(c["key"], "access_count", 1)
                    # When this hit resolved to a parent doc, refresh the
                    # doc: key TTL so verbatim recall stays alive alongside
                    # its chunks.
                    parent_key = c.get("_parent_key")
                    if parent_key:
                        self._client.expire(parent_key, ttl)
                except Exception:
                    pass

        final: list[dict] = []
        for c in candidates[:limit]:
            c.pop("_parent_key", None)
            final.append(c)

        return final

    def _vector_search(self, query: str, owner_id: str, limit: int) -> dict[str, dict]:
        # owner_id TAG filter omitted — UUID hyphens cause parse errors in RediSearch.
        # We fetch KNN candidates globally and filter by owner_id in Python.
        vec_bytes = embed(query, is_query=True)
        if not vec_bytes:
            return {}
        knn_limit = min(limit * 5, 100)
        try:
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                f"*=>[KNN {knn_limit} @embedding $vec AS __dist]",
                "PARAMS", "2", "vec", vec_bytes,
                "RETURN", "9", "__dist", "owner_id", "content", "timestamp",
                "importance", "access_count", "parent_id", "id", "session_id",
                "SORTBY", "__dist",
                "LIMIT", "0", str(knn_limit),
                "DIALECT", "2",
            )
        except Exception as e:
            print(f"[n3mc] vector search: {e}", file=sys.stderr)
            return {}

        if not res or res[0] == 0:
            return {}

        out: dict[str, dict] = {}
        i = 1
        while i + 1 < len(res):
            key = res[i].decode() if isinstance(res[i], bytes) else str(res[i])
            fdict = _parse_fields(res[i + 1])
            if fdict.get("owner_id") != owner_id:
                i += 2
                continue
            dist = _to_float(fdict.get("__dist", "1.0"))
            out[key] = {
                "cos_sim": cosine_sim_from_distance(dist),
                "content": fdict.get("content", ""),
                "timestamp": fdict.get("timestamp", ""),
                "importance": _to_float(fdict.get("importance", "1.0")),
                "access_count": _to_int(fdict.get("access_count", "0")),
                "parent_id": fdict.get("parent_id", ""),
                "id": fdict.get("id", ""),
                "session_id": fdict.get("session_id", ""),
            }
            i += 2
        return out

    def _bm25_search(self, query: str, owner_id: str, limit: int) -> dict[str, dict]:
        # owner_id TAG filter omitted — UUID hyphens cause parse errors in RediSearch.
        # We filter by owner_id in Python after fetching BM25 results.
        fts_q = prepare_query(query)
        if not fts_q:
            return {}
        bm25_limit = min(limit * 5, 100)
        fts_query = f"(@content:({fts_q}) | @content_ngram:({fts_q}))"
        try:
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                fts_query,
                "SCORER", "BM25",
                "WITHSCORES",
                "RETURN", "8", "owner_id", "content", "timestamp",
                "importance", "access_count", "parent_id", "id", "session_id",
                "LIMIT", "0", str(bm25_limit),
                "DIALECT", "2",
            )
        except Exception as e:
            print(f"[n3mc] BM25 search: {e}", file=sys.stderr)
            return {}

        if not res or res[0] == 0:
            return {}

        out: dict[str, dict] = {}
        i = 1
        while i + 2 < len(res):
            key = res[i].decode() if isinstance(res[i], bytes) else str(res[i])
            score_raw = res[i + 1]
            score = _to_float(score_raw.decode() if isinstance(score_raw, bytes) else score_raw)
            fdict = _parse_fields(res[i + 2])
            if fdict.get("owner_id") != owner_id:
                i += 3
                continue
            out[key] = {
                "bm25_score": score,
                "content": fdict.get("content", ""),
                "timestamp": fdict.get("timestamp", ""),
                "importance": _to_float(fdict.get("importance", "1.0")),
                "access_count": _to_int(fdict.get("access_count", "0")),
                "parent_id": fdict.get("parent_id", ""),
                "id": fdict.get("id", ""),
                "session_id": fdict.get("session_id", ""),
            }
            i += 3
        return out

    # ── list ────────────────────────────────────────────────────────────────

    def list_memories(self, limit: int = 20) -> list[dict]:
        if not self._ok:
            return []
        owner_id = self.cfg["owner_id"]
        results: list[dict] = []

        try:
            # Use "*" to fetch all indexed memories, filter owner_id and parent_id in Python.
            fetch_limit = max(limit * 5, 200)
            res = self._client.execute_command(
                "FT.SEARCH", INDEX_NAME,
                "*",
                "RETURN", "5", "owner_id", "parent_id", "content", "timestamp", "id",
                "SORTBY", "timestamp_epoch", "DESC",
                "LIMIT", "0", str(fetch_limit),
                "DIALECT", "2",
            )
            if res and res[0] > 0:
                i = 1
                while i + 1 < len(res):
                    fdict = _parse_fields(res[i + 1])
                    # Only independent memories (no parent) belonging to this owner
                    if fdict.get("owner_id") != owner_id or fdict.get("parent_id", ""):
                        i += 2
                        continue
                    results.append({
                        "id": fdict.get("id", ""),
                        "content": fdict.get("content", ""),
                        "timestamp": fdict.get("timestamp", ""),
                        "tag": "",
                    })
                    i += 2
        except Exception as e:
            print(f"[n3mc] list_memories: {e}", file=sys.stderr)

        try:
            cursor = 0
            parents: list[dict] = []
            while True:
                cursor, keys = self._client.scan(cursor, match="doc:*", count=100)
                for key in keys:
                    try:
                        d = self._client.hgetall(key)
                        if d.get(b"owner_id", b"").decode() == owner_id:
                            preview = d[b"content"].decode("utf-8")
                            tag = f"[doc×{d.get(b'chunk_count', b'?').decode()}]"
                            parents.append({
                                "id": d[b"id"].decode(),
                                "content": preview[:200] + ("..." if len(preview) > 200 else ""),
                                "timestamp": d.get(b"timestamp", b"").decode(),
                                "tag": tag,
                            })
                    except Exception:
                        pass
                if cursor == 0:
                    break
            results.extend(parents)
        except Exception as e:
            print(f"[n3mc] parent scan: {e}", file=sys.stderr)

        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return results[:limit]

    # ── delete ──────────────────────────────────────────────────────────────

    def delete_memory(self, mem_id: str) -> dict:
        if not self._ok:
            return {"status": "error", "reason": _DOCKER_HINT}

        doc_key = f"doc:{mem_id}"
        if self._client.exists(doc_key):
            d = self._client.hgetall(doc_key)
            sha_key = None
            try:
                content = d[b"content"].decode("utf-8")
                sha_key = f"docsha:{_sha1(content)}"
            except Exception:
                pass

            chunk_keys: list[str] = []
            esc_pid = _escape_tag(mem_id)
            try:
                res = self._client.execute_command(
                    "FT.SEARCH", INDEX_NAME,
                    f"@parent_id:{{{esc_pid}}}",
                    "RETURN", "0",
                    "LIMIT", "0", "1000",
                    "DIALECT", "2",
                )
                if res and res[0] > 0:
                    i = 1
                    while i + 1 < len(res):
                        raw = res[i]
                        chunk_keys.append(raw.decode() if isinstance(raw, bytes) else str(raw))
                        i += 2
            except Exception:
                pass

            # Fallback: SCAN for any chunks FT.SEARCH missed
            if not chunk_keys:
                cursor = 0
                while True:
                    cursor, keys = self._client.scan(cursor, match="mem:*", count=200)
                    for key in keys:
                        raw_key = key.decode() if isinstance(key, bytes) else str(key)
                        if raw_key.startswith("mem:sha:"):
                            continue
                        try:
                            pid_val = self._client.hget(key, b"parent_id")
                            if pid_val and pid_val.decode() == mem_id:
                                chunk_keys.append(raw_key)
                        except Exception:
                            pass
                    if cursor == 0:
                        break

            pipe = self._client.pipeline()
            pipe.delete(doc_key)
            if sha_key:
                pipe.delete(sha_key)
            for ck in chunk_keys:
                pipe.delete(ck)
            pipe.execute()
            return {"status": "deleted", "id": mem_id, "chunks_deleted": len(chunk_keys)}

        mem_key = f"mem:{mem_id}"
        if not self._client.exists(mem_key):
            return {"status": "not_found", "id": mem_id}

        sha_key = None
        try:
            raw_content = self._client.hget(mem_key, b"content")
            if raw_content:
                content = raw_content.decode("utf-8") if isinstance(raw_content, bytes) else raw_content
                sha_key = f"mem:sha:{_sha1(content)}"
        except Exception:
            pass

        pipe = self._client.pipeline()
        pipe.delete(mem_key)
        if sha_key:
            pipe.delete(sha_key)
        pipe.execute()
        return {"status": "deleted", "id": mem_id}

    # ── bulk delete by session ──────────────────────────────────────────────

    def delete_by_session(self, session_id: str) -> dict:
        """Delete every memory (singles + parent docs + child chunks + sha keys)
        whose session_id matches. Scoped to the configured owner_id.
        """
        if not self._ok:
            return {"status": "error", "reason": _DOCKER_HINT}
        session_id = session_id.strip()
        if not session_id:
            return {"status": "error", "reason": "session_id required"}

        owner_id = self.cfg["owner_id"]
        deleted_singles = 0
        deleted_chunks = 0
        deleted_docs = 0
        sha_keys: list[str] = []
        keys_to_delete: list[str] = []

        # Phase 1: scan mem:* (skip mem:sha:*) and collect matches
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="mem:*", count=200)
            for key in keys:
                raw_key = key.decode() if isinstance(key, bytes) else str(key)
                if raw_key.startswith("mem:sha:"):
                    continue
                try:
                    fields = self._client.hmget(
                        raw_key, b"session_id", b"owner_id", b"parent_id", b"content"
                    )
                    sid = fields[0].decode() if fields[0] else ""
                    oid = fields[1].decode() if fields[1] else ""
                    pid = fields[2].decode() if fields[2] else ""
                    content_b = fields[3]
                except Exception:
                    continue
                if sid != session_id or oid != owner_id:
                    continue
                keys_to_delete.append(raw_key)
                if pid:
                    deleted_chunks += 1
                else:
                    deleted_singles += 1
                    if content_b:
                        try:
                            sha_keys.append(
                                f"mem:sha:{_sha1(content_b.decode('utf-8'))}"
                            )
                        except Exception:
                            pass
            if cursor == 0:
                break

        # Phase 2: scan doc:* for matching session_id
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match="doc:*", count=100)
            for key in keys:
                raw_key = key.decode() if isinstance(key, bytes) else str(key)
                try:
                    fields = self._client.hmget(
                        raw_key, b"session_id", b"owner_id", b"content"
                    )
                    sid = fields[0].decode() if fields[0] else ""
                    oid = fields[1].decode() if fields[1] else ""
                    content_b = fields[2]
                except Exception:
                    continue
                if sid != session_id or oid != owner_id:
                    continue
                keys_to_delete.append(raw_key)
                deleted_docs += 1
                if content_b:
                    try:
                        sha_keys.append(
                            f"docsha:{_sha1(content_b.decode('utf-8'))}"
                        )
                    except Exception:
                        pass
            if cursor == 0:
                break

        if not keys_to_delete and not sha_keys:
            return {
                "status": "not_found",
                "session_id": session_id,
                "deleted": 0,
            }

        pipe = self._client.pipeline()
        for k in keys_to_delete:
            pipe.delete(k)
        for sk in sha_keys:
            pipe.delete(sk)
        pipe.execute()

        return {
            "status": "deleted",
            "session_id": session_id,
            "documents_deleted": deleted_docs,
            "chunks_deleted": deleted_chunks,
            "singles_deleted": deleted_singles,
            "deleted": deleted_singles + deleted_chunks + deleted_docs,
        }

    # ── repair ──────────────────────────────────────────────────────────────

    def repair_memory(self) -> dict:
        if not self._ok:
            return {"status": "error", "message": _DOCKER_HINT}
        try:
            self.ensure_index()
            return {"status": "ok", "message": "index ensured"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
