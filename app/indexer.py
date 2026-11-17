"""Cleaning-indexer service.

Owns ruleset revisions and derived indexes.  A build is staged in a temporary
directory and published atomically, so an interrupted rebuild can never leave
a half-written index visible.  Derived indexes are keyed by
(source_revision_id, ruleset_revision_id) and are immutable once published.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .cleaning import (
    apply_rules,
    canonical_ruleset,
    find_occurrences,
    map_clean_to_source,
)
from .common import (
    AppError,
    RemoteError,
    Response,
    Service,
    connect,
    ensure_parent,
    int_param,
    now_iso,
    q1,
    remote_bytes,
    remote_json,
    row_to_dict,
    rows_to_dicts,
    serve,
    utf8_prefix,
)

INDEX_ENGINE_VERSION = "1"


def ruleset_revision_id(ruleset_id: str, definition_sha256: str) -> str:
    digest = hashlib.sha256(f"{ruleset_id}|{definition_sha256}".encode("utf-8")).hexdigest()
    return "rsr_" + digest[:16]


def index_id(source_revision_id: str, ruleset_revision_id: str, engine: str) -> str:
    digest = hashlib.sha256(
        f"{source_revision_id}|{ruleset_revision_id}|{engine}".encode("utf-8")
    ).hexdigest()
    return "idx_" + digest[:20]


def char_to_byte(text: str, position: int) -> int:
    if position < 0 or position > len(text):
        raise AppError(422, "range_out_of_bounds", f"Character offset {position} outside text")
    return utf8_prefix(text)[position]


class IndexerService(Service):
    name = "indexer"

    def __init__(self, db_path: str, intake_url: str, slice_dir: str):
        super().__init__()
        self.db_path = db_path
        self.intake_url = intake_url.rstrip("/")
        self.slice_dir = slice_dir
        ensure_parent(db_path)
        os.makedirs(slice_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._source_cache: Dict[str, str] = {}
        self._init_db()
        self._recover_incomplete_builds()
        self._register_routes()

    # -- schema -----------------------------------------------------------
    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rulesets (
                    ruleset_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    definition_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ruleset_revisions (
                    ruleset_revision_id TEXT PRIMARY KEY,
                    ruleset_id TEXT NOT NULL REFERENCES rulesets(ruleset_id),
                    revision_number INTEGER NOT NULL,
                    definition_json TEXT NOT NULL,
                    definition_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (ruleset_id, revision_number),
                    UNIQUE (ruleset_id, definition_sha256)
                );
                CREATE TABLE IF NOT EXISTS build_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_revision_id TEXT NOT NULL,
                    ruleset_revision_id TEXT NOT NULL,
                    index_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS indexes (
                    index_id TEXT PRIMARY KEY,
                    source_revision_id TEXT NOT NULL,
                    ruleset_revision_id TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_byte_length INTEGER NOT NULL,
                    source_char_length INTEGER NOT NULL,
                    clean_sha256 TEXT NOT NULL,
                    clean_byte_length INTEGER NOT NULL,
                    clean_char_length INTEGER NOT NULL,
                    paragraph_count INTEGER NOT NULL,
                    mapping_segments INTEGER NOT NULL,
                    mapping_bytes INTEGER NOT NULL,
                    deleted_ranges INTEGER NOT NULL,
                    ruleset_definition_sha256 TEXT NOT NULL,
                    rule_stats_json TEXT NOT NULL,
                    integrity_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (source_revision_id, ruleset_revision_id, engine_version)
                );
                CREATE TABLE IF NOT EXISTS paragraph_index (
                    index_id TEXT NOT NULL REFERENCES indexes(index_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    clean_char_start INTEGER NOT NULL,
                    clean_char_end INTEGER NOT NULL,
                    clean_byte_start INTEGER NOT NULL,
                    clean_byte_end INTEGER NOT NULL,
                    source_char_start INTEGER NOT NULL,
                    source_char_end INTEGER NOT NULL,
                    source_byte_start INTEGER NOT NULL,
                    source_byte_end INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (index_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS paragraph_text ON paragraph_index(index_id, ordinal);
                CREATE TABLE IF NOT EXISTS clean_text (
                    index_id TEXT PRIMARY KEY REFERENCES indexes(index_id) ON DELETE CASCADE,
                    text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deleted_ranges (
                    index_id TEXT NOT NULL REFERENCES indexes(index_id) ON DELETE CASCADE,
                    range_order INTEGER NOT NULL,
                    source_char_start INTEGER NOT NULL,
                    source_char_end INTEGER NOT NULL,
                    source_byte_start INTEGER NOT NULL,
                    source_byte_end INTEGER NOT NULL,
                    PRIMARY KEY (index_id, range_order)
                );
                """
            )
            conn.commit()

    def _recover_incomplete_builds(self) -> None:
        """Any job left in a running state died with a previous process.

        Published indexes are unaffected because publication is a single
        transaction; staged files are discarded.
        """
        with connect(self.db_path) as conn:
            stale = conn.execute(
                "SELECT * FROM build_jobs WHERE status IN ('queued','running')"
            ).fetchall()
            for job in stale:
                conn.execute(
                    """UPDATE build_jobs SET status='interrupted', stage='abandoned',
                       message=?, updated_at=?, finished_at=? WHERE job_id=?""",
                    (
                        "Process stopped before publication; no partial index was published. Rebuild to retry.",
                        now_iso(),
                        now_iso(),
                        job["job_id"],
                    ),
                )
            conn.commit()
        # Remove stage directories not referenced by a finished index
        try:
            for name in os.listdir(self.slice_dir):
                if name.startswith("staging-"):
                    shutil.rmtree(os.path.join(self.slice_dir, name), ignore_errors=True)
        except FileNotFoundError:
            pass

    # -- helpers ----------------------------------------------------------
    def _fetch_source(self, revision_id: str) -> Tuple[bytes, str]:
        url = f"{self.intake_url}/api/revisions/{revision_id}/source"
        try:
            raw, headers = remote_bytes(url)
            sha = headers.get("X-Source-Sha256", "")
        except RemoteError as exc:
            if exc.status == 404:
                raise AppError(404, "source_revision_not_found", f"Unknown source revision {revision_id}")
            raise AppError(502, "intake_unavailable", f"Intake service error: {exc}")
        return raw, sha

    def _source_text(self, revision_id: str, expected_sha: str) -> str:
        """Read the immutable source locked into the index, verifying its hash.

        The index pins (revision_id, sha256).  If intake ever serves different
        bytes, the build/query fails loudly instead of mapping to new text.
        """
        cached = self._source_cache.get(revision_id)
        if cached is not None:
            if hashlib.sha256(cached.encode("utf-8")).hexdigest() != expected_sha:
                raise AppError(
                    409,
                    "source_revision_changed",
                    "Cached source no longer matches the hash pinned by the index",
                    {"revision_id": revision_id, "pinned_sha256": expected_sha},
                )
            return cached
        raw, upstream_sha = self._fetch_source(revision_id)
        actual = hashlib.sha256(raw).hexdigest()
        if upstream_sha and upstream_sha != actual:
            raise AppError(
                409,
                "source_hash_mismatch",
                "Intake served bytes that do not match its own recorded hash",
                {"recorded": upstream_sha, "fetched": actual},
            )
        if actual != expected_sha:
            raise AppError(
                409,
                "source_revision_changed",
                "Source revision no longer has the bytes this index was built from",
                {"revision_id": revision_id, "pinned_sha256": expected_sha, "served_sha256": actual},
            )
        text = raw.decode("utf-8")
        self._source_cache[revision_id] = text
        return text

    def _ruleset_revision(self, conn: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM ruleset_revisions WHERE ruleset_revision_id=?", (revision_id,)
        ).fetchone()
        if row is None:
            raise AppError(404, "ruleset_revision_not_found", f"Unknown ruleset revision {revision_id}")
        return row

    # -- routes -----------------------------------------------------------
    def _register_routes(self) -> None:
        self.route("GET", r"/api/health", self.health)
        self.route("GET", r"/api/rulesets", self.list_rulesets)
        self.route("POST", r"/api/rulesets", self.create_ruleset)
        self.route("GET", r"/api/rulesets/([^/]+)", self.get_ruleset)
        self.route("POST", r"/api/rulesets/([^/]+)/revisions", self.revise_ruleset)
        self.route("GET", r"/api/ruleset-revisions/([^/]+)", self.get_ruleset_revision)
        self.route("POST", r"/api/builds", self.start_build)
        self.route("GET", r"/api/builds", self.list_builds)
        self.route("GET", r"/api/builds/([^/]+)", self.get_build)
        self.route("POST", r"/api/builds/([^/]+)/retry", self.retry_build)
        self.route("GET", r"/api/indexes", self.list_indexes)
        self.route("GET", r"/api/indexes/([^/]+)", self.get_index)
        self.route("GET", r"/api/indexes/([^/]+)/paragraphs", self.get_paragraphs)
        self.route("GET", r"/api/indexes/([^/]+)/clean", self.get_clean)
        self.route("GET", r"/api/indexes/([^/]+)/map", self.get_map)
        self.route("POST", r"/api/indexes/([^/]+)/locate", self.locate)
        self.route("POST", r"/api/indexes/([^/]+)/quote", self.quote)
        self.route("GET", r"/api/storage", self.storage_cost)

    def health(self, match, query, body) -> Response:
        return Response({"service": self.name, "ok": True, "time": now_iso()})

    # -- rulesets ---------------------------------------------------------
    def _ruleset_summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        revisions = conn.execute(
            """SELECT ruleset_revision_id, revision_number, definition_sha256, created_at
               FROM ruleset_revisions WHERE ruleset_id=? ORDER BY revision_number DESC""",
            (row["ruleset_id"],),
        ).fetchall()
        latest = revisions[0] if revisions else None
        return {
            "ruleset_id": row["ruleset_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "latest_revision_id": latest["ruleset_revision_id"] if latest else None,
            "latest_revision_number": latest["revision_number"] if latest else None,
            "revision_count": len(revisions),
            "definition": json.loads(row["definition_json"])["rules"],
        }

    def list_rulesets(self, match, query, body) -> Response:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM rulesets ORDER BY created_at").fetchall()
            items = [self._ruleset_summary(conn, row) for row in rows]
        return Response({"rulesets": items})

    def create_ruleset(self, match, query, body) -> Response:
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        canonical = canonical_ruleset(body.get("name"), body.get("rules"))
        ruleset_id = "rset_" + secrets.token_hex(8)
        rev_id = ruleset_revision_id(ruleset_id, canonical["sha256"])
        with connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO rulesets
                   (ruleset_id, name, definition_json, definition_sha256, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    ruleset_id,
                    canonical["name"],
                    canonical["definition_json"],
                    canonical["sha256"],
                    now_iso(),
                ),
            )
            conn.execute(
                """INSERT INTO ruleset_revisions
                   (ruleset_revision_id, ruleset_id, revision_number, definition_json,
                    definition_sha256, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    rev_id,
                    ruleset_id,
                    1,
                    canonical["definition_json"],
                    canonical["sha256"],
                    now_iso(),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM rulesets WHERE ruleset_id=?", (ruleset_id,)).fetchone()
            summary = self._ruleset_summary(conn, row)
        return Response(
            {
                "ruleset": summary,
                "revision": {
                    "ruleset_revision_id": rev_id,
                    "ruleset_id": ruleset_id,
                    "revision_number": 1,
                    "definition": canonical["rules"],
                    "definition_sha256": canonical["sha256"],
                    "created_at": now_iso(),
                },
            },
            status=201,
        )

    def get_ruleset(self, match, query, body) -> Response:
        ruleset_id = match.group(1)
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM rulesets WHERE ruleset_id=?", (ruleset_id,)).fetchone()
            if row is None:
                raise AppError(404, "ruleset_not_found", f"Unknown ruleset {ruleset_id}")
            summary = self._ruleset_summary(conn, row)
            revisions = conn.execute(
                """SELECT ruleset_revision_id, revision_number, definition_json,
                          definition_sha256, created_at
                   FROM ruleset_revisions WHERE ruleset_id=? ORDER BY revision_number""",
                (ruleset_id,),
            ).fetchall()
        summary["revisions"] = [
            {
                "ruleset_revision_id": rev["ruleset_revision_id"],
                "revision_number": rev["revision_number"],
                "definition_sha256": rev["definition_sha256"],
                "created_at": rev["created_at"],
                "definition": json.loads(rev["definition_json"])["rules"],
            }
            for rev in revisions
        ]
        return Response(summary)

    def revise_ruleset(self, match, query, body) -> Response:
        ruleset_id = match.group(1)
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        with connect(self.db_path) as conn:
            ruleset = conn.execute("SELECT * FROM rulesets WHERE ruleset_id=?", (ruleset_id,)).fetchone()
            if ruleset is None:
                raise AppError(404, "ruleset_not_found", f"Unknown ruleset {ruleset_id}")
        canonical = canonical_ruleset(body.get("name", ruleset["name"]), body.get("rules"))
        new_rev_id = ruleset_revision_id(ruleset_id, canonical["sha256"])
        with connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM ruleset_revisions WHERE ruleset_revision_id=?", (new_rev_id,)
            ).fetchone()
            if existing is not None:
                return Response(
                    {
                        "revision": {
                            "ruleset_revision_id": existing["ruleset_revision_id"],
                            "ruleset_id": ruleset_id,
                            "revision_number": existing["revision_number"],
                            "definition": json.loads(existing["definition_json"])["rules"],
                            "definition_sha256": existing["definition_sha256"],
                            "created_at": existing["created_at"],
                        },
                        "created": False,
                        "message": "Identical definition already exists; reusing revision",
                    }
                )
            last = conn.execute(
                "SELECT MAX(revision_number) AS n FROM ruleset_revisions WHERE ruleset_id=?",
                (ruleset_id,),
            ).fetchone()
            number = int(last["n"] or 0) + 1
            conn.execute(
                """INSERT INTO ruleset_revisions
                   (ruleset_revision_id, ruleset_id, revision_number, definition_json,
                    definition_sha256, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    new_rev_id,
                    ruleset_id,
                    number,
                    canonical["definition_json"],
                    canonical["sha256"],
                    now_iso(),
                ),
            )
            conn.commit()
        return Response(
            {
                "revision": {
                    "ruleset_revision_id": new_rev_id,
                    "ruleset_id": ruleset_id,
                    "revision_number": number,
                    "definition": canonical["rules"],
                    "definition_sha256": canonical["sha256"],
                    "created_at": now_iso(),
                },
                "created": True,
            },
            status=201,
        )

    def get_ruleset_revision(self, match, query, body) -> Response:
        revision_id = match.group(1)
        with connect(self.db_path) as conn:
            row = self._ruleset_revision(conn, revision_id)
            ruleset = conn.execute(
                "SELECT * FROM rulesets WHERE ruleset_id=?", (row["ruleset_id"],)
            ).fetchone()
            builds = conn.execute(
                "SELECT COUNT(*) AS n FROM indexes WHERE ruleset_revision_id=?", (revision_id,)
            ).fetchone()
        return Response(
            {
                "ruleset_revision_id": row["ruleset_revision_id"],
                "ruleset_id": row["ruleset_id"],
                "ruleset_name": ruleset["name"],
                "revision_number": row["revision_number"],
                "definition_sha256": row["definition_sha256"],
                "definition": json.loads(row["definition_json"])["rules"],
                "created_at": row["created_at"],
                "index_count": builds["n"],
            }
        )

    # -- builds -----------------------------------------------------------
    def start_build(self, match, query, body) -> Response:
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        source_revision_id = body.get("source_revision_id")
        ruleset_revision_id = body.get("ruleset_revision_id")
        if not isinstance(source_revision_id, str) or not source_revision_id:
            raise AppError(422, "invalid_source_revision", "source_revision_id is required")
        if not isinstance(ruleset_revision_id, str) or not ruleset_revision_id:
            raise AppError(422, "invalid_ruleset_revision", "ruleset_revision_id is required")
        expected_index = index_id(source_revision_id, ruleset_revision_id, INDEX_ENGINE_VERSION)
        with connect(self.db_path) as conn:
            self._ruleset_revision(conn, ruleset_revision_id)
            published = conn.execute("SELECT * FROM indexes WHERE index_id=?", (expected_index,)).fetchone()
            if published is not None:
                return Response(
                    {
                        "job": None,
                        "index": self._index_summary(published),
                        "created": False,
                        "message": "Index already published for this source and ruleset revision",
                    }
                )
            active = conn.execute(
                """SELECT * FROM build_jobs WHERE index_id=? AND status IN ('queued','running')
                   ORDER BY created_at DESC LIMIT 1""",
                (expected_index,),
            ).fetchone()
            if active is not None:
                return Response({"job": dict(active), "created": False, "index": None})
            job_id = "job_" + secrets.token_hex(8)
            timestamp = now_iso()
            simulate = body.get("simulate")
            if simulate not in (None, "", "interrupt"):
                raise AppError(422, "invalid_simulation", "simulate must be 'interrupt' or omitted")
            if simulate == "interrupt":
                # Deliberately model a crash after staging but before publication.
                conn.execute(
                    """INSERT INTO build_jobs
                       (job_id, source_revision_id, ruleset_revision_id, index_id, status, stage,
                        progress, message, created_at, updated_at, finished_at, attempts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        source_revision_id,
                        ruleset_revision_id,
                        expected_index,
                        "interrupted",
                        "staged",
                        0.6,
                        "Interrupted before publication: staged data was discarded and no index was published",
                        timestamp,
                        timestamp,
                        timestamp,
                        1,
                    ),
                )
                conn.commit()
                with connect(self.db_path) as conn:
                    job = conn.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
                return Response(
                    {
                        "job": dict(job),
                        "index": None,
                        "created": True,
                        "message": "Simulated interruption; nothing was published. Retry to build for real.",
                    },
                    status=202,
                )
            conn.execute(
                """INSERT INTO build_jobs
                   (job_id, source_revision_id, ruleset_revision_id, index_id, status, stage,
                    progress, message, created_at, updated_at, attempts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    source_revision_id,
                    ruleset_revision_id,
                    expected_index,
                    "queued",
                    "queued",
                    0.0,
                    "Build queued",
                    timestamp,
                    timestamp,
                    1,
                ),
            )
            conn.commit()
        thread = threading.Thread(target=self._run_build, args=(job_id,), daemon=True)
        thread.start()
        with connect(self.db_path) as conn:
            job = conn.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
        return Response({"job": dict(job), "index": None, "created": True}, status=202)

    def _update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        fields = ["updated_at=?"]
        values: List[Any] = [now_iso()]
        for name, value in (
            ("status", status),
            ("stage", stage),
            ("progress", progress),
            ("message", message),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                fields.append(f"{name}=?")
                values.append(value)
        if finished:
            fields.append("finished_at=?")
            values.append(now_iso())
        values.append(job_id)
        with connect(self.db_path) as conn:
            conn.execute(f"UPDATE build_jobs SET {', '.join(fields)} WHERE job_id=?", values)
            conn.commit()

    def _run_build(self, job_id: str) -> None:
        with connect(self.db_path) as conn:
            job = conn.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            return
        stage_dir = os.path.join(self.slice_dir, f"staging-{job_id}-{secrets.token_hex(4)}")
        try:
            self._update_job(job_id, status="running", stage="fetch_source", progress=0.05,
                             message="Fetching immutable source revision")
            raw, upstream_sha = self._fetch_source(job["source_revision_id"])
            source_sha = hashlib.sha256(raw).hexdigest()
            if upstream_sha and upstream_sha != source_sha:
                raise AppError(
                    409,
                    "source_hash_mismatch",
                    "Source bytes fetched from intake do not match the recorded hash",
                    {"recorded": upstream_sha, "fetched": source_sha},
                )
            source_text = raw.decode("utf-8")
            source_bytes = utf8_prefix(source_text)
            self._check_cancel(job_id)
            self._update_job(job_id, stage="apply_rules", progress=0.25,
                             message="Applying ruleset revision")
            with connect(self.db_path) as conn:
                revision = self._ruleset_revision(conn, job["ruleset_revision_id"])
            rules = json.loads(revision["definition_json"])["rules"]
            result = apply_rules(source_text, rules)
            self._check_cancel(job_id)
            self._update_job(job_id, stage="segment", progress=0.5,
                             message="Segmenting paragraphs and building mapping")
            expected_index = index_id(
                job["source_revision_id"], job["ruleset_revision_id"], INDEX_ENGINE_VERSION
            )
            if expected_index != job["index_id"]:
                raise AppError(500, "internal_error", "Index identity mismatch during build")
            mapping_path = os.path.join(stage_dir, "mapping.json")
            os.makedirs(stage_dir, exist_ok=True)
            mapping_payload = {
                "index_id": expected_index,
                "source_revision_id": job["source_revision_id"],
                "ruleset_revision_id": job["ruleset_revision_id"],
                "engine_version": INDEX_ENGINE_VERSION,
                "clean_sha256": result.output_sha256,
                "segments": result.segments,
            }
            with open(mapping_path, "w", encoding="utf-8") as handle:
                json.dump(mapping_payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            mapping_bytes = os.path.getsize(mapping_path)
            deleted_rows = []
            for order, (start, end) in enumerate(result.deleted_spans):
                deleted_rows.append(
                    (expected_index, order, start, end, source_bytes[start], source_bytes[end])
                )
            self._check_cancel(job_id)
            self._publish(
                job=job,
                expected_index=expected_index,
                revision=revision,
                result=result,
                source_sha=source_sha,
                source_bytes_len=len(raw),
                mapping_bytes=mapping_bytes,
                mapping_path=mapping_path,
                deleted_rows=deleted_rows,
            )
            shutil.rmtree(stage_dir, ignore_errors=True)
            self._update_job(job_id, status="succeeded", stage="published", progress=1.0,
                             message="Index published atomically", finished=True)
        except AppError as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            self._update_job(job_id, status="failed", stage="failed", progress=0.0,
                             message=exc.message, error_code=exc.code,
                             error_message=exc.message, finished=True)
        except Exception as exc:  # pragma: no cover - defensive
            shutil.rmtree(stage_dir, ignore_errors=True)
            self._update_job(job_id, status="failed", stage="failed", progress=0.0,
                             message=f"{type(exc).__name__}: {exc}",
                             error_code="internal_error",
                             error_message=f"{type(exc).__name__}: {exc}", finished=True)

    def _check_cancel(self, job_id: str) -> None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is not None and row["status"] == "cancelled":
            raise AppError(409, "build_cancelled", "Build was cancelled")

    def _publish(
        self,
        *,
        job: sqlite3.Row,
        expected_index: str,
        revision: sqlite3.Row,
        result,
        source_sha: str,
        source_bytes_len: int,
        mapping_bytes: int,
        mapping_path: str,
        deleted_rows: List[Tuple[Any, ...]],
    ) -> None:
        final_mapping = os.path.join(self.slice_dir, expected_index + ".map.json")
        tmp_mapping = final_mapping + ".tmp-" + secrets.token_hex(4)
        shutil.copyfile(mapping_path, tmp_mapping)
        with open(tmp_mapping, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_mapping, final_mapping)
        integrity = {
            "source_sha256": source_sha,
            "clean_sha256": result.output_sha256,
            "source_char_length": result.source_char_count,
            "clean_char_length": result.clean_char_count,
            "paragraph_count": len(result.paragraphs),
            "mapping_segments": len(result.segments),
            "mapping_bytes": mapping_bytes,
        }
        timestamp = now_iso()
        with connect(self.db_path) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT source_sha256 FROM indexes WHERE index_id=?", (expected_index,)
                ).fetchone()
                if existing is not None:
                    if existing["source_sha256"] != source_sha:
                        raise AppError(
                            409,
                            "index_identity_conflict",
                            "An index with this identity exists with different source content",
                        )
                    conn.rollback()
                    return
                conn.execute(
                    """INSERT INTO indexes
                       (index_id, source_revision_id, ruleset_revision_id, engine_version,
                        source_sha256, source_byte_length, source_char_length, clean_sha256,
                        clean_byte_length, clean_char_length, paragraph_count,
                        mapping_segments, mapping_bytes, deleted_ranges,
                        ruleset_definition_sha256, rule_stats_json, integrity_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        expected_index,
                        job["source_revision_id"],
                        job["ruleset_revision_id"],
                        INDEX_ENGINE_VERSION,
                        source_sha,
                        source_bytes_len,
                        result.source_char_count,
                        result.output_sha256,
                        len(result.clean_text.encode("utf-8")),
                        result.clean_char_count,
                        len(result.paragraphs),
                        len(result.segments),
                        mapping_bytes,
                        len(result.deleted_spans),
                        revision["definition_sha256"],
                        json.dumps(result.rule_stats, sort_keys=True),
                        json.dumps(integrity, sort_keys=True),
                        timestamp,
                    ),
                )
                conn.executemany(
                    """INSERT INTO paragraph_index
                       (index_id, ordinal, clean_char_start, clean_char_end,
                        clean_byte_start, clean_byte_end, source_char_start, source_char_end,
                        source_byte_start, source_byte_end, text)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            expected_index,
                            para.ordinal,
                            para.clean_char_start,
                            para.clean_char_end,
                            para.clean_byte_start,
                            para.clean_byte_end,
                            para.source_char_start,
                            para.source_char_end,
                            para.source_byte_start,
                            para.source_byte_end,
                            para.text,
                        )
                        for para in result.paragraphs
                    ],
                )
                conn.execute(
                    "INSERT INTO clean_text (index_id, text) VALUES (?,?)",
                    (expected_index, result.clean_text),
                )
                conn.executemany(
                    """INSERT INTO deleted_ranges
                       (index_id, range_order, source_char_start, source_char_end,
                        source_byte_start, source_byte_end)
                       VALUES (?,?,?,?,?,?)""",
                    deleted_rows,
                )
                conn.execute(
                    """UPDATE build_jobs SET status='published', stage='committed',
                       progress=0.95, message='Rows committed; finalising',
                       updated_at=? WHERE job_id=?""",
                    (now_iso(), job["job_id"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                try:
                    os.remove(final_mapping)
                except FileNotFoundError:
                    pass
                raise

    def list_builds(self, match, query, body) -> Response:
        limit = int(q1(query, "limit") or 50)
        limit = max(1, min(limit, 200))
        with connect(self.db_path) as conn:
            jobs = conn.execute(
                "SELECT * FROM build_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return Response({"jobs": rows_to_dicts(jobs)})

    def get_build(self, match, query, body) -> Response:
        job_id = match.group(1)
        with connect(self.db_path) as conn:
            job = conn.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise AppError(404, "build_not_found", f"Unknown build job {job_id}")
            index = conn.execute("SELECT * FROM indexes WHERE index_id=?", (job["index_id"],)).fetchone()
        payload = dict(job)
        payload["index"] = self._index_summary(index) if index else None
        return Response(payload)

    def retry_build(self, match, query, body) -> Response:
        job_id = match.group(1)
        with connect(self.db_path) as conn:
            job = conn.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise AppError(404, "build_not_found", f"Unknown build job {job_id}")
            if job["status"] in ("queued", "running"):
                raise AppError(409, "build_active", "Build is still active")
            published = conn.execute(
                "SELECT index_id FROM indexes WHERE index_id=?", (job["index_id"],)
            ).fetchone()
            if published is not None:
                return Response(
                    {
                        "job": dict(job),
                        "index": self._index_summary(published),
                        "created": False,
                        "message": "Index already published; nothing to retry",
                    }
                )
            new_job_id = "job_" + secrets.token_hex(8)
            timestamp = now_iso()
            conn.execute(
                """INSERT INTO build_jobs
                   (job_id, source_revision_id, ruleset_revision_id, index_id, status, stage,
                    progress, message, created_at, updated_at, attempts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_job_id,
                    job["source_revision_id"],
                    job["ruleset_revision_id"],
                    job["index_id"],
                    "queued",
                    "queued",
                    0.0,
                    f"Retry of {job_id}",
                    timestamp,
                    timestamp,
                    job["attempts"] + 1,
                ),
            )
            conn.commit()
        thread = threading.Thread(target=self._run_build, args=(new_job_id,), daemon=True)
        thread.start()
        with connect(self.db_path) as conn:
            new_job = conn.execute("SELECT * FROM build_jobs WHERE job_id=?", (new_job_id,)).fetchone()
        return Response({"job": dict(new_job), "index": None, "created": True}, status=202)

    # -- indexes ----------------------------------------------------------
    @staticmethod
    def _index_summary(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["rule_stats"] = json.loads(data.pop("rule_stats_json"))
        data["integrity"] = json.loads(data.pop("integrity_json"))
        return data

    def list_indexes(self, match, query, body) -> Response:
        source = q1(query, "source_revision_id")
        ruleset = q1(query, "ruleset_revision_id")
        clauses = []
        values: List[Any] = []
        if source:
            clauses.append("source_revision_id=?")
            values.append(source)
        if ruleset:
            clauses.append("ruleset_revision_id=?")
            values.append(ruleset)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM indexes" + where + " ORDER BY created_at DESC", values
            ).fetchall()
        return Response({"indexes": [self._index_summary(row) for row in rows]})

    def _load_index(self, conn: sqlite3.Connection, index_id_value: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM indexes WHERE index_id=?", (index_id_value,)).fetchone()
        if row is None:
            raise AppError(404, "index_not_found", f"Unknown index {index_id_value}")
        return row

    def get_index(self, match, query, body) -> Response:
        index_id_value = match.group(1)
        with connect(self.db_path) as conn:
            row = self._load_index(conn, index_id_value)
            deleted = conn.execute(
                """SELECT source_char_start, source_char_end, source_byte_start, source_byte_end
                   FROM deleted_ranges WHERE index_id=? ORDER BY range_order""",
                (index_id_value,),
            ).fetchall()
            builds = conn.execute(
                "SELECT COUNT(*) AS n FROM build_jobs WHERE index_id=?", (index_id_value,)
            ).fetchone()
        summary = self._index_summary(row)
        summary["deleted_ranges"] = rows_to_dicts(deleted)
        summary["build_attempts"] = builds["n"]
        return Response(summary)

    def get_paragraphs(self, match, query, body) -> Response:
        index_id_value = match.group(1)
        offset = max(0, int(q1(query, "offset") or 0))
        limit = max(1, min(int(q1(query, "limit") or 200), 1000))
        with connect(self.db_path) as conn:
            self._load_index(conn, index_id_value)
            rows = conn.execute(
                """SELECT ordinal, clean_char_start, clean_char_end, clean_byte_start,
                          clean_byte_end, source_char_start, source_char_end,
                          source_byte_start, source_byte_end, text
                   FROM paragraph_index WHERE index_id=? ORDER BY ordinal LIMIT ? OFFSET ?""",
                (index_id_value, limit, offset),
            ).fetchall()
        items = rows_to_dicts(rows)
        for item in items:
            item["char_count"] = item["clean_char_end"] - item["clean_char_start"]
        return Response({"index_id": index_id_value, "paragraphs": items, "offset": offset, "limit": limit})

    def _mapping(self, index_id_value: str) -> Dict[str, Any]:
        path = os.path.join(self.slice_dir, index_id_value + ".map.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise AppError(
                500, "mapping_missing", f"Mapping file for {index_id_value} is missing from storage"
            )

    def get_clean(self, match, query, body) -> Response:
        index_id_value = match.group(1)
        with connect(self.db_path) as conn:
            self._load_index(conn, index_id_value)
            row = conn.execute(
                "SELECT text FROM clean_text WHERE index_id=?", (index_id_value,)
            ).fetchone()
        if row is None:
            raise AppError(500, "clean_text_missing", "Clean text for this index is missing")
        payload = row["text"]
        return Response(raw=payload.encode("utf-8"), content_type="text/plain; charset=utf-8",
                        headers={"X-Index-Id": index_id_value})

    def get_map(self, match, query, body) -> Response:
        index_id_value = match.group(1)
        with connect(self.db_path) as conn:
            self._load_index(conn, index_id_value)
        mapping = self._mapping(index_id_value)
        mode = q1(query, "mode", "full")
        if mode == "summary":
            return Response(
                {
                    "index_id": index_id_value,
                    "clean_sha256": mapping["clean_sha256"],
                    "segment_count": len(mapping["segments"]),
                    "segments": mapping["segments"][:20],
                    "truncated": len(mapping["segments"]) > 20,
                }
            )
        return Response(mapping)

    def locate(self, match, query, body) -> Response:
        """Character positions in clean text -> source character AND byte spans."""
        index_id_value = match.group(1)
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        start = int_param(body.get("clean_char_start"), "clean_char_start")
        end = int_param(body.get("clean_char_end"), "clean_char_end")
        if end <= start:
            raise AppError(422, "invalid_range", "clean_char_end must be greater than clean_char_start")
        with connect(self.db_path) as conn:
            index = self._load_index(conn, index_id_value)
        clean_length = index["clean_char_length"]
        if start < 0 or end > clean_length:
            raise AppError(
                422,
                "range_out_of_bounds",
                f"Clean character range [{start}, {end}) outside 0..{clean_length}",
            )
        mapping = self._mapping(index_id_value)
        spans = map_clean_to_source(mapping["segments"], start, end)
        source_text = self._source_text(index["source_revision_id"], index["source_sha256"])
        source_bytes = utf8_prefix(source_text)
        for span in spans:
            span["source_byte_start"] = source_bytes[span["source_char_start"]]
            span["source_byte_end"] = source_bytes[span["source_char_end"]]
        return Response(
            {
                "index_id": index_id_value,
                "requested_clean_chars": {"start": start, "end": end},
                "clean_sha256": mapping["clean_sha256"],
                "source_revision_id": index["source_revision_id"],
                "source_sha256": index["source_sha256"],
                "source_spans": spans,
                "source_char_ranges": _merge_ranges(spans, "source_char_start", "source_char_end"),
                "source_byte_ranges": _merge_ranges(spans, "source_byte_start", "source_byte_end"),
                "has_synthetic": any(span["synthetic"] for span in spans),
            }
        )

    def quote(self, match, query, body) -> Response:
        """Resolve a literal quote in the index's clean text.

        Matching happens on the exact stored clean text, so context that spans
        paragraph separators behaves predictably.  Offsets are clean character
        positions; use /locate to obtain source spans.
        """
        index_id_value = match.group(1)
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        quote_text = body.get("quote")
        if not isinstance(quote_text, str) or not quote_text:
            raise AppError(422, "invalid_quote", "quote must be a non-empty string")
        if len(quote_text) > 4000:
            raise AppError(422, "invalid_quote", "quote must be at most 4000 characters")
        before = body.get("before")
        after = body.get("after")
        if before is not None and not isinstance(before, str):
            raise AppError(422, "invalid_context", "before must be a string")
        if after is not None and not isinstance(after, str):
            raise AppError(422, "invalid_context", "after must be a string")
        with connect(self.db_path) as conn:
            index = self._load_index(conn, index_id_value)
            clean_row = conn.execute(
                "SELECT text FROM clean_text WHERE index_id=?", (index_id_value,)
            ).fetchone()
            if clean_row is None:
                raise AppError(500, "clean_text_missing", "Clean text for this index is missing")
            paragraphs = conn.execute(
                "SELECT ordinal, clean_char_start, clean_char_end FROM paragraph_index "
                "WHERE index_id=? ORDER BY ordinal",
                (index_id_value,),
            ).fetchall()
            clean_text = clean_row["text"]
        occurrences = find_occurrences(clean_text, quote_text, limit=500)
        total = len(occurrences)

        # Context filtering is part of resolution, so "ambiguous" is honest.
        filtered = occurrences
        if before:
            filtered = [pair for pair in filtered if clean_text[: pair[0]].endswith(before)]
        if after:
            filtered = [pair for pair in filtered if clean_text[pair[1] :].startswith(after)]

        matches = []
        for start, end in filtered:
            ordinal = _paragraph_for(paragraphs, start, end)
            matches.append(
                {
                    "paragraph_ordinal": ordinal,
                    "clean_char_start": start,
                    "clean_char_end": end,
                    "clean_before": clean_text[max(0, start - 160) : start],
                    "clean_after": clean_text[end : end + 160],
                    "clean_remaining": clean_text[end:],
                    "source_char_start": None,
                    "source_char_end": None,
                }
            )
        return Response(
            {
                "index_id": index_id_value,
                "quote": quote_text,
                "clean_sha256": index["clean_sha256"],
                "occurrence_count": total,
                "filtered_count": len(filtered),
                "match_count": len(matches),
                "ambiguous": len(matches) > 1,
                "matches": matches,
                "context_used": {"before": bool(before), "after": bool(after)},
                "hint": (
                    "Quote occurs more than once even after context filtering; narrow it further"
                    if len(matches) > 1
                    else "Quote resolves uniquely in this index"
                    if matches
                    else "Quote not present in this index"
                    if total == 0
                    else "Quote occurs, but not with the supplied surrounding context"
                ),
            }
        )

    def storage_cost(self, match, query, body) -> Response:
        with connect(self.db_path) as conn:
            index_rows = conn.execute("SELECT * FROM indexes").fetchall()
            paragraph_rows = conn.execute(
                "SELECT index_id, COUNT(*) AS n, SUM(LENGTH(text)) AS chars FROM paragraph_index GROUP BY index_id"
            ).fetchall()
            deleted_rows = conn.execute(
                "SELECT index_id, COUNT(*) AS n FROM deleted_ranges GROUP BY index_id"
            ).fetchall()
            clean_rows = conn.execute(
                "SELECT index_id, LENGTH(text) AS chars FROM clean_text"
            ).fetchall()
            jobs = conn.execute("SELECT status, COUNT(*) AS n FROM build_jobs GROUP BY status").fetchall()
        paragraph_map = {row["index_id"]: row for row in paragraph_rows}
        deleted_map = {row["index_id"]: row["n"] for row in deleted_rows}
        clean_map = {row["index_id"]: row["chars"] for row in clean_rows}
        mapping_total = 0
        items = []
        for row in index_rows:
            path = os.path.join(self.slice_dir, row["index_id"] + ".map.json")
            size = os.path.getsize(path) if os.path.exists(path) else 0
            mapping_total += size
            para = paragraph_map.get(row["index_id"])
            items.append(
                {
                    "index_id": row["index_id"],
                    "source_revision_id": row["source_revision_id"],
                    "ruleset_revision_id": row["ruleset_revision_id"],
                    "source_byte_length": row["source_byte_length"],
                    "clean_byte_length": row["clean_byte_length"],
                    "paragraph_count": row["paragraph_count"],
                    "mapping_segments": row["mapping_segments"],
                    "mapping_file_bytes": size,
                    "mapping_bytes_per_source_byte": round(
                        size / row["source_byte_length"], 4
                    )
                    if row["source_byte_length"]
                    else 0,
                    "paragraph_text_bytes": para["chars"] if para else 0,
                    "cleaned_text_chars": clean_map.get(row["index_id"], 0),
                    "deleted_range_count": deleted_map.get(row["index_id"], 0),
                }
            )
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return Response(
            {
                "index_count": len(index_rows),
                "mapping_file_bytes": mapping_total,
                "metadata_db_bytes": db_size,
                "total_bytes": mapping_total + db_size,
                "indexes": items,
                "jobs_by_status": {row["status"]: row["n"] for row in jobs},
                "notes": [
                    "Source bytes are stored once per content hash; the mapping is new per (source, ruleset) pair.",
                    "One mapping segment is stored per contiguous run of source characters.",
                    "Synthetic segments mark inserted replacement characters and are not source text.",
                    "Mapping JSON is the dominant storage cost; paragraph text duplicates clean text.",
                ],
            }
        )


def _paragraph_for(paragraphs: List[sqlite3.Row], start: int, end: int) -> Optional[int]:
    for row in paragraphs:
        if row["clean_char_start"] <= start and end <= row["clean_char_end"]:
            return row["ordinal"]
    for row in paragraphs:
        if row["clean_char_start"] < end and start < row["clean_char_end"]:
            return row["ordinal"]
    return None


def _merge_ranges(spans: List[Dict[str, Any]], start_key: str, end_key: str) -> List[List[int]]:
    ranges: List[List[int]] = []
    for span in spans:
        start = span.get(start_key)
        end = span.get(end_key)
        if start is None or end is None:
            continue
        if ranges and ranges[-1][1] >= start:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    return ranges


def create_service(
    db_path: Optional[str] = None,
    intake_url: Optional[str] = None,
    slice_dir: Optional[str] = None,
) -> IndexerService:
    db_path = db_path or os.environ.get("INDEXER_DB", "data/indexer/indexer.sqlite3")
    slice_dir = slice_dir or os.environ.get("INDEXER_MAPPINGS", "data/indexer/mappings")
    intake_url = intake_url or os.environ.get("INTAKE_URL", "http://127.0.0.1:8101")
    return IndexerService(db_path, intake_url, slice_dir)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Cleaning indexer service")
    parser.add_argument("--host", default=os.environ.get("INDEXER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INDEXER_PORT", "8102")))
    args = parser.parse_args(argv)
    service = create_service()
    server = serve(service, args.host, args.port)
    print(f"indexer listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
