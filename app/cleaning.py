"""Cleaning engine with explicit, lossless position mapping.

A *ruleset revision* is an ordered list of parametrised rules.  Applying a
ruleset to a source version produces:

* clean text,
* one atom per clean code point, holding the source code-point span it came
  from (replacement characters are marked synthetic),
* compact copy/synthetic mapping segments for storage,
* deleted source ranges,
* per-rule statistics.

Everything is deterministic: the same source bytes plus the same canonical
ruleset JSON always produce byte-identical output and mapping.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .common import AppError, utf8_prefix

MAX_RULES = 24
RULE_TYPES = {
    "normalize_newlines",
    "strip_chars",
    "trim_lines",
    "collapse_spaces",
    "merge_paragraphs",
}

_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")


# ---------------------------------------------------------------------------
# Ruleset validation / canonicalisation
# ---------------------------------------------------------------------------
def canonical_rules(rules: Any) -> List[Dict[str, Any]]:
    if not isinstance(rules, list) or not rules:
        raise AppError(422, "invalid_rules", "rules must be a non-empty array")
    if len(rules) > MAX_RULES:
        raise AppError(422, "invalid_rules", f"at most {MAX_RULES} rules are allowed")
    seen = set()
    canonical: List[Dict[str, Any]] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise AppError(422, "invalid_rules", f"rule {index} must be an object")
        rule_type = rule.get("type")
        if rule_type not in RULE_TYPES:
            raise AppError(
                422,
                "invalid_rules",
                f"rule {index} has unknown type {rule_type!r}",
                {"allowed": sorted(RULE_TYPES)},
            )
        if rule_type in seen:
            raise AppError(422, "invalid_rules", f"rule type {rule_type!r} appears twice")
        seen.add(rule_type)
        cleaned: Dict[str, Any] = {"type": rule_type}
        extra = set(rule) - {"type", "chars", "replacement"}
        if extra:
            raise AppError(422, "invalid_rules", f"rule {index} has unknown fields {sorted(extra)}")
        if rule_type == "strip_chars":
            chars = rule.get("chars", "​‌‍﻿­")
            cleaned["chars"] = _validate_chars(chars, index)
        elif rule_type == "trim_lines":
            chars = rule.get("chars", " \t")
            cleaned["chars"] = _validate_chars(chars, index)
        elif rule_type == "collapse_spaces":
            chars = rule.get("chars", " \t")
            cleaned["chars"] = _validate_chars(chars, index)
            replacement = rule.get("replacement", " ")
            if not isinstance(replacement, str) or length(replacement) != 1:
                raise AppError(422, "invalid_rules", f"rule {index} replacement must be one character")
            if replacement in "\r\n":
                raise AppError(
                    422,
                    "invalid_rules",
                    f"rule {index} replacement must not be a line break",
                )
            cleaned["replacement"] = replacement
        elif rule_type == "merge_paragraphs":
            if "chars" in rule or "replacement" in rule:
                raise AppError(422, "invalid_rules", "merge_paragraphs takes no parameters")
        canonical.append(cleaned)
    return canonical


def _validate_chars(chars: Any, index: int) -> str:
    if not isinstance(chars, str) or not chars:
        raise AppError(422, "invalid_rules", f"rule {index} chars must be a non-empty string")
    if "\n" in chars or "\r" in chars:
        raise AppError(422, "invalid_rules", f"rule {index} chars must not contain line breaks")
    if len(set(chars)) != length(chars):
        # duplicates are harmless; normalise order to keep hashes stable
        chars = "".join(dict.fromkeys(chars))
    return "".join(sorted(set(chars)))


def canonical_ruleset(name: Any, rules: Any) -> Dict[str, Any]:
    if not isinstance(name, str) or not name or not _NAME_RE.fullmatch(name):
        raise AppError(
            422,
            "invalid_name",
            "name must be 1-64 printable characters",
        )
    canonical = canonical_rules(rules)
    # The ruleset revision identity covers the rules ONLY.  Two differently
    # named rulesets that clean identically therefore share one derived index,
    # and renaming a ruleset can never silently change derived offsets.
    identity = {"rules": canonical}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return {
        "name": name,
        "rules": canonical,
        "definition_json": encoded,
        "sha256": digest,
    }


def length(text: str) -> int:
    return len(text)


# ---------------------------------------------------------------------------
# Atoms and rule implementations
# ---------------------------------------------------------------------------
@dataclass
class Atom:
    ch: str
    start: int  # source code-point offset, inclusive
    end: int  # source code-point offset, exclusive
    synthetic: bool = False


@dataclass
class RuleStat:
    type: str
    deleted_chars: int = 0
    replaced_runs: int = 0
    inserted_newlines: int = 0
    merged_breaks: int = 0
    dropped_newlines: int = 0
    changed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "deleted_chars": self.deleted_chars,
            "replaced_runs": self.replaced_runs,
            "merged_breaks": self.merged_breaks,
            "dropped_newlines": self.dropped_newlines,
            "changed": self.changed,
        }


@dataclass
class Paragraph:
    ordinal: int
    clean_char_start: int
    clean_char_end: int
    clean_byte_start: int
    clean_byte_end: int
    source_char_start: int
    source_char_end: int
    source_byte_start: int
    source_byte_end: int
    text: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "clean_char_start": self.clean_char_start,
            "clean_char_end": self.clean_char_end,
            "clean_byte_start": self.clean_byte_start,
            "clean_byte_end": self.clean_byte_end,
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
            "source_byte_start": self.source_byte_start,
            "source_byte_end": self.source_byte_end,
            "char_count": self.clean_char_end - self.clean_char_start,
            "text": self.text,
        }


@dataclass
class CleanResult:
    clean_text: str
    atoms: List[Atom]
    segments: List[Dict[str, Any]]
    deleted_spans: List[Tuple[int, int]]
    rule_stats: List[Dict[str, Any]]
    source_char_count: int
    clean_char_count: int
    output_sha256: str
    paragraphs: List[Paragraph] = field(default_factory=list)


def apply_rules(source_text: str, rules: List[Dict[str, Any]]) -> CleanResult:
    atoms = [Atom(ch, i, i + 1) for i, ch in enumerate(source_text)]
    stats: List[RuleStat] = []
    for rule in rules:
        rule_type = rule["type"]
        stat = RuleStat(rule_type)
        if rule_type == "normalize_newlines":
            atoms = _normalize_newlines(atoms, stat)
        elif rule_type == "strip_chars":
            atoms = _strip_chars(atoms, rule["chars"], stat)
        elif rule_type == "trim_lines":
            atoms = _trim_lines(atoms, rule["chars"], stat)
        elif rule_type == "collapse_spaces":
            atoms = _collapse_spaces(atoms, rule["chars"], rule["replacement"], stat)
        elif rule_type == "merge_paragraphs":
            atoms = _merge_paragraphs(atoms, stat)
        else:  # guarded by canonical_rules
            raise AppError(500, "internal_error", f"unhandled rule {rule_type}")
        stats.append(stat)

    clean_text = "".join(atom.ch for atom in atoms)
    segments = _build_segments(atoms)
    deleted = _deleted_spans(atoms, source_text)
    clean_bytes = utf8_prefix(clean_text)
    result = CleanResult(
        clean_text=clean_text,
        atoms=atoms,
        segments=segments,
        deleted_spans=deleted,
        rule_stats=[stat.as_dict() for stat in stats],
        source_char_count=len(source_text),
        clean_char_count=len(clean_text),
        output_sha256=hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
    )
    result.paragraphs = segment_paragraphs(clean_text, atoms, clean_bytes, source_text)
    return result


def _normalize_newlines(atoms: List[Atom], stat: RuleStat) -> List[Atom]:
    out: List[Atom] = []
    i = 0
    while i < len(atoms):
        atom = atoms[i]
        if atom.ch == "\r":
            if i + 1 < len(atoms) and atoms[i + 1].ch == "\n":
                out.append(Atom("\n", atom.start, atoms[i + 1].end, synthetic=True))
                i += 2
            else:
                out.append(Atom("\n", atom.start, atom.end, synthetic=True))
                i += 1
            stat.replaced_runs += 1
            stat.changed = True
        else:
            out.append(atom)
            i += 1
    return out


def _strip_chars(atoms: List[Atom], chars: str, stat: RuleStat) -> List[Atom]:
    keep = set(chars)
    out: List[Atom] = []
    for atom in atoms:
        if atom.ch in keep:
            stat.deleted_chars += 1
            stat.changed = True
        else:
            out.append(atom)
    return out


def _trim_lines(atoms: List[Atom], chars: str, stat: RuleStat) -> List[Atom]:
    trimmable = set(chars)
    out: List[Atom] = []
    i = 0
    while i < len(atoms):
        if atoms[i].ch in "\r\n":
            out.append(atoms[i])
            i += 1
            continue
        line: List[Atom] = []
        while i < len(atoms) and atoms[i].ch not in "\r\n":
            line.append(atoms[i])
            i += 1
        start = 0
        end = len(line)
        while start < end and line[start].ch in trimmable:
            start += 1
        while end > start and line[end - 1].ch in trimmable:
            end -= 1
        stat.deleted_chars += start + (len(line) - end)
        if start or end != len(line):
            stat.changed = True
        out.extend(line[start:end])
    return out


def _collapse_spaces(
    atoms: List[Atom], chars: str, replacement: str, stat: RuleStat
) -> List[Atom]:
    collapsible = set(chars)
    out: List[Atom] = []
    i = 0
    while i < len(atoms):
        if atoms[i].ch not in collapsible:
            out.append(atoms[i])
            i += 1
            continue
        run_start = i
        while i < len(atoms) and atoms[i].ch in collapsible:
            i += 1
        if i - run_start == 1:
            out.append(atoms[run_start])
        else:
            out.append(
                Atom(replacement, atoms[run_start].start, atoms[i - 1].end, synthetic=True)
            )
            stat.replaced_runs += 1
            stat.deleted_chars += (i - run_start) - 1
            stat.changed = True
    return out


def _merge_paragraphs(atoms: List[Atom], stat: RuleStat) -> List[Atom]:
    """Single newlines between text lines become spaces; blank-line separators
    collapse to exactly two newlines; newlines at the start/end are dropped."""
    lines: List[List[Atom]] = [[]]
    separators: List[List[Atom]] = []
    current: List[Atom] = []
    for atom in atoms:
        if atom.ch in "\r\n":
            lines[-1].extend(current)
            current = []
            separators.append([atom])
            lines.append([])
        else:
            current.append(atom)
    lines[-1].extend(current)

    def non_empty(line: List[Atom]) -> bool:
        return any(atom.ch.strip() for atom in line)

    out: List[Atom] = []
    pending: List[Atom] = []
    have_content = False
    for index, line in enumerate(lines):
        if index > 0:
            pending.extend(separators[index - 1])
        if not non_empty(line):
            continue
        if have_content and pending:
            if len(pending) == 1:
                out.append(Atom(" ", pending[0].start, pending[0].end, synthetic=True))
                stat.merged_breaks += 1
                stat.changed = True
            else:
                out.append(Atom("\n", pending[0].start, pending[0].end, synthetic=True))
                second = pending[1]
                out.append(Atom("\n", second.start, second.end, synthetic=True))
                stat.dropped_newlines += len(pending) - 2
                stat.changed = True
        elif not have_content and pending:
            stat.dropped_newlines += len(pending)
            stat.changed = True
        pending = []
        out.extend(line)
        have_content = True
    if pending:
        stat.dropped_newlines += len(pending)
        stat.changed = True
    return out


# ---------------------------------------------------------------------------
# Mapping artefacts
# ---------------------------------------------------------------------------
def _build_segments(atoms: List[Atom]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for clean_index, atom in enumerate(atoms):
        delta = atom.start - clean_index
        if not atom.synthetic:
            if (
                current is not None
                and current["synthetic"] == 0
                and current["_delta"] == delta
            ):
                current["clean_char_end"] = clean_index + 1
                current["source_char_end"] = atom.end
                continue
            current = {
                "clean_char_start": clean_index,
                "clean_char_end": clean_index + 1,
                "source_char_start": atom.start,
                "source_char_end": atom.end,
                "synthetic": 0,
                "_delta": delta,
            }
            segments.append(current)
        else:
            if (
                current is not None
                and current["synthetic"] == 1
                and current["source_char_end"] == atom.start
                and current["_clean_len"] == (current["clean_char_end"] - current["clean_char_start"])
            ):
                current["clean_char_end"] = clean_index + 1
                current["source_char_end"] = atom.end
                current["_clean_len"] += 1
                continue
            current = {
                "clean_char_start": clean_index,
                "clean_char_end": clean_index + 1,
                "source_char_start": atom.start,
                "source_char_end": atom.end,
                "synthetic": 1,
                "_clean_len": 1,
            }
            segments.append(current)
    for segment in segments:
        segment.pop("_delta", None)
        segment.pop("_clean_len", None)
    return segments


def _deleted_spans(atoms: List[Atom], source_text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    cursor = 0
    for atom in atoms:
        if atom.start > cursor:
            if spans and spans[-1][1] == cursor:
                spans[-1] = (spans[-1][0], atom.start)
            else:
                spans.append((cursor, atom.start))
        cursor = max(cursor, atom.end)
    if cursor < len(source_text):
        if spans and spans[-1][1] == cursor:
            spans[-1] = (spans[-1][0], len(source_text))
        else:
            spans.append((cursor, len(source_text)))
    return spans


def map_clean_to_source(
    segments: List[Dict[str, Any]], clean_start: int, clean_end: int
) -> List[Dict[str, Any]]:
    """Source spans for a clean character range.  Character positions, not bytes."""
    out: List[Dict[str, Any]] = []
    for segment in segments:
        start = max(clean_start, segment["clean_char_start"])
        end = min(clean_end, segment["clean_char_end"])
        if start >= end:
            continue
        if segment["synthetic"]:
            source_start = segment["source_char_start"]
            source_end = segment["source_char_end"]
        else:
            source_start = segment["source_char_start"] + (start - segment["clean_char_start"])
            source_end = source_start + (end - start)
        out.append(
            {
                "clean_char_start": start,
                "clean_char_end": end,
                "source_char_start": source_start,
                "source_char_end": source_end,
                "synthetic": segment["synthetic"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Paragraph segmentation
# ---------------------------------------------------------------------------
_BLANK_LINE_RE = re.compile(r"(?:\r\n|\r|\n){2,}")


def segment_paragraphs(
    clean_text: str,
    atoms: List[Atom],
    clean_bytes: List[int],
    source_text: str,
) -> List[Paragraph]:
    boundaries: List[Tuple[int, int]] = []
    for match in _BLANK_LINE_RE.finditer(clean_text):
        boundaries.append((match.start(), match.end()))
    paragraphs: List[Paragraph] = []
    cursor = 0
    raw_ranges: List[Tuple[int, int]] = []
    for start, end in boundaries:
        if start > cursor:
            raw_ranges.append((cursor, start))
        cursor = end
    if cursor < len(clean_text):
        raw_ranges.append((cursor, len(clean_text)))

    source_bytes = utf8_prefix(source_text)
    ordinal = 0
    for raw_start, raw_end in raw_ranges:
        start, end = raw_start, raw_end
        while start < end and clean_text[start].isspace():
            start += 1
        while end > start and clean_text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue
        if not atoms:
            continue
        source_char_start = atoms[start].start
        source_char_end = atoms[end - 1].end
        paragraphs.append(
            Paragraph(
                ordinal=ordinal,
                clean_char_start=start,
                clean_char_end=end,
                clean_byte_start=clean_bytes[start],
                clean_byte_end=clean_bytes[end],
                source_char_start=source_char_start,
                source_char_end=source_char_end,
                source_byte_start=source_bytes[source_char_start],
                source_byte_end=source_bytes[source_char_end],
                text=clean_text[start:end],
            )
        )
        ordinal += 1
    return paragraphs


def find_occurrences(text: str, quote: str, limit: int = 200) -> List[Tuple[int, int]]:
    """Literal (non-overlapping) search over *characters* of clean text."""
    if not quote:
        return []
    out: List[Tuple[int, int]] = []
    start = 0
    while len(out) < limit:
        found = text.find(quote, start)
        if found < 0:
            break
        out.append((found, found + len(quote)))
        start = found + 1
    return out


def snippet(text: str, start: int, end: int, limit: int = 32) -> Tuple[str, str]:
    before = text[max(0, start - limit) : start]
    after = text[end : end + limit]
    return before, after
