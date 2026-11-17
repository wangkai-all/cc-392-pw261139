"""Citation-query service.

Owns analysis reports and their citations.  Every citation is *pinned* to the
exact source revision and index it was created against, and stores both the
character span and byte span it resolved to.  Reading a report is cheap and
never silently re-anchors: it reports the pinned positions.  Comparing against
another index is an explicit operation that re-locates the literal quote and
classifies the outcome (same text, moved, ambiguous, or gone).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

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
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _citation_id() -> str:
    return "cit_" + secrets.token_hex(8)


def _report_id() -> str:
    return "rpt_" + secrets.token_hex(8)


class QueryService(Service):
    name = "query"

    def __init__(self, db_path: str, indexer_url: str, intake_url: str, static_dir: Optional[str] = None):
        super().__init__()
        self.db_path = db_path
        self.indexer_url = indexer_url.rstrip("/")
        self.intake_url = intake_url.rstrip("/")
        self.static_dir = static_dir or STATIC_DIR
        ensure_parent(db_path)
        self._init_db()
        self._register_routes()

    # -- schema -----------------------------------------------------------
    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_revision_id TEXT NOT NULL,
                    index_id TEXT NOT NULL,
                    ruleset_revision_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    clean_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS citations (
                    citation_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL REFERENCES reports(report_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    quote TEXT NOT NULL,
                    quote_sha256 TEXT NOT NULL,
                    before_text TEXT NOT NULL DEFAULT '',
                    after_text TEXT NOT NULL DEFAULT '',
                    paragraph_ordinal INTEGER,
                    clean_char_start INTEGER,
                    clean_char_end INTEGER,
                    source_char_start INTEGER,
                    source_char_end INTEGER,
                    source_byte_start INTEGER,
                    source_byte_end INTEGER,
                    source_spans_json TEXT NOT NULL DEFAULT '[]',
                    source_sha256 TEXT NOT NULL,
                    index_id TEXT NOT NULL,
                    source_revision_id TEXT NOT NULL,
                    ruleset_revision_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS citations_report ON citations(report_id, ordinal);
                """
            )
            self._migrate(conn)
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive migrations keep existing citation databases readable."""
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(citations)").fetchall()
        }
        if "source_spans_json" not in columns:
            conn.execute(
                "ALTER TABLE citations ADD COLUMN source_spans_json TEXT NOT NULL DEFAULT '[]'"
            )

    # -- upstream ---------------------------------------------------------
    def _up(self, path: str, method: str = "GET", payload: Any = None, base: Optional[str] = None) -> Any:
        url = (base or self.indexer_url) + path
        try:
            return remote_json(url, method=method, payload=payload)
        except RemoteError as exc:
            code = exc.code or ("not_found" if exc.status == 404 else "upstream_error")
            status = exc.status or 502
            raise AppError(status, code, f"Upstream {url}: {exc}")

    def _index(self, index_id: str) -> Dict[str, Any]:
        return self._up(f"/api/indexes/{index_id}")

    def _latest_index_for_source(self, source_revision_id: str) -> Optional[Dict[str, Any]]:
        data = self._up(f"/api/indexes?source_revision_id={source_revision_id}")
        indexes = data.get("indexes") or []
        if not indexes:
            return None
        indexes.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        return indexes[0]

    def _indexes_for_source(self, source_revision_id: str) -> List[Dict[str, Any]]:
        data = self._up(f"/api/indexes?source_revision_id={source_revision_id}")
        return data.get("indexes") or []

    # -- routes -----------------------------------------------------------
    def _register_routes(self) -> None:
        self.route("GET", r"/api/health", self.health)
        # Facade: the browser talks to this service only.
        self.route("GET", r"/api/documents", self.proxy_intake_get)
        self.route("POST", r"/api/documents", self.proxy_intake_post)
        self.route("GET", r"/api/documents/([^/]+)", self.proxy_intake_get)
        self.route("POST", r"/api/documents/([^/]+)/revisions", self.proxy_intake_post)
        self.route("GET", r"/api/revisions/([^/]+)", self.proxy_intake_get)
        self.route("GET", r"/api/revisions/([^/]+)/source", self.proxy_source)
        self.route("POST", r"/api/revisions/([^/]+)/slice", self.proxy_intake_post)
        self.route("POST", r"/api/revisions/([^/]+)/search", self.proxy_intake_post)
        self.route("GET", r"/api/rulesets", self.proxy_indexer_get)
        self.route("POST", r"/api/rulesets", self.proxy_indexer_post)
        self.route("GET", r"/api/rulesets/([^/]+)", self.proxy_indexer_get)
        self.route("POST", r"/api/rulesets/([^/]+)/revisions", self.proxy_indexer_post)
        self.route("GET", r"/api/ruleset-revisions/([^/]+)", self.proxy_indexer_get)
        self.route("GET", r"/api/indexes", self.proxy_indexer_get)
        self.route("GET", r"/api/indexes/([^/]+)", self.proxy_indexer_get)
        self.route("GET", r"/api/indexes/([^/]+)/paragraphs", self.proxy_indexer_get)
        self.route("GET", r"/api/builds", self.proxy_indexer_get)
        self.route("POST", r"/api/builds", self.proxy_indexer_post)
        self.route("GET", r"/api/builds/([^/]+)", self.proxy_indexer_get)
        self.route("POST", r"/api/builds/([^/]+)/retry", self.proxy_indexer_post)
        self.route("GET", r"/api/storage-cost", self.storage_cost)
        self.route("GET", r"/api/reports", self.list_reports)
        self.route("POST", r"/api/reports", self.create_report)
        self.route("GET", r"/api/reports/([^/]+)", self.get_report)
        self.route("GET", r"/api/citations/([^/]+)", self.get_citation)
        self.route("GET", r"/api/citations/([^/]+)/verify", self.verify_citation)
        self.route("GET", r"/api/citations/([^/]+)/context", self.citation_context)
        self.route("POST", r"/api/citations/([^/]+)/compare", self.compare_citation)
        self.route("POST", r"/api/citations/([^/]+)/reanchor", self.reanchor_citation)
        self.route("GET", r"/index.html", self.index_html)
        self.route("GET", r"/", self.index_html)
        self.route("GET", r"/(app\.js)", self.static_file)
        self.route("GET", r"/(styles\.css)", self.static_file)

    def health(self, match, query, body) -> Response:
        return Response({"service": self.name, "ok": True, "time": now_iso()})

    # -- facade proxying ---------------------------------------------------
    def _proxy(self, base: str, path: str, method: str, query: Dict[str, List[str]], body: Any) -> Response:
        params = []
        for key, values in query.items():
            for value in values:
                params.append(f"{urllib.parse.quote(key)}={urllib.parse.quote(value)}")
        url = path + ("?" + "&".join(params) if params else "")
        try:
            payload = remote_json(base + url, method=method, payload=body if method == "POST" else None)
        except RemoteError as exc:
            raise AppError(exc.status or 502, exc.code or "upstream_error", str(exc))
        return Response(payload)

    def proxy_intake_get(self, match, query, body) -> Response:
        return self._proxy(self.intake_url, match.string, "GET", query, None)

    def proxy_intake_post(self, match, query, body) -> Response:
        return self._proxy(self.intake_url, match.string, "POST", query, body)

    def proxy_indexer_get(self, match, query, body) -> Response:
        return self._proxy(self.indexer_url, match.string, "GET", query, None)

    def proxy_indexer_post(self, match, query, body) -> Response:
        return self._proxy(self.indexer_url, match.string, "POST", query, body)

    def proxy_source(self, match, query, body) -> Response:
        try:
            raw, _headers = remote_bytes(self.intake_url + match.string)
        except RemoteError as exc:
            raise AppError(exc.status or 502, exc.code or "upstream_error", str(exc))
        return Response(raw=raw, content_type="text/plain; charset=utf-8")

    def storage_cost(self, match, query, body) -> Response:
        intake = self._up("/api/storage", base=self.intake_url)
        indexer = self._up("/api/storage", base=self.indexer_url)
        with connect(self.db_path) as conn:
            reports = conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
            citations = conn.execute("SELECT COUNT(*) AS n FROM citations").fetchone()["n"]
        db_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        total = intake["total_bytes"] + indexer["total_bytes"] + db_bytes
        return Response(
            {
                "intake": intake,
                "indexer": indexer,
                "reports": {
                    "db_bytes": db_bytes,
                    "report_count": reports,
                    "citation_count": citations,
                    "bytes_per_citation": round(db_bytes / citations, 1) if citations else 0,
                },
                "total_bytes": total,
                "read_cost": {
                    "read_report": "1 indexed SQLite read for the report + 1 for its citations; no re-location",
                    "read_citation": "1 indexed SQLite read; offsets are returned as stored",
                    "verify_citation": (
                        "1 revision metadata read + 1 source window slice + 1 char-range slice "
                        "per copied span in the quote"
                    ),
                    "compare_citation": "1 literal scan of the target index's stored clean text",
                    "reanchor": "scan + locate + insert a new citation row; the original row is retained",
                },
                "tradeoff": {
                    "mapping_strategy": (
                        "Keep every source revision and store a compact char-span mapping per index: "
                        "any index can be projected back to its own source bytes, and a citation can "
                        "always be verified against the revision it was pinned to. Cost is one mapping "
                        "file per (source revision, ruleset revision) pair; it is never shared."
                    ),
                    "pinning_strategy": (
                        "Pinning the revision id + sha256 makes citation reads O(1) and independent of "
                        "later rule edits or re-imports. The cost is that an old citation is not "
                        "automatically valid in a newer index; comparison and re-anchoring are explicit "
                        "operations that pay for a scan and create a new row."
                    ),
                    "why_both": (
                        "Mapping answers 'where did this text come from'; pinning answers 'what does "
                        "this reference mean'. Storing only the mapping lets a rebuild silently change "
                        "citation text; storing only the pin loses byte-level provenance."
                    ),
                },
            }
        )

    # -- static UI --------------------------------------------------------
    def index_html(self, match, query, body) -> Response:
        return self._serve_static("index.html", "text/html; charset=utf-8")

    def static_file(self, match, query, body) -> Response:
        name = match.group(1)
        content_type = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
        return self._serve_static(name, content_type)

    def _serve_static(self, name: str, content_type: str) -> Response:
        path = os.path.join(self.static_dir, name)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            raise AppError(404, "not_found", f"UI asset {name} is missing")
        return Response(raw=raw, content_type=content_type)

    # -- report creation --------------------------------------------------
    def create_report(self, match, query, body) -> Response:
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        title = body.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise AppError(422, "invalid_title", "title must be 1-200 characters")
        index_id = body.get("index_id")
        if not isinstance(index_id, str) or not index_id:
            raise AppError(422, "invalid_index", "index_id is required")
        annotations = body.get("annotations")
        if not isinstance(annotations, list) or not annotations:
            raise AppError(422, "invalid_annotations", "annotations must be a non-empty array")
        if len(annotations) > 200:
            raise AppError(422, "invalid_annotations", "at most 200 annotations per report")
        index = self._index(index_id)
        report_row = {
            "report_id": _report_id(),
            "title": title.strip(),
            "source_revision_id": index["source_revision_id"],
            "index_id": index["index_id"],
            "ruleset_revision_id": index["ruleset_revision_id"],
            "source_sha256": index["source_sha256"],
            "clean_sha256": index["clean_sha256"],
            "created_at": now_iso(),
        }
        resolved: List[Dict[str, Any]] = []
        for ordinal, annotation in enumerate(annotations):
            if not isinstance(annotation, dict):
                raise AppError(422, "invalid_annotations", f"annotation {ordinal} must be an object")
            quote = annotation.get("quote")
            if not isinstance(quote, str) or not quote:
                raise AppError(422, "invalid_annotations", f"annotation {ordinal} needs a quote")
            note = annotation.get("note", "")
            if not isinstance(note, str) or len(note) > 2000:
                raise AppError(422, "invalid_annotations", f"annotation {ordinal} note is invalid")
            payload: Dict[str, Any] = {"quote": quote}
            for key in ("before", "after"):
                if isinstance(annotation.get(key), str):
                    payload[key] = annotation[key]
            resolution = self._up(f"/api/indexes/{index_id}/quote", "POST", payload)
            matches = resolution.get("matches") or []
            chosen, status = self._choose_match(matches, annotation)
            citation = {
                "citation_id": _citation_id(),
                "report_id": report_row["report_id"],
                "ordinal": ordinal,
                "note": note,
                "quote": quote,
                "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                "before_text": annotation.get("before", "") if isinstance(annotation.get("before"), str) else "",
                "after_text": annotation.get("after", "") if isinstance(annotation.get("after"), str) else "",
                "paragraph_ordinal": chosen["paragraph_ordinal"] if chosen else None,
                "clean_char_start": chosen["clean_char_start"] if chosen else None,
                "clean_char_end": chosen["clean_char_end"] if chosen else None,
                "source_char_start": chosen.get("source_char_start") if chosen else None,
                "source_char_end": chosen.get("source_char_end") if chosen else None,
                "source_byte_start": None,
                "source_byte_end": None,
                "source_spans": [],
                "source_sha256": report_row["source_sha256"],
                "index_id": index_id,
                "source_revision_id": report_row["source_revision_id"],
                "ruleset_revision_id": report_row["ruleset_revision_id"],
                "status": status,
                "created_at": now_iso(),
            }
            if chosen is not None:
                located = self._up(
                    f"/api/indexes/{index_id}/locate",
                    "POST",
                    {
                        "clean_char_start": chosen["clean_char_start"],
                        "clean_char_end": chosen["clean_char_end"],
                    },
                )
                spans = located.get("source_spans") or []
                citation["source_spans"] = spans
                if len(spans) == 1:
                    citation["source_char_start"] = spans[0]["source_char_start"]
                    citation["source_char_end"] = spans[0]["source_char_end"]
                    citation["source_byte_start"] = spans[0]["source_byte_start"]
                    citation["source_byte_end"] = spans[0]["source_byte_end"]
                elif spans:
                    citation["source_char_start"] = spans[0]["source_char_start"]
                    citation["source_char_end"] = spans[-1]["source_char_end"]
                    citation["source_byte_start"] = spans[0]["source_byte_start"]
                    citation["source_byte_end"] = spans[-1]["source_byte_end"]
                if citation["source_char_start"] is not None:
                    self._verify_quote_at_pin(citation)
            resolved.append(citation)
        with connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO reports
                   (report_id, title, source_revision_id, index_id, ruleset_revision_id,
                    source_sha256, clean_sha256, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    report_row["report_id"],
                    report_row["title"],
                    report_row["source_revision_id"],
                    report_row["index_id"],
                    report_row["ruleset_revision_id"],
                    report_row["source_sha256"],
                    report_row["clean_sha256"],
                    report_row["created_at"],
                ),
            )
            conn.executemany(
                """INSERT INTO citations
                   (citation_id, report_id, ordinal, note, quote, quote_sha256,
                    before_text, after_text, paragraph_ordinal, clean_char_start,
                    clean_char_end, source_char_start, source_char_end, source_byte_start,
                    source_byte_end, source_spans_json, source_sha256, index_id,
                    source_revision_id, ruleset_revision_id, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        c["citation_id"], c["report_id"], c["ordinal"], c["note"], c["quote"],
                        c["quote_sha256"], c["before_text"], c["after_text"],
                        c["paragraph_ordinal"], c["clean_char_start"], c["clean_char_end"],
                        c["source_char_start"], c["source_char_end"], c["source_byte_start"],
                        c["source_byte_end"], json.dumps(c["source_spans"], sort_keys=True),
                        c["source_sha256"], c["index_id"],
                        c["source_revision_id"], c["ruleset_revision_id"], c["status"],
                        c["created_at"],
                    )
                    for c in resolved
                ],
            )
            conn.commit()
        report = self._report_payload(report_row["report_id"])
        return Response(report, status=201)

    @staticmethod
    def _choose_match(matches: List[Dict[str, Any]], annotation: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        if not matches:
            return None, "unresolved"
        if len(matches) == 1:
            return matches[0], "resolved"
        before = annotation.get("before")
        after = annotation.get("after")
        filtered = matches
        if isinstance(before, str) and before:
            filtered = [m for m in filtered if m["clean_before"].endswith(before)]
        if isinstance(after, str) and after:
            filtered = [m for m in filtered if m["clean_remaining"].startswith(after)]
        if len(filtered) == 1:
            return filtered[0], "resolved"
        return None, "ambiguous"

    def _spans_of(self, citation: Dict[str, Any]) -> List[Dict[str, Any]]:
        if citation.get("source_spans"):
            return citation["source_spans"]
        if citation.get("source_spans_json"):
            try:
                return json.loads(citation["source_spans_json"])
            except json.JSONDecodeError:
                return []
        return []

    def _reconstruct_from_spans(self, citation: Dict[str, Any]) -> Optional[str]:
        """Rebuild the expected source-side text from recorded spans.

        Every mapped span must exactly reproduce its slice of the source; runs
        of synthetic clean characters (inserted by cleaning, e.g. a collapsed
        space) must reproduce the quote's corresponding characters.
        """
        spans = self._spans_of(citation)
        if not spans:
            # Citations written before span capture existed: fall back to the
            # single recorded source span.  This can only reproduce quotes with
            # no synthetic characters, and the caller reports that honestly.
            if citation.get("source_char_start") is None:
                return None
            return self._slice_exact(
                citation["source_revision_id"],
                citation["source_char_start"],
                citation["source_char_end"],
            )
        quote = citation["quote"]
        clean_cursor = citation["clean_char_start"]
        parts: List[str] = []
        for span in spans:
            clean_start = span["clean_char_start"]
            clean_end = span["clean_char_end"]
            if clean_start != clean_cursor:
                return None
            width = clean_end - clean_start
            if span["synthetic"]:
                parts.append(quote[clean_start - citation["clean_char_start"]:][:width])
            else:
                text = self._slice_exact(
                    citation["source_revision_id"], span["source_char_start"], span["source_char_end"]
                )
                if text is None or len(text) != width:
                    return None
                parts.append(text)
            clean_cursor = clean_end
        return "".join(parts)

    def _verify_quote_at_pin(self, citation: Dict[str, Any]) -> None:
        """Confirm the pinned spans still reproduce the quoted text exactly."""
        try:
            reconstructed = self._reconstruct_from_spans(citation)
        except AppError:
            citation["status"] = "source_unavailable"
            return
        if reconstructed is None:
            citation["status"] = "source_mismatch"
            return
        if reconstructed == citation["quote"]:
            citation["status"] = "resolved"
        else:
            window = self._source_window(
                citation["source_revision_id"],
                citation["source_char_start"],
                citation["source_char_end"],
            )
            citation["status"] = (
                "resolved_span_synthetic" if window is not None and citation["quote"] in window
                else "source_mismatch"
            )

    def _source_window(self, source_revision_id: str, start: int, end: int, pad: int = 32) -> Optional[str]:
        low = max(0, start - pad)
        high = end + pad
        for attempt in (high, end):
            if attempt < low:
                return None
            try:
                data = self._up(
                    f"/api/revisions/{source_revision_id}/slice",
                    "POST",
                    {"unit": "char", "start": low, "end": attempt},
                    base=self.intake_url,
                )
                return data.get("text")
            except AppError as exc:
                if exc.code == "range_out_of_bounds":
                    continue
                return None
        return None

    # -- report reads -----------------------------------------------------
    def _report_row(self, conn: sqlite3.Connection, report_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            raise AppError(404, "report_not_found", f"Unknown report {report_id}")
        return row

    def _citation_row(self, conn: sqlite3.Connection, citation_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM citations WHERE citation_id=?", (citation_id,)).fetchone()
        if row is None:
            raise AppError(404, "citation_not_found", f"Unknown citation {citation_id}")
        return row

    def _report_payload(self, report_id: str) -> Dict[str, Any]:
        with connect(self.db_path) as conn:
            report = self._report_row(conn, report_id)
            citations = conn.execute(
                "SELECT * FROM citations WHERE report_id=? ORDER BY ordinal", (report_id,)
            ).fetchall()
        citation_items = rows_to_dicts(citations)
        for item in citation_items:
            item["source_spans"] = self._spans_of(item)
        return {
            "report": dict(report),
            "citations": citation_items,
            "positions_semantics": {
                "clean_char_*": "character (code point) offsets in cleaned text for the pinned index",
                "source_char_*": "character offsets in the immutable source revision",
                "source_byte_*": "UTF-8 byte offsets in the immutable source revision",
            },
        }

    def list_reports(self, match, query, body) -> Response:
        with connect(self.db_path) as conn:
            reports = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
            counts = conn.execute(
                "SELECT report_id, COUNT(*) AS n FROM citations GROUP BY report_id"
            ).fetchall()
        count_map = {row["report_id"]: row["n"] for row in counts}
        items = []
        for row in reports:
            item = dict(row)
            item["citation_count"] = count_map.get(row["report_id"], 0)
            items.append(item)
        return Response({"reports": items})

    def get_report(self, match, query, body) -> Response:
        return Response(self._report_payload(match.group(1)))

    def get_citation(self, match, query, body) -> Response:
        with connect(self.db_path) as conn:
            citation = dict(self._citation_row(conn, match.group(1)))
        citation["source_spans"] = self._spans_of(citation)
        return Response(citation)

    def verify_citation(self, match, query, body) -> Response:
        """Verify against the pinned source revision, independent of any index."""
        citation_id = match.group(1)
        with connect(self.db_path) as conn:
            citation = dict(self._citation_row(conn, citation_id))
        if citation["source_char_start"] is None:
            return Response(
                {
                    "citation": citation,
                    "status": "unresolved",
                    "message": "Citation never resolved to a source span",
                }
            )
        try:
            revision = self._up(
                f"/api/revisions/{citation['source_revision_id']}", base=self.intake_url
            )
        except AppError as exc:
            return Response(
                {
                    "citation": citation,
                    "verified": False,
                    "status": "source_unavailable" if exc.code == "upstream_error" else "source_revision_missing",
                    "message": exc.message,
                    "server_sha256": None,
                    "pinned_sha256": citation["source_sha256"],
                    "checks": {"source_hash_unchanged": False, "quote_text_unchanged_at_span": False},
                },
                status=409 if exc.code != "upstream_error" else 502,
            )
        server_sha = revision.get("sha256")
        hash_matches = server_sha == citation["source_sha256"]
        if not hash_matches:
            return Response(
                {
                    "citation": citation,
                    "verified": False,
                    "status": "pin_mismatch",
                    "message": (
                        "Source revision metadata no longer matches the hash this citation was "
                        "pinned to; the citation must not be treated as pointing at the quote"
                    ),
                    "pinned_sha256": citation["source_sha256"],
                    "server_sha256": server_sha,
                    "checks": {"source_hash_unchanged": False, "quote_text_unchanged_at_span": False},
                },
                status=409,
            )
        window = self._source_window(
            citation["source_revision_id"],
            citation["source_char_start"],
            citation["source_char_end"],
        )
        if window is None:
            return Response(
                {
                    "citation": citation,
                    "verified": False,
                    "status": "pin_mismatch",
                    "message": (
                        "Stored source bytes for this revision no longer match their content hash; "
                        "the citation cannot be trusted to point at the quoted text"
                    ),
                    "pinned_sha256": citation["source_sha256"],
                    "server_sha256": server_sha,
                    "checks": {"source_hash_unchanged": hash_matches, "quote_text_unchanged_at_span": False},
                },
                status=409,
            )
        try:
            quote_at_span = self._reconstruct_from_spans(citation)
        except AppError:
            quote_at_span = None
        text_matches = quote_at_span == citation["quote"]
        status = "verified" if (hash_matches and text_matches) else "pin_mismatch"
        return Response(
            {
                "citation": citation,
                "verified": bool(hash_matches and text_matches),
                "status": status,
                "pinned_sha256": citation["source_sha256"],
                "server_sha256": server_sha,
                "pinned_quote": citation["quote"],
                "source_at_pinned_span": quote_at_span,
                "source_window": window,
                "checks": {
                    "source_hash_unchanged": hash_matches,
                    "quote_text_unchanged_at_span": text_matches,
                },
                "message": (
                    "Pinned source bytes still contain this exact quote at the recorded offsets"
                    if status == "verified"
                    else "Pinned source no longer matches; do not treat this citation as pointing at the quote"
                ),
            },
            status=200 if status == "verified" else 409,
        )

    def _slice_exact(self, revision_id: str, start: int, end: int) -> Optional[str]:
        try:
            data = self._up(
                f"/api/revisions/{revision_id}/slice",
                "POST",
                {"unit": "char", "start": start, "end": end},
                base=self.intake_url,
            )
        except AppError:
            return None
        return data.get("text")

    def citation_context(self, match, query, body) -> Response:
        """Context read from the pinned source revision, by character offsets."""
        citation_id = match.group(1)
        pad = max(0, min(int(q1(query, "pad") or 80), 2000))
        with connect(self.db_path) as conn:
            citation = dict(self._citation_row(conn, citation_id))
        if citation["source_char_start"] is None:
            raise AppError(409, "citation_unresolved", "Citation has no pinned source span")
        start = max(0, citation["source_char_start"] - pad)
        end = citation["source_char_end"] + pad
        # Clamp to the revision length: a citation near the end of the document
        # must still show context rather than failing an out-of-bounds read.
        revision = self._up(
            f"/api/revisions/{citation['source_revision_id']}", base=self.intake_url
        )
        end = min(end, revision["char_length"])
        data = self._up(
            f"/api/revisions/{citation['source_revision_id']}/slice",
            "POST",
            {"unit": "char", "start": start, "end": end},
            base=self.intake_url,
        )
        return Response(
            {
                "citation_id": citation_id,
                "source_revision_id": citation["source_revision_id"],
                "pinned_span": {
                    "source_char_start": citation["source_char_start"],
                    "source_char_end": citation["source_char_end"],
                    "source_byte_start": citation["source_byte_start"],
                    "source_byte_end": citation["source_byte_end"],
                },
                "slice": data,
                "padding_chars": pad,
            }
        )

    # -- comparison against another index --------------------------------
    def _default_comparison_index(self, citation: sqlite3.Row) -> Optional[Dict[str, Any]]:
        indexes = self._indexes_for_source(citation["source_revision_id"])
        if not indexes:
            return None
        candidates = [row for row in indexes if row["index_id"] != citation["index_id"]]
        if not candidates:
            return None
        candidates.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        return candidates[0]

    def compare_citation(self, match, query, body) -> Response:
        """Explicit (and therefore explicit-cost) comparison against an index."""
        citation_id = match.group(1)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        with connect(self.db_path) as conn:
            citation = dict(self._citation_row(conn, citation_id))
        target_id = body.get("index_id")
        target: Optional[Dict[str, Any]] = None
        if target_id:
            if not isinstance(target_id, str):
                raise AppError(422, "invalid_index", "index_id must be a string")
            target = self._index(target_id)
            if target["source_revision_id"] != citation["source_revision_id"]:
                return Response(
                    {
                        "citation_id": citation_id,
                        "status": "revision_differs",
                        "message": (
                            "Compared index was built from a different source revision; "
                            "positions are not comparable"
                        ),
                        "citation_source_revision_id": citation["source_revision_id"],
                        "target_source_revision_id": target["source_revision_id"],
                    },
                    status=409,
                )
        else:
            target = self._default_comparison_index(citation)
            if target is None:
                return Response(
                    {
                        "citation_id": citation_id,
                        "status": "no_comparison_index",
                        "message": "No other index exists for this source revision",
                    }
                )
        resolution = self._up(
            f"/api/indexes/{target['index_id']}/quote",
            "POST",
            {"quote": citation["quote"]},
        )
        matches = resolution.get("matches") or []
        pinned_text = citation["quote"]
        pinned_index: Optional[Dict[str, Any]] = None
        try:
            pinned_index = self._index(citation["index_id"])
        except AppError:
            pinned_index = None
        outcome = self._classify(citation, target, matches, pinned_index)
        return Response(
            {
                "citation_id": citation_id,
                "pinned_index_id": citation["index_id"],
                "target_index_id": target["index_id"],
                "target_ruleset_revision_id": target["ruleset_revision_id"],
                "target_clean_sha256": target["clean_sha256"],
                "quote": pinned_text,
                "match_count": len(matches),
                "matches": matches,
                "status": outcome["status"],
                "message": outcome["message"],
                "compare_cost": {
                    "note": "Comparison scans the target index's stored clean text for the literal quote",
                    "clean_chars_scanned": target["clean_char_length"],
                    "paragraphs_scanned": target["paragraph_count"],
                },
            }
        )

    def _classify(
        self,
        citation: Dict[str, Any],
        target: Dict[str, Any],
        matches: List[Dict[str, Any]],
        pinned_index: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        if not matches:
            return {
                "status": "quote_missing",
                "message": "Quote does not occur in the compared index; the reference is stale there",
            }
        at_same_offsets = any(
            match["clean_char_start"] == citation["clean_char_start"]
            and match["clean_char_end"] == citation["clean_char_end"]
            for match in matches
        )
        if at_same_offsets:
            # "Unchanged" means the quote occupies identical clean offsets in
            # both indexes.  Identical clean text is required for that claim,
            # and a distinct ruleset with the same rules still qualifies.
            same_clean = bool(
                pinned_index is not None
                and target["clean_sha256"] == pinned_index.get("clean_sha256")
            )
            if same_clean:
                return {
                    "status": "unchanged",
                    "message": "Identical cleaned text and identical clean character offsets",
                }
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "message": "Quote occurs multiple times in the compared index; cannot re-anchor safely",
            }
        if at_same_offsets:
            return {
                "status": "same_offsets_different_clean",
                "message": (
                    "Quote sits at the same clean character offsets, but the cleaned text around it "
                    "differs; treat the agreement as coincidental, not as confirmation"
                ),
            }
        return {
            "status": "moved",
            "message": "Quote is still present, but at different offsets under the compared ruleset",
        }

    def reanchor_citation(self, match, query, body) -> Response:
        """Create a NEW citation pinned to the target index, keeping the old one.

        This is the only operation that changes what a reference points at, and
        it refuses when the quote is missing or ambiguous.
        """
        citation_id = match.group(1)
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        target_index_id = body.get("index_id")
        if not isinstance(target_index_id, str) or not target_index_id:
            raise AppError(422, "invalid_index", "index_id is required")
        with connect(self.db_path) as conn:
            original = dict(self._citation_row(conn, citation_id))
            report = dict(self._report_row(conn, original["report_id"]))
        target = self._index(target_index_id)
        if target["source_revision_id"] != original["source_revision_id"]:
            raise AppError(
                409,
                "revision_differs",
                "Cannot re-anchor to an index built from a different source revision",
            )
        resolution = self._up(
            f"/api/indexes/{target_index_id}/quote", "POST", {"quote": original["quote"]}
        )
        matches = resolution.get("matches") or []
        chosen, status = self._choose_match(
            matches, {"before": original["before_text"], "after": original["after_text"]}
        )
        if chosen is None:
            raise AppError(
                409,
                "cannot_reanchor",
                "Quote is ambiguous or absent in the target index; refusing to silently repoint",
                {"match_count": len(matches), "status": status},
            )
        located = self._up(
            f"/api/indexes/{target_index_id}/locate",
            "POST",
            {
                "clean_char_start": chosen["clean_char_start"],
                "clean_char_end": chosen["clean_char_end"],
            },
        )
        spans = located.get("source_spans") or []
        new_citation_id = _citation_id()
        with connect(self.db_path) as conn:
            last = conn.execute(
                "SELECT MAX(ordinal) AS n FROM citations WHERE report_id=?", (report["report_id"],)
            ).fetchone()
            ordinal = int(last["n"] or 0) + 1
            conn.execute(
                """INSERT INTO citations
                   (citation_id, report_id, ordinal, note, quote, quote_sha256,
                    before_text, after_text, paragraph_ordinal, clean_char_start,
                    clean_char_end, source_char_start, source_char_end, source_byte_start,
                    source_byte_end, source_spans_json, source_sha256, index_id,
                    source_revision_id, ruleset_revision_id, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_citation_id,
                    report["report_id"],
                    ordinal,
                    (original["note"] + " (re-anchored)").strip(),
                    original["quote"],
                    original["quote_sha256"],
                    original["before_text"],
                    original["after_text"],
                    chosen["paragraph_ordinal"],
                    chosen["clean_char_start"],
                    chosen["clean_char_end"],
                    spans[0]["source_char_start"] if spans else None,
                    spans[-1]["source_char_end"] if spans else None,
                    spans[0]["source_byte_start"] if spans else None,
                    spans[-1]["source_byte_end"] if spans else None,
                    json.dumps(spans, sort_keys=True),
                    target["source_sha256"],
                    target_index_id,
                    target["source_revision_id"],
                    target["ruleset_revision_id"],
                    "resolved",
                    now_iso(),
                ),
            )
            conn.commit()
        with connect(self.db_path) as conn:
            created = dict(self._citation_row(conn, new_citation_id))
        return Response(
            {
                "replaced_citation_id": citation_id,
                "citation": created,
                "message": "Created a new citation pinned to the target index; original kept for audit",
            },
            status=201,
        )


def create_service(
    db_path: Optional[str] = None,
    indexer_url: Optional[str] = None,
    intake_url: Optional[str] = None,
) -> QueryService:
    db_path = db_path or os.environ.get("QUERY_DB", "data/query/query.sqlite3")
    indexer_url = indexer_url or os.environ.get("INDEXER_URL", "http://127.0.0.1:8102")
    intake_url = intake_url or os.environ.get("INTAKE_URL", "http://127.0.0.1:8101")
    return QueryService(db_path, indexer_url, intake_url)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Citation-query service and web UI")
    parser.add_argument("--host", default=os.environ.get("QUERY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QUERY_PORT", "8100")))
    args = parser.parse_args(argv)
    service = create_service()
    server = serve(service, args.host, args.port)
    print(f"query UI listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
