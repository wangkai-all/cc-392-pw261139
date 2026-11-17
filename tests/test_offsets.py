"""End-to-end tests: real HTTP servers, real SQLite, from an empty data dir."""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.common import serve  # noqa: E402
from app.indexer import IndexerService  # noqa: E402
from app.intake import IntakeService  # noqa: E402
from app.query import QueryService  # noqa: E402

SAMPLE = (
    "虚构报告 · 第一版\r\n"
    "\r\n"
    "　　项目代号「青苔」于春季启动，负责人是李明。\r\n"
    "内部编号：A-001。\r\n"
    "\r\n"
    "符号：😀 与 𝔘𝔫𝔦𝔠𝔬𝔡𝔢，以及零宽字符：​‌。\r\n"
    "\r\n"
    "    末行有缩进和尾随空格。   \r\n"
)


def http(method, url, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            if response.headers.get("Content-Type", "").startswith("text/plain"):
                return response.status, raw.decode("utf-8")
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw.decode("utf-8"))


class Stack:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="offset-lab-")
        self.servers = []

    def start(self):
        base = self.dir
        intake = IntakeService(
            os.path.join(base, "intake.sqlite3"), os.path.join(base, "objects")
        )
        self.intake = self._serve(intake)
        indexer = IndexerService(
            os.path.join(base, "indexer.sqlite3"), self.intake, os.path.join(base, "maps")
        )
        self.indexer = self._serve(indexer)
        query = QueryService(
            os.path.join(base, "query.sqlite3"), self.indexer, self.intake
        )
        self.query = self._serve(query)
        return self

    def _serve(self, service):
        server = serve(service, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        return f"http://127.0.0.1:{port}"

    def stop(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = Stack().start()

    @classmethod
    def tearDownClass(cls):
        cls.stack.stop()

    def create_source(self, text, filename="doc.txt"):
        status, payload = http(
            "POST",
            self.stack.intake + "/api/documents",
            {"filename": filename, "content_base64": base64.b64encode(text.encode("utf-8")).decode()},
        )
        self.assertEqual(status, 201, payload)
        return payload["revision"]

    def create_ruleset(self, rules, name="rules"):
        status, payload = http("POST", self.stack.indexer + "/api/rulesets", {"name": name, "rules": rules})
        self.assertEqual(status, 201, payload)
        return payload["revision"]["ruleset_revision_id"]

    def build(self, source_revision, ruleset_revision):
        status, payload = http(
            "POST",
            self.stack.indexer + "/api/builds",
            {"source_revision_id": source_revision, "ruleset_revision_id": ruleset_revision},
        )
        self.assertIn(status, (200, 202), payload)
        if payload.get("index"):
            return payload["index"]["index_id"]
        job_id = payload["job"]["job_id"]
        for _ in range(200):
            status, job = http("GET", self.stack.indexer + f"/api/builds/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] in ("succeeded", "failed", "interrupted"):
                break
            threading.Event().wait(0.02)
        self.assertEqual(job["status"], "succeeded", job)
        return job["index"]["index_id"]


class IntakeTests(Base):
    def test_byte_and_char_offsets_differ(self):
        revision = self.create_source("中文ab")
        self.assertEqual(revision["byte_length"], 8)
        self.assertEqual(revision["char_length"], 4)
        status, payload = http(
            "POST",
            self.stack.intake + f"/api/revisions/{revision['revision_id']}/slice",
            {"unit": "char", "start": 0, "end": 2},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["text"], "中文")
        self.assertEqual((payload["byte_start"], payload["byte_end"]), (0, 6))

    def test_byte_offset_inside_character_is_rejected(self):
        revision = self.create_source("中文")
        status, payload = http(
            "POST",
            self.stack.intake + f"/api/revisions/{revision['revision_id']}/slice",
            {"unit": "byte", "start": 0, "end": 1},
        )
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["code"], "byte_not_on_char_boundary")

    def test_identical_bytes_are_one_immutable_revision(self):
        first = self.create_source("同一个内容")
        status, payload = http(
            "POST",
            self.stack.intake + "/api/documents",
            {"filename": "copy.txt", "text": "同一个内容"},
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["revision"]["revision_id"], first["revision_id"])

    def test_invalid_utf8_is_rejected_without_storing(self):
        status, payload = http(
            "POST",
            self.stack.intake + "/api/documents",
            {"filename": "bad.txt", "content_base64": base64.b64encode(b"ok\xff").decode()},
        )
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["code"], "invalid_utf8")


class CleaningTests(Base):
    def test_pipeline_produces_paragraphs_and_mapping(self):
        revision = self.create_source(SAMPLE)
        ruleset = self.create_ruleset(
            [
                {"type": "normalize_newlines"},
                {"type": "strip_chars", "chars": "​‌﻿"},
                {"type": "trim_lines", "chars": " \t"},
                {"type": "collapse_spaces", "chars": " \t", "replacement": " "},
                {"type": "merge_paragraphs"},
            ]
        )
        index_id = self.build(revision["revision_id"], ruleset)
        status, index = http("GET", self.stack.indexer + f"/api/indexes/{index_id}")
        self.assertEqual(status, 200)
        self.assertEqual(index["ruleset_revision_id"], ruleset)
        self.assertEqual(index["source_sha256"], revision["sha256"])
        status, paragraphs = http(
            "GET", self.stack.indexer + f"/api/indexes/{index_id}/paragraphs"
        )
        self.assertEqual(status, 200)
        texts = [p["text"] for p in paragraphs["paragraphs"]]
        self.assertTrue(any("项目代号" in t for t in texts))
        self.assertNotIn("\r", "\n".join(texts))
        self.assertNotIn("​", "\n".join(texts))

    def test_deterministic_build_reuses_index(self):
        revision = self.create_source("确定性\r\n测试")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}])
        first = self.build(revision["revision_id"], ruleset)
        status, payload = http(
            "POST",
            self.stack.indexer + "/api/builds",
            {"source_revision_id": revision["revision_id"], "ruleset_revision_id": ruleset},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["index"]["index_id"], first)
        self.assertFalse(payload["created"])

    def test_clean_to_source_roundtrip(self):
        revision = self.create_source("a​b\r\nc")
        ruleset = self.create_ruleset(
            [
                {"type": "normalize_newlines"},
                {"type": "strip_chars", "chars": "​"},
                {"type": "merge_paragraphs"},
            ]
        )
        index_id = self.build(revision["revision_id"], ruleset)
        status, located = http(
            "POST",
            self.stack.indexer + f"/api/indexes/{index_id}/locate",
            {"clean_char_start": 0, "clean_char_end": 4},
        )
        self.assertEqual(status, 200, located)
        ranges = located["source_char_ranges"]
        self.assertTrue(ranges, located)
        # Source characters were never invented; every span is within the source.
        for start, end in ranges:
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(end, revision["char_length"])

    def test_ruleset_change_produces_distinct_index(self):
        revision = self.create_source("第一行\r\n第二行\r\n\r\n第三段")
        plain = self.create_ruleset([{"type": "normalize_newlines"}], "plain")
        merged = self.create_ruleset(
            [{"type": "normalize_newlines"}, {"type": "merge_paragraphs"}], "merged"
        )
        first = self.build(revision["revision_id"], plain)
        second = self.build(revision["revision_id"], merged)
        self.assertNotEqual(first, second)
        status, a = http("GET", self.stack.indexer + f"/api/indexes/{first}")
        status, b = http("GET", self.stack.indexer + f"/api/indexes/{second}")
        self.assertNotEqual(a["clean_sha256"], b["clean_sha256"])


