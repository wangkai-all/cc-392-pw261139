# 偏移实验室 · offset-preserving citations

Three independent services process fictional documents, clean them with
versioned rules, and store report citations that survive rule changes,
rebuilds and source revisions. Standard library only; no LLM retrieval.

```
python3 -m app.run            # UI :8100, intake :8101, indexer :8102
python3 -m app.run --reset    # wipe data/ first
python3 -m unittest discover -s tests -v
```

## Three services, three databases

| service | port | owns | state |
|---|---|---|---|
| intake | 8101 | raw bytes, documents, revisions | `data/intake/intake.sqlite3`, `data/intake/objects/<sha256>.txt` |
| indexer | 8102 | rulesets, builds, indexes, mappings | `data/indexer/indexer.sqlite3`, `data/indexer/mappings/<index_id>.map.json` |
| query | 8100 | reports, citations, UI | `data/query/query.sqlite3` |

The query service is also the single browser origin: it proxies intake and
indexer. Nothing is shared through memory or a common database — every
cross-service read is an HTTP call, so each can be restarted alone.

## Byte offsets vs. character positions

They are separate fields everywhere, never interchangeable:

- `char_length`, `char_start`, `char_end` count **Unicode code points**.
- `byte_length`, `byte_start`, `byte_end` count **UTF-8 bytes**.

`POST /api/revisions/{rev}/slice` accepts `unit: "char"` or `"byte"`. A byte
offset that lands inside a multi-byte character is rejected with
`byte_not_on_char_boundary` rather than silently rounding — the UI has a
"probe mid-character byte" button for exactly this. For `é 😀 中文` a two-character
read is `bytes [0, 6)`. Every citation stores both spans, and you can see the
byte span being wider than the char span in the report panel.

## Immutable inputs

- Source revisions are content-addressed: `src_` + sha256 prefix of the bytes.
  Re-importing identical bytes reuses the same revision; nothing is rewritten
  in place, so an old citation can always be resolved against its own bytes.
- Rulesets are immutable revisions too. The identity hashes **only the rule
  list** (`name` excluded); editing rules creates a new `rsr_…`.
- An index id is `idx_` + sha256(`source_revision_id | ruleset_revision_id |
  engine_version`). The same pair never produces a second, competing index.

## Cleaning preserves provenance

Rules (`normalize_newlines`, `strip_chars`, `trim_lines`, `collapse_spaces`,
`merge_paragraphs`) are applied atomically over the source characters, and each
clean code point keeps a provenance record: the source char span it came from,
or `synthetic` when a rule inserted it. Those records are compacted into
contiguous mapping segments plus a list of deleted source ranges.

- Mapping is monotonic and in-bounds (tested), so `locate` can turn any clean
  char range into source char **and** byte spans.
- Synthetic spans are why verification does not just slice source bytes: the
  quote's characters are rebuilt span by span, copying source text for copied
  spans and reproducing the quote's own characters for inserted ones, then
  hashed against the pinned hash.

## What protects a citation

A citation pins `report_id`, `index_id`, `ruleset_revision_id`,
`source_revision_id`, `source_sha256`, clean char span, source char span and
source byte span, plus the per-span provenance.

- **Reading is never re-location.** `GET /api/citations/{id}` returns stored
  offsets. If rules or the source change, the stored answer does not drift.
- **Comparison is explicit.** `POST /api/citations/{id}/compare` scans the
  target index's clean text and returns a status rather than a silent move:
  `unchanged`, `moved`, `same_offsets_different_clean`, `ambiguous`,
  `quote_missing`, `revision_differs`.
- **Re-anchoring is explicit and additive.** `POST .../reanchor` refuses with
  409 `cannot_reanchor` when the quote is missing or ambiguous; otherwise it
  inserts a **new** citation and keeps the original.
- **Interrupted builds publish nothing.** Builds stage the mapping, fsync, then
  `os.replace` and insert paragraphs/clean text in one transaction. A simulated
  or genuine interruption leaves only an `interrupted` job; retry is offered.
- **Verification catches tampering.** `GET .../verify` checks the revision hash
  and the rebuilt quote text at the pinned span, returning
  `status: pin_mismatch` (409) instead of trusting the offsets.

## Storage and read cost (measured, not estimated)

From `GET /api/storage-cost` on the demo data:

| what | value |
|---|---|
| source objects (6 revisions) | 610 B (one copy per content hash) |
| intake metadata db | 36 864 B |
| mapping files (20 indexes) | 20 702 B total |
| one 275-byte multibyte document, per index | ~1 267 B mapping = **4.6× source bytes**, 10 segments |
| indexer metadata db | 155 648 B |
| reports + citations db | 32 768 B for 9 reports / 15 citations (~2.2 KB per citation incl. SQLite overhead) |

**Strategy A — keep the mapping.** Cost is one mapping file per
(source revision, ruleset revision) pair, never shared: ~4.6× the source size
here, and it grows with the number of rules that split spans. In exchange,
any index projects back to its own source bytes and any citation is
verifiable.

**Strategy B — pin the revision.** Cost is a fixed handful of columns per
citation and no mapping reads on the report path: a report read is 1 indexed
SQLite read plus 1 for its citations. The price is that an old citation is not
automatically valid in a newer index — comparison costs one literal scan of
that index's clean text, and re-anchoring writes a new row.

Both are in use: mapping for provenance, pinning for meaning. Keeping only the
mapping would let a rebuild silently change what a citation displays; keeping
only the pin would lose byte-level provenance and tamper detection.

## Layout

```
app/common.py     shared HTTP/SQLite/text plumbing (stdlib only)
app/intake.py     documents, immutable revisions, byte/char slices, object store
app/cleaning.py   rules, canonical identity, atom cleaning, mapping, paragraphs
app/indexer.py    rulesets, atomic builds, indexes, locate/quote, storage cost
app/query.py      reports, pinned citations, compare/verify/re-anchor, UI façade
app/run.py        starts the three processes
app/static/       five-tab UI (sources, rules, indexes, reports, cost)
tests/test_offsets.py
```
