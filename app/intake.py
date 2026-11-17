"""Source-intake service.

Owns immutable source revisions.  Bytes are content-addressed and written
once; SQLite holds only metadata.  Every revision exposes both UTF-8 byte
offsets and Unicode code-point (character) offsets, never conflating them.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .common import (
    AppError,
    Response,
    Service,
    connect,
    decode_utf8,
    ensure_parent,
    int_param,
    line_ending_stats,
    now_iso,
    q1,
    row_to_dict,
    rows_to_dicts,
    serve,
    utf8_prefix,
)

MAX_SOURCE_BYTES = 8 * 1024 * 1024
_FILENAME_RE = re.compile(r"[^\x00-\x1f/\\]{1,200}")


def _revision_id(sha: str) -> str:
    return "src_" + sha[:20]


def _doc_id() -> str:
    return "doc_" + secrets.token_hex(8)


def _source_metadata(raw: bytes) -> Dict[str, Any]:
    sha = hashlib.sha256(raw).hexdigest()
    text = decode_utf8(raw, "source")
    multibyte = sum(1 for ch in text if ord(ch) > 0x7F)
    astral = sum(1 for ch in text if ord(ch) > 0xFFFF)
    return {
        "revision_id": _revision_id(sha),
        "sha256": sha,
        "byte_length": len(raw),
        "char_length": len(text),
        "multibyte_chars": multibyte,
        "astral_chars": astral,
        "line_endings": line_ending_stats(text),
        "has_bom": text.startswith("﻿"),
    }


class IntakeService(Service):
    name = "intake"

    def __init__(self, db_path: str, object_dir: str):
        super().__init__()
        self.db_path = db_path
        self.object_dir = object_dir
        ensure_parent(db_path)
        os.makedirs(object_dir, exist_ok=True)
        self._init_db()
        self._register_routes()

    # -- schema -----------------------------------------------------------
    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    latest_revision_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    revision_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    byte_length INTEGER NOT NULL,
                    char_length INTEGER NOT NULL,
                    multibyte_chars INTEGER NOT NULL,
                    astral_chars INTEGER NOT NULL,
                    line_endings_json TEXT NOT NULL,
                    has_bom INTEGER NOT NULL,
                    object_name TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_revisions (
                    doc_id TEXT NOT NULL REFERENCES documents(doc_id),
                    revision_number INTEGER NOT NULL,
                    revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
                    filename TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (doc_id, revision_number),
                    UNIQUE (doc_id, revision_id)
                );
                """
            )
            conn.commit()

    # -- helpers ----------------------------------------------------------
    def _object_path(self, object_name: str) -> str:
        return os.path.join(self.object_dir, object_name)

    def _read_object(self, object_name: str, expected_sha: str) -> bytes:
        path = self._object_path(object_name)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            raise AppError(
                500,
                "source_missing",
                f"Source object {object_name} is missing from storage",
                {"sha256": expected_sha},
            )
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected_sha:
            raise AppError(
                500,
                "source_corrupt",
                "Stored source bytes no longer match their content hash",
                {"expected": expected_sha, "actual": actual},
            )
        return raw

    @staticmethod
    def _decode_payload(body: Any) -> Tuple[bytes, str, str]:
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        filename = body.get("filename")
        if not isinstance(filename, str) or not _FILENAME_RE.fullmatch(filename):
            raise AppError(422, "invalid_filename", "filename must be 1-200 printable characters")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise AppError(422, "invalid_note", "note must be a string of at most 500 characters")
        if "content_base64" in body:
            encoded = body["content_base64"]
            if not isinstance(encoded, str):
                raise AppError(422, "invalid_content", "content_base64 must be a string")
            encoded = encoded.strip()
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise AppError(422, "invalid_content", f"content_base64 is not valid base64: {exc}")
        elif "text" in body:
            text = body["text"]
            if not isinstance(text, str):
                raise AppError(422, "invalid_content", "text must be a string")
            raw = text.encode("utf-8")
        else:
            raise AppError(422, "invalid_content", "Provide content_base64 or text")
        if len(raw) > MAX_SOURCE_BYTES:
            raise AppError(
                413,
                "source_too_large",
                f"Source is {len(raw)} bytes; limit is {MAX_SOURCE_BYTES}",
            )
        decode_utf8(raw, "source")
        return raw, filename, note

    def _store_source(self, raw: bytes) -> Dict[str, Any]:
        meta = _source_metadata(raw)
        object_name = meta["sha256"] + ".txt"
        path = self._object_path(object_name)
        if not os.path.exists(path):
            temp = path + ".tmp-" + secrets.token_hex(4)
            with open(temp, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        return meta

    @staticmethod
    def _revision_payload(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data.pop("object_name", None)
        data["line_endings"] = json.loads(data.pop("line_endings_json"))
        data["has_bom"] = bool(data["has_bom"])
        return data

    def _find_revision(self, conn: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            raise AppError(404, "revision_not_found", f"Unknown revision {revision_id}")
        return row

    def _upload_to_document(
        self, conn: sqlite3.Connection, doc_id: str, raw: bytes, filename: str, note: str
    ) -> Dict[str, Any]:
        doc = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if doc is None:
            raise AppError(404, "document_not_found", f"Unknown document {doc_id}")
        meta = self._store_source(raw)
        existing = conn.execute(
            "SELECT * FROM revisions WHERE revision_id=?", (meta["revision_id"],)
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO revisions
                   (revision_id, sha256, byte_length, char_length, multibyte_chars,
                    astral_chars, line_endings_json, has_bom, object_name,
                    original_filename, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta["revision_id"],
                    meta["sha256"],
                    meta["byte_length"],
                    meta["char_length"],
                    meta["multibyte_chars"],
                    meta["astral_chars"],
                    json.dumps(meta["line_endings"], sort_keys=True),
                    1 if meta["has_bom"] else 0,
                    meta["sha256"] + ".txt",
                    filename,
                    now_iso(),
                ),
            )
        attached = conn.execute(
            "SELECT * FROM document_revisions WHERE doc_id=? AND revision_id=?",
            (doc_id, meta["revision_id"]),
        ).fetchone()
        if attached is None:
            last = conn.execute(
                "SELECT MAX(revision_number) AS n FROM document_revisions WHERE doc_id=?",
                (doc_id,),
            ).fetchone()
            number = int(last["n"] or 0) + 1
            conn.execute(
                """INSERT INTO document_revisions
                   (doc_id, revision_number, revision_id, filename, note, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (doc_id, number, meta["revision_id"], filename, note, now_iso()),
            )
            conn.execute(
                "UPDATE documents SET latest_revision_id=? WHERE doc_id=?",
                (meta["revision_id"], doc_id),
            )
            created = True
        else:
            number = int(attached["revision_number"])
            created = False
        revision = conn.execute(
            "SELECT * FROM revisions WHERE revision_id=?", (meta["revision_id"],)
        ).fetchone()
        return {
            "created": created,
            "revision_number": number,
            "revision": self._revision_payload(revision),
        }

    # -- routes -----------------------------------------------------------
    def _register_routes(self) -> None:
        self.route("GET", r"/api/health", self.health)
        self.route("GET", r"/api/documents", self.list_documents)
        self.route("POST", r"/api/documents", self.create_document)
        self.route("GET", r"/api/documents/([^/]+)", self.get_document)
        self.route("POST", r"/api/documents/([^/]+)/revisions", self.add_revision)
        self.route("GET", r"/api/revisions/([^/]+)", self.get_revision)
        self.route("GET", r"/api/revisions/([^/]+)/source", self.get_source)
        self.route("POST", r"/api/revisions/([^/]+)/slice", self.slice_source)
        self.route("POST", r"/api/revisions/([^/]+)/search", self.search_source)
        self.route("GET", r"/api/storage", self.storage_cost)

    def health(self, match, query, body) -> Response:
        return Response({"service": self.name, "ok": True, "time": now_iso()})

    def list_documents(self, match, query, body) -> Response:
        with connect(self.db_path) as conn:
            docs = conn.execute("SELECT * FROM documents ORDER BY created_at, doc_id").fetchall()
            out = []
            for doc in docs:
                revisions = conn.execute(
                    """SELECT dr.revision_number, dr.revision_id, dr.note, dr.created_at,
                              r.byte_length, r.char_length, r.sha256, r.multibyte_chars
                       FROM document_revisions dr
                       JOIN revisions r ON r.revision_id = dr.revision_id
                       WHERE dr.doc_id=? ORDER BY dr.revision_number""",
                    (doc["doc_id"],),
                ).fetchall()
                out.append(
                    {
                        "doc_id": doc["doc_id"],
                        "filename": doc["filename"],
                        "created_at": doc["created_at"],
                        "latest_revision_id": doc["latest_revision_id"],
                        "revision_count": len(revisions),
                        "revisions": rows_to_dicts(revisions),
                    }
                )
        return Response({"documents": out})

    def create_document(self, match, query, body) -> Response:
        raw, filename, note = self._decode_payload(body)
        doc_id = _doc_id()
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO documents (doc_id, filename, created_at, latest_revision_id) VALUES (?,?,?,?)",
                (doc_id, filename, now_iso(), ""),
            )
            result = self._upload_to_document(conn, doc_id, raw, filename, note)
            doc = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            conn.commit()
        return Response(
            {"document": dict(doc), **result},
            status=201,
            headers={"Location": f"/api/documents/{doc_id}"},
        )

    def get_document(self, match, query, body) -> Response:
        doc_id = match.group(1)
        with connect(self.db_path) as conn:
            doc = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            if doc is None:
                raise AppError(404, "document_not_found", f"Unknown document {doc_id}")
            revisions = conn.execute(
                """SELECT dr.revision_number, dr.revision_id, dr.filename, dr.note, dr.created_at,
                          r.byte_length, r.char_length, r.sha256, r.multibyte_chars,
                          r.astral_chars, r.line_endings_json, r.has_bom
                   FROM document_revisions dr
                   JOIN revisions r ON r.revision_id = dr.revision_id
                   WHERE dr.doc_id=? ORDER BY dr.revision_number""",
                (doc_id,),
            ).fetchall()
        items = []
        for row in revisions:
            item = dict(row)
            item["line_endings"] = json.loads(item.pop("line_endings_json"))
            item["has_bom"] = bool(item["has_bom"])
            items.append(item)
        return Response({"document": dict(doc), "revisions": items})

    def add_revision(self, match, query, body) -> Response:
        doc_id = match.group(1)
        raw, filename, note = self._decode_payload(body)
        with connect(self.db_path) as conn:
            result = self._upload_to_document(conn, doc_id, raw, filename, note)
            doc = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
            conn.commit()
        return Response(
            {
                "document": dict(doc),
                **result,
                "message": (
                    "Identical bytes already attached; existing revision reused"
                    if not result["created"]
                    else "New immutable source revision stored"
                ),
            },
            status=201 if result["created"] else 200,
        )

    def get_revision(self, match, query, body) -> Response:
        revision_id = match.group(1)
        with connect(self.db_path) as conn:
            row = self._find_revision(conn, revision_id)
            docs = conn.execute(
                """SELECT dr.doc_id, dr.revision_number, dr.note, dr.created_at, d.filename
                   FROM document_revisions dr JOIN documents d ON d.doc_id=dr.doc_id
                   WHERE dr.revision_id=? ORDER BY dr.created_at""",
                (revision_id,),
            ).fetchall()
        payload = self._revision_payload(row)
        payload["documents"] = rows_to_dicts(docs)
        return Response(payload)

    def get_source(self, match, query, body) -> Response:
        revision_id = match.group(1)
        with connect(self.db_path) as conn:
            row = self._find_revision(conn, revision_id)
        raw = self._read_object(row["object_name"], row["sha256"])
        return Response(
            raw=raw,
            content_type="text/plain; charset=utf-8",
            headers={
                "X-Source-Revision": revision_id,
                "X-Source-Sha256": row["sha256"],
                "X-Source-Bytes": str(row["byte_length"]),
                "X-Source-Chars": str(row["char_length"]),
                "Content-Disposition": f"inline; filename={row['sha256'][:12]}.txt",
            },
        )

    def _char_to_byte_table(self, text: str) -> List[int]:
        return utf8_prefix(text)

    def slice_source(self, match, query, body) -> Response:
        revision_id = match.group(1)
        if not isinstance(body, dict):
            raise AppError(400, "invalid_body", "Expected a JSON object")
        unit = body.get("unit", "char")
        if unit not in ("char", "byte"):
            raise AppError(422, "invalid_unit", "unit must be 'char' or 'byte'")
        with connect(self.db_path) as conn:
            row = self._find_revision(conn, revision_id)
        raw = self._read_object(row["object_name"], row["sha256"])
        text = decode_utf8(raw, "source")
        prefix = self._char_to_byte_table(text)
        limit = row["byte_length"] if unit == "byte" else row["char_length"]
        start = int_param(body.get("start"), "start")
        end = int_param(body.get("end"), "end")
        if start < 0 or end < start or end > limit:
            raise AppError(
                422,
                "range_out_of_bounds",
                f"{unit} range [{start}, {end}) is outside 0..{limit}",
            )
        if unit == "char":
            char_start, char_end = start, end
            byte_start, byte_end = prefix[char_start], prefix[char_end]
        else:
            boundaries = set(prefix)
            for position in (start, end):
                if position not in boundaries:
                    nearest = min(prefix, key=lambda value: abs(value - position))
                    raise AppError(
                        422,
                        "byte_not_on_char_boundary",
                        f"Byte offset {position} splits a multi-byte UTF-8 character",
                        {
                            "offset": position,
                            "nearest_character_boundary": nearest,
                            "hint": "Use unit='char' for character positions",
                        },
                    )
            byte_start, byte_end = start, end
            char_start = _bisect(prefix, byte_start)
            char_end = _bisect(prefix, byte_end)
        if byte_end - byte_start > MAX_SOURCE_BYTES:
            raise AppError(413, "slice_too_large", "Requested slice is too large")
        slice_text = text[char_start:char_end]
        return Response(
            {
                "revision_id": revision_id,
                "unit": unit,
                "requested": {"start": start, "end": end},
                "char_start": char_start,
                "char_end": char_end,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "char_length": char_end - char_start,
                "byte_length": byte_end - byte_start,
                "text": slice_text,
                "sha256_of_slice": hashlib.sha256(
                    text[char_start:char_end].encode("utf-8")
                ).hexdigest(),
                "note": "byte offsets refer to the immutable stored source revision",
            }
        )

    def search_source(self, match, query, body) -> Response:
        revision_id = match.group(1)
        if not isinstance(body, dict) or not isinstance(body.get("quote"), str):
            raise AppError(422, "invalid_quote", "quote must be a string")
        quote = body["quote"]
        if not quote:
            raise AppError(422, "invalid_quote", "quote must not be empty")
        if len(quote) > 2000:
            raise AppError(422, "invalid_quote", "quote must be at most 2000 characters")
        limit = int_param(body.get("limit"), "limit", 20)
        if limit < 1 or limit > 200:
            raise AppError(422, "invalid_limit", "limit must be between 1 and 200")
        with connect(self.db_path) as conn:
            row = self._find_revision(conn, revision_id)
        raw = self._read_object(row["object_name"], row["sha256"])
        text = decode_utf8(raw, "source")
        prefix = self._char_to_byte_table(text)
        occurrences = []
        cursor = 0
        while len(occurrences) < limit:
            found = text.find(quote, cursor)
            if found < 0:
                break
            end = found + len(quote)
            occurrences.append(
                {
                    "char_start": found,
                    "char_end": end,
                    "byte_start": prefix[found],
                    "byte_end": prefix[end],
                    "before": text[max(0, found - 24) : found],
                    "after": text[end : end + 24],
                }
            )
            cursor = found + 1
        return Response(
            {
                "revision_id": revision_id,
                "quote": quote,
                "occurrences": occurrences,
                "truncated": len(occurrences) >= limit,
            }
        )

    def storage_cost(self, match, query, body) -> Response:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM revisions").fetchall()
        objects = []
        total_object_bytes = 0
        for row in rows:
            path = self._object_path(row["object_name"])
            size = os.path.getsize(path) if os.path.exists(path) else 0
            total_object_bytes += size
            objects.append(
                {
                    "revision_id": row["revision_id"],
                    "byte_length": row["byte_length"],
                    "stored_object_bytes": size,
                    "sha256": row["sha256"],
                }
            )
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return Response(
            {
                "revision_count": len(rows),
                "source_object_bytes": total_object_bytes,
                "metadata_db_bytes": db_size,
                "total_bytes": total_object_bytes + db_size,
                "objects": objects,
                "note": "Source bytes are stored once per content hash and never rewritten in place.",
            }
        )


def _bisect(prefix: List[int], value: int) -> int:
    low, high = 0, len(prefix) - 1
    while low < high:
        mid = (low + high) // 2
        if prefix[mid] < value:
            low = mid + 1
        else:
            high = mid
    return low


def create_service(
    db_path: Optional[str] = None, object_dir: Optional[str] = None
) -> IntakeService:
    db_path = db_path or os.environ.get("INTAKE_DB", "data/intake/intake.sqlite3")
    object_dir = object_dir or os.environ.get("INTAKE_OBJECTS", "data/intake/objects")
    return IntakeService(db_path, object_dir)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Source intake service")
    parser.add_argument("--host", default=os.environ.get("INTAKE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INTAKE_PORT", "8101")))
    args = parser.parse_args(argv)
    service = create_service()
    server = serve(service, args.host, args.port)
    print(f"intake listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