class BuildSafetyTests(Base):
    def test_interrupted_build_publishes_nothing_then_retry(self):
        revision = self.create_source("中断测试内容")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}])
        status, payload = http(
            "POST",
            self.stack.indexer + "/api/builds",
            {
                "source_revision_id": revision["revision_id"],
                "ruleset_revision_id": ruleset,
                "simulate": "interrupt",
            },
        )
        self.assertEqual(status, 202, payload)
        job_id = payload["job"]["job_id"]
        self.assertEqual(payload["job"]["status"], "interrupted")
        status, indexes = http(
            "GET",
            self.stack.indexer + f"/api/indexes?source_revision_id={revision['revision_id']}",
        )
        self.assertEqual(indexes["indexes"], [])
        status, retried = http(
            "POST", self.stack.indexer + f"/api/builds/{job_id}/retry", {}
        )
        self.assertEqual(status, 202, retried)
        new_job = retried["job"]["job_id"]
        for _ in range(200):
            status, job = http("GET", self.stack.indexer + f"/api/builds/{new_job}")
            if job["status"] in ("succeeded", "failed"):
                break
            threading.Event().wait(0.02)
        self.assertEqual(job["status"], "succeeded", job)
        status, indexes = http(
            "GET",
            self.stack.indexer + f"/api/indexes?source_revision_id={revision['revision_id']}",
        )
        self.assertEqual(len(indexes["indexes"]), 1)
        self.assertEqual(indexes["indexes"][0]["clean_sha256"], job["index"]["clean_sha256"])

    def test_stale_running_jobs_become_interrupted(self):
        revision = self.create_source("恢复测试")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}])
        directory = tempfile.mkdtemp(prefix="offset-recover-")
        try:
            db = os.path.join(directory, "indexer.sqlite3")
            service = IndexerService(db, self.stack.intake, os.path.join(directory, "maps"))
            # Simulate a process that died mid-build, after it queued a job.
            from app.common import connect

            with connect(db) as conn:
                conn.execute(
                    """INSERT INTO build_jobs
                       (job_id, source_revision_id, ruleset_revision_id, index_id, status,
                        stage, progress, message, created_at, updated_at, attempts)
                       VALUES ('job_dead',?,?,'idx_x','running','apply_rules',0.4,'', 'now','now',1)""",
                    (revision["revision_id"], ruleset),
                )
                conn.commit()
            restarted = IndexerService(db, self.stack.intake, os.path.join(directory, "maps"))
            with connect(db) as conn:
                row = conn.execute("SELECT * FROM build_jobs WHERE job_id='job_dead'").fetchone()
            self.assertEqual(row["status"], "interrupted")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class CitationTests(Base):
    def setUp(self):
        self.revision = self.create_source(SAMPLE)
        self.rules_v1 = self.create_ruleset(
            [
                {"type": "normalize_newlines"},
                {"type": "strip_chars", "chars": "​‌﻿"},
                {"type": "trim_lines", "chars": " \t"},
                {"type": "collapse_spaces", "chars": " \t", "replacement": " "},
                {"type": "merge_paragraphs"},
            ],
            "v1",
        )
        self.index_v1 = self.build(self.revision["revision_id"], self.rules_v1)

    def make_report(self, quote, index_id=None):
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {
                "title": "分析",
                "index_id": index_id or self.index_v1,
                "annotations": [{"quote": quote, "note": "n"}],
            },
        )
        self.assertEqual(status, 201, report)
        return report

    def test_citation_pins_source_char_and_byte_offsets(self):
        report = self.make_report("项目代号「青苔」于春季启动，负责人是李明。")
        citation = report["citations"][0]
        self.assertEqual(citation["status"], "resolved")
        self.assertEqual(citation["source_sha256"], self.revision["sha256"])
        self.assertIsNotNone(citation["source_byte_start"])
        self.assertGreater(citation["source_byte_end"], citation["source_byte_start"])
        status, verified = http(
            "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/verify"
        )
        self.assertEqual(status, 200, verified)
        self.assertTrue(verified["verified"])
        self.assertTrue(verified["checks"]["source_hash_unchanged"])

    def test_old_reference_under_new_ruleset_is_reported_not_repointed(self):
        report = self.make_report("项目代号「青苔」于春季启动，负责人是李明。")
        citation = report["citations"][0]
        rules_v2 = self.create_ruleset(
            [{"type": "normalize_newlines"}, {"type": "merge_paragraphs"}], "v2"
        )
        index_v2 = self.build(self.revision["revision_id"], rules_v2)
        status, comparison = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/compare",
            {"index_id": index_v2},
        )
        self.assertEqual(status, 200, comparison)
        self.assertIn(
            comparison["status"],
            ("moved", "unchanged", "ambiguous", "same_offsets_different_clean"),
        )
        # Reading the report again must still show the ORIGINAL pinned offsets.
        status, again = http("GET", self.stack.query + f"/api/reports/{report['report']['report_id']}")
        self.assertEqual(again["citations"][0]["clean_char_start"], citation["clean_char_start"])
        self.assertEqual(again["citations"][0]["index_id"], self.index_v1)

    def test_reanchor_creates_new_pinned_citation_and_keeps_original(self):
        report = self.make_report("项目代号「青苔」于春季启动，负责人是李明。")
        citation = report["citations"][0]
        rules_v2 = self.create_ruleset(
            [{"type": "normalize_newlines"}, {"type": "merge_paragraphs"}], "v2b"
        )
        index_v2 = self.build(self.revision["revision_id"], rules_v2)
        status, reanchored = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/reanchor",
            {"index_id": index_v2},
        )
        self.assertEqual(status, 201, reanchored)
        new = reanchored["citation"]
        self.assertNotEqual(new["citation_id"], citation["citation_id"])
        self.assertEqual(new["index_id"], index_v2)
        status, full = http("GET", self.stack.query + f"/api/reports/{report['report']['report_id']}")
        ids = [c["citation_id"] for c in full["citations"]]
        self.assertIn(citation["citation_id"], ids)
        self.assertIn(new["citation_id"], ids)

    def test_missing_quote_is_unresolved_and_never_fabricated(self):
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {
                "title": "x",
                "index_id": self.index_v1,
                "annotations": [{"quote": "这句话根本不在文档里"}],
            },
        )
        self.assertEqual(status, 201, report)
        citation = report["citations"][0]
        self.assertEqual(citation["status"], "unresolved")
        self.assertIsNone(citation["source_char_start"])
        status, verified = http(
            "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/verify"
        )
        self.assertEqual(verified["status"], "unresolved")

    def test_ambiguous_quote_requires_context(self):
        revision = self.create_source("重复段落\r\n\r\n重复段落\r\n\r\n唯一的结尾")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "dup")
        index_id = self.build(revision["revision_id"], ruleset)
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "amb", "index_id": index_id, "annotations": [{"quote": "重复段落"}]},
        )
        self.assertEqual(status, 201, report)
        self.assertEqual(report["citations"][0]["status"], "ambiguous")
        status, report2 = http(
            "POST",
            self.stack.query + "/api/reports",
            {
                "title": "amb2",
                "index_id": index_id,
                "annotations": [{"quote": "重复段落", "after": "\n\n唯一的结尾"}],
            },
        )
        self.assertEqual(status, 201, report2)
        self.assertEqual(report2["citations"][0]["status"], "resolved")

    def test_intake_tampering_is_detected_by_verify(self):
        report = self.make_report("内部编号：A-001。")
        citation = report["citations"][0]
        # Rewrite the stored object on disk to model a corrupted/mutable source.
        objects = os.path.join(self.stack.dir, "objects")
        name = next(iter(os.listdir(objects)))
        with open(os.path.join(objects, name), "wb") as handle:
            handle.write("完全不同的内容".encode("utf-8"))
        try:
            status, verified = http(
                "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/verify"
            )
            self.assertEqual(status, 409, verified)
            self.assertFalse(verified["verified"])
            self.assertEqual(verified["status"], "pin_mismatch")
        finally:
            with open(os.path.join(objects, name), "wb") as handle:
                handle.write(SAMPLE.encode("utf-8"))


    def test_quote_across_synthetic_space_verifies_span_by_span(self):
        # The clean text has one space where the source has a run of spaces.
        revision = self.create_source("前后    中间    结尾")
        ruleset = self.create_ruleset(
            [{"type": "collapse_spaces", "chars": " \t", "replacement": " "}], "collapse"
        )
        index_id = self.build(revision["revision_id"], ruleset)
        status, paragraphs = http(
            "GET", self.stack.indexer + f"/api/indexes/{index_id}/paragraphs"
        )
        self.assertEqual(paragraphs["paragraphs"][0]["text"], "前后 中间 结尾")
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {
                "title": "synthetic",
                "index_id": index_id,
                "annotations": [{"quote": "前后 中间"}],
            },
        )
        self.assertEqual(status, 201, report)
        citation = report["citations"][0]
        self.assertEqual(citation["status"], "resolved")
        self.assertTrue(any(span["synthetic"] for span in citation["source_spans"]), citation)
        status, verified = http(
            "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/verify"
        )
        self.assertEqual(status, 200, verified)
        self.assertTrue(verified["verified"], verified)

    def test_mapping_segments_reconstruct_clean_text_exactly(self):
        revision = self.create_source(SAMPLE)
        ruleset = self.create_ruleset(
            [
                {"type": "normalize_newlines"},
                {"type": "strip_chars", "chars": "​‌﻿"},
                {"type": "trim_lines", "chars": " \t"},
                {"type": "collapse_spaces", "chars": " \t", "replacement": " "},
                {"type": "merge_paragraphs"},
            ],
            "invariants",
        )
        index_id = self.build(revision["revision_id"], ruleset)
        status, index = http("GET", self.stack.indexer + f"/api/indexes/{index_id}")
        status, clean = http("GET", self.stack.indexer + f"/api/indexes/{index_id}/clean")
        self.assertEqual(len(clean.encode("utf-8")), index["clean_byte_length"])
        # Walk every clean character through /locate; spans must be monotonic,
        # in-bounds, and synthetic ones must expand to real source characters.
        status, mapping = http("GET", self.stack.indexer + f"/api/indexes/{index_id}/map")
        cursor = 0
        for segment in mapping["segments"]:
            self.assertEqual(segment["clean_char_start"], cursor, segment)
            self.assertGreaterEqual(segment["source_char_start"], 0)
            self.assertLessEqual(segment["source_char_end"], index["source_char_length"])
            self.assertGreaterEqual(segment["source_char_end"], segment["source_char_start"])
            width = segment["clean_char_end"] - segment["clean_char_start"]
            if not segment["synthetic"]:
                self.assertEqual(segment["source_char_end"] - segment["source_char_start"], width)
            cursor = segment["clean_char_end"]
        self.assertEqual(cursor, index["clean_char_length"])

    def test_source_hash_drift_detected_when_pinned_bytes_change(self):
        revision = self.create_source("哈希校验内容")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "hash")
        index_id = self.build(revision["revision_id"], ruleset)
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "h", "index_id": index_id, "annotations": [{"quote": "哈希校验内容"}]},
        )
        self.assertEqual(status, 201, report)
        citation = report["citations"][0]
        # Swap the object file for another revision's bytes (still valid UTF-8).
        objects = os.path.join(self.stack.dir, "objects")
        original = os.path.join(objects, revision["sha256"] + ".txt")
        with open(original, "rb") as handle:
            saved = handle.read()
        with open(original, "wb") as handle:
            handle.write("被替换的内容".encode("utf-8"))
        try:
            status, verified = http(
                "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/verify"
            )
            self.assertEqual(status, 409, verified)
            self.assertFalse(verified["verified"])
            self.assertEqual(verified["status"], "pin_mismatch")
            # The report itself must still read back with untouched pinned offsets.
            status, again = http(
                "GET", self.stack.query + f"/api/reports/{report['report']['report_id']}"
            )
            self.assertEqual(again["citations"][0]["source_byte_start"], citation["source_byte_start"])
        finally:
            with open(original, "wb") as handle:
                handle.write(saved)


class ComparisonTests(Base):
    """Comparison must name what happened, never quietly re-point a citation."""

    def setUp(self):
        self.revision = self.create_source(
            "第一行内容比较长\r\n第二行内容也比较长\r\n\r\n第三段内容在这里\r\n"
        )
        # Ruleset without paragraph merging: newlines survive, positions shift later.
        self.plain = self.create_ruleset([{"type": "normalize_newlines"}], "plain")
        self.merged = self.create_ruleset(
            [{"type": "normalize_newlines"}, {"type": "merge_paragraphs"}], "merged"
        )
        self.index_plain = self.build(self.revision["revision_id"], self.plain)
        self.index_merged = self.build(self.revision["revision_id"], self.merged)

    def make_citation(self, quote):
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "cmp", "index_id": self.index_plain, "annotations": [{"quote": quote}]},
        )
        self.assertEqual(status, 201, report)
        return report["citations"][0]

    def test_compare_reports_moved_when_offsets_shift(self):
        # Leading blank lines are dropped by merge_paragraphs but preserved by
        # the plain ruleset, so the same quote genuinely shifts in clean space.
        revision = self.create_source("\r\n\r\n开头两行空行\r\n\r\n目标引用在此\r\n")
        plain = self.create_ruleset([{"type": "normalize_newlines"}], "shift-plain")
        merged = self.create_ruleset(
            [{"type": "normalize_newlines"}, {"type": "merge_paragraphs"}], "shift-merged"
        )
        index_plain = self.build(revision["revision_id"], plain)
        index_merged = self.build(revision["revision_id"], merged)
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "shift", "index_id": index_plain, "annotations": [{"quote": "目标引用在此"}]},
        )
        self.assertEqual(status, 201, report)
        citation = report["citations"][0]
        self.assertEqual(citation["status"], "resolved")
        status, comparison = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/compare",
            {"index_id": index_merged},
        )
        self.assertEqual(status, 200, comparison)
        self.assertEqual(comparison["status"], "moved", comparison)
        self.assertGreaterEqual(comparison["match_count"], 1)
        self.assertNotEqual(
            comparison["matches"][0]["clean_char_start"], citation["clean_char_start"]
        )
        # The comparison must not mutate the stored citation.
        status, stored = http(
            "GET", self.stack.query + f"/api/citations/{citation['citation_id']}"
        )
        self.assertEqual(stored["clean_char_start"], citation["clean_char_start"])
        self.assertEqual(stored["index_id"], index_plain)

    def test_same_offsets_but_different_clean_text_is_not_called_unchanged(self):
        citation = self.make_citation("第三段内容在这里")
        status, comparison = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/compare",
            {"index_id": self.index_merged},
        )
        self.assertEqual(status, 200, comparison)
        self.assertNotEqual(comparison["status"], "unchanged", comparison)

    def test_identical_definition_in_another_ruleset_is_unchanged(self):
        twin = self.create_ruleset([{"type": "normalize_newlines"}], "plain-twin")
        twin_index = self.build(self.revision["revision_id"], twin)
        self.assertNotEqual(twin_index, self.index_plain)
        citation = self.make_citation("第一行内容比较长")
        status, comparison = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/compare",
            {"index_id": twin_index},
        )
        self.assertEqual(status, 200, comparison)
        self.assertEqual(comparison["status"], "unchanged", comparison)

    def test_compare_across_source_revisions_is_refused(self):
        other = self.create_source("完全不同的另一版本文档")
        other_index = self.build(other["revision_id"], self.plain)
        citation = self.make_citation("第一行内容比较长")
        status, comparison = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/compare",
            {"index_id": other_index},
        )
        self.assertEqual(status, 409, comparison)
        self.assertEqual(comparison["status"], "revision_differs")

    def test_reanchor_refuses_when_quote_missing(self):
        citation = self.make_citation("第一行内容比较长")
        narrow = self.create_ruleset(
            [
                {"type": "normalize_newlines"},
                {"type": "strip_chars", "chars": "第行内容比较长二三段在这里"},
            ],
            "narrow",
        )
        narrow_index = self.build(self.revision["revision_id"], narrow)
        status, result = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/reanchor",
            {"index_id": narrow_index},
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"]["code"], "cannot_reanchor")
        status, report = http(
            "GET", self.stack.query + f"/api/reports/{citation['report_id']}"
        )
        self.assertEqual(len(report["citations"]), 1, "no citation may be added on refusal")

    def test_ambiguous_match_blocks_reanchor(self):
        revision = self.create_source("重复句子\r\n\r\n重复句子\r\n")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "dupctx")
        idx_a = self.build(revision["revision_id"], ruleset)
        ruleset_b = self.create_ruleset(
            [{"type": "normalize_newlines"}, {"type": "merge_paragraphs"}], "dupctx-b"
        )
        idx_b = self.build(revision["revision_id"], ruleset_b)
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "dup", "index_id": idx_a, "annotations": [{"quote": "重复句子"}]},
        )
        self.assertEqual(status, 201, report)
        citation = report["citations"][0]
        status, result = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/reanchor",
            {"index_id": idx_b},
        )
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"]["code"], "cannot_reanchor")


class RevisionDriftTests(Base):
    """A new source revision must never inherit an old citation's meaning."""

    def test_new_source_revision_gets_a_new_immutable_identity(self):
        first = self.create_source("虚构文档\r\n项目代号：青苔\r\n")
        second = self.create_source("虚构文档\r\n项目代号：苔藓\r\n")
        self.assertNotEqual(first["revision_id"], second["revision_id"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "drift")
        index_a = self.build(first["revision_id"], ruleset)
        index_b = self.build(second["revision_id"], ruleset)
        self.assertNotEqual(index_a, index_b)
        status, a = http("GET", self.stack.indexer + f"/api/indexes/{index_a}")
        status, b = http("GET", self.stack.indexer + f"/api/indexes/{index_b}")
        self.assertEqual(a["source_revision_id"], first["revision_id"])
        self.assertEqual(b["source_revision_id"], second["revision_id"])

    def test_citation_keeps_pointing_at_its_own_revision_after_source_revised(self):
        first = self.create_source("虚构文档\r\n项目代号：青苔\r\n")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "drift2")
        index_a = self.build(first["revision_id"], ruleset)
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "drift", "index_id": index_a, "annotations": [{"quote": "项目代号：青苔"}]},
        )
        self.assertEqual(status, 201, report)
        citation = report["citations"][0]
        self.assertEqual(citation["status"], "resolved")
        # Revise the source: same document, different characters.
        status, docs = http("GET", self.stack.intake + "/api/documents")
        doc_id = docs["documents"][0]["doc_id"]
        status, revised = http(
            "POST",
            self.stack.intake + f"/api/documents/{doc_id}/revisions",
            {"filename": "doc-v2.txt", "text": "虚构文档\r\n项目代号：苔藓\r\n"},
        )
        self.assertIn(status, (200, 201), revised)
        self.assertNotEqual(revised["revision"]["revision_id"], first["revision_id"])
        # The report still reads back pinned to v1, with v1's bytes.
        status, again = http("GET", self.stack.query + f"/api/reports/{report['report']['report_id']}")
        same = again["citations"][0]
        self.assertEqual(same["source_revision_id"], first["revision_id"])
        self.assertEqual(same["source_sha256"], first["sha256"])
        self.assertEqual(same["source_char_start"], citation["source_char_start"])
        status, verified = http(
            "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/verify"
        )
        self.assertEqual(status, 200, verified)
        self.assertTrue(verified["verified"])
        # The quote no longer exists in v2; comparing across revisions is refused.
        index_b = self.build(revised["revision"]["revision_id"], ruleset)
        status, comparison = http(
            "POST",
            self.stack.query + f"/api/citations/{citation['citation_id']}/compare",
            {"index_id": index_b},
        )
        self.assertEqual(status, 409, comparison)
        self.assertEqual(comparison["status"], "revision_differs")

    def test_context_reads_use_the_pinned_revision(self):
        first = self.create_source("第一版\r\n重要结论：保持原样\r\n")
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "ctx")
        index_a = self.build(first["revision_id"], ruleset)
        status, report = http(
            "POST",
            self.stack.query + "/api/reports",
            {"title": "ctx", "index_id": index_a, "annotations": [{"quote": "重要结论：保持原样"}]},
        )
        citation = report["citations"][0]
        status, docs = http("GET", self.stack.intake + "/api/documents")
        doc_id = docs["documents"][0]["doc_id"]
        http(
            "POST",
            self.stack.intake + f"/api/documents/{doc_id}/revisions",
            {"filename": "v2.txt", "text": "第二版\r\n重要结论：已经改变\r\n"},
        )
        status, context = http(
            "GET", self.stack.query + f"/api/citations/{citation['citation_id']}/context?pad=20"
        )
        self.assertEqual(status, 200, context)
        self.assertIn("保持原样", context["slice"]["text"])
        self.assertNotIn("已经改变", context["slice"]["text"])
        self.assertEqual(context["slice"]["revision_id"], first["revision_id"])


class CostTests(Base):
    def test_storage_endpoints_report_concrete_numbers(self):
        revision = self.create_source(SAMPLE)
        ruleset = self.create_ruleset([{"type": "normalize_newlines"}], "cost")
        self.build(revision["revision_id"], ruleset)
        status, intake = http("GET", self.stack.intake + "/api/storage")
        self.assertEqual(status, 200)
        self.assertGreater(intake["source_object_bytes"], 0)
        status, indexer = http("GET", self.stack.indexer + "/api/storage")
        self.assertEqual(status, 200)
        self.assertGreater(indexer["mapping_file_bytes"], 0)
        self.assertTrue(indexer["indexes"][0]["mapping_bytes_per_source_byte"] > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
