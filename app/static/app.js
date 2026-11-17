/* Offset Lab UI — talks only to the query service, which fans out to intake
   and the indexer. No LLM, no retrieval QA: this is a position-accounting demo. */
"use strict";

const state = {
  selectedRevision: null,
  selectedDocument: null,
  selectedIndex: null,
  selectedCitation: null,
  latestIndex: null,
  rulesets: [],
  ruleDraft: null,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const pretty = (value) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));

function flash(message, isError) {
  const node = $("flash");
  node.hidden = false;
  node.className = "flash" + (isError ? " error" : "");
  node.textContent = message;
  clearTimeout(flash._timer);
  flash._timer = setTimeout(() => { node.hidden = true; }, 6000);
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { raw: text }; }
  if (!response.ok) {
    const message = payload && payload.error ? `${payload.error.code}: ${payload.error.message}` : `HTTP ${response.status}`;
    const error = new Error(message);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* ---------------------------------------------------------------- tabs */
for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => {
    for (const other of document.querySelectorAll("#tabs button")) other.classList.toggle("active", other === button);
    for (const section of document.querySelectorAll(".tab")) section.classList.toggle("active", section.id === "tab-" + button.dataset.tab);
    if (button.dataset.tab === "cost") loadCost();
  });
}

/* ------------------------------------------------------------- sources */
function b64encodeUtf8(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function submitSource(text, filename, note) {
  const body = { filename, note, content_base64: b64encodeUtf8(text) };
  const result = await post("/api/documents", body);
  flash(`Stored ${result.revision.revision_id} (${result.revision.byte_length} bytes, ${result.revision.char_length} chars)`);
  await refreshDocuments();
  await selectRevision(result.revision.revision_id);
  return result;
}

$("src-submit").addEventListener("click", async () => {
  try {
    const file = $("src-file").files[0];
    let text = $("src-text").value;
    if (file) text = await file.text();
    if (!text) return flash("Provide a file or paste text", true);
    await submitSource(text, $("src-filename").value.trim() || "document.txt", $("src-note").value);
  } catch (error) { flash(error.message, true); }
});

$("src-example").addEventListener("click", async () => {
  const lines = [
    "﻿虚构档案 · CRLF 版本",
    "",
    "    第一行以四个半角空格开头，行尾还有空格。   ",
    "第二行含有全角空格：　　以及零宽字符：​‍隐藏。",
    "符号测试：é 😀 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 中文。",
    "",
    "结尾段落用于合并测试。",
  ];
  const text = lines.join("\r\n");
  $("src-text").value = text;
  $("src-filename").value = "虚构档案-CRLF-BOM.txt";
  try { await submitSource(text, "虚构档案-CRLF-BOM.txt", "CRLF + BOM sample"); }
  catch (error) { flash(error.message, true); }
});

$("src-refresh").addEventListener("click", () => refreshDocuments().catch((e) => flash(e.message, true)));

let documents = [];

async function refreshDocuments() {
  const data = await api("/api/documents");
  documents = data.documents;
  const list = $("doc-list");
  list.replaceChildren();
  if (!documents.length) {
    list.append(el("p", "hint", "No documents yet. Import one on the left."));
  }
  for (const doc of documents) {
    const item = el("div", "item" + (doc.doc_id === state.selectedDocument ? " selected" : ""));
    const title = el("div", "title");
    title.append(el("span", null, doc.filename), el("span", "badge", `${doc.revision_count} rev`));
    item.append(title, el("div", "meta", doc.doc_id));
    item.addEventListener("click", () => selectDocument(doc.doc_id));
    list.append(item);
  }
  const latest = documents.flatMap((doc) => doc.revisions).at(-1);
  if (latest && !state.selectedRevision) await selectRevision(latest.revision_id);
  fillBuildSource();
}

async function selectDocument(docId) {
  state.selectedDocument = docId;
  const data = await api(`/api/documents/${docId}`);
  const detail = $("revision-detail");
  detail.replaceChildren();
  detail.append(el("h3", null, data.document.filename + " · revisions"));
  for (const rev of data.revisions) {
    const item = el("div", "item" + (rev.revision_id === state.selectedRevision ? " selected" : ""));
    item.append(
      el("div", "title", `#${rev.revision_number} · ${rev.filename}`),
      el("div", "meta", `${rev.revision_id} · ${rev.byte_length} B / ${rev.char_length} chars · multibyte ${rev.multibyte_chars} · ${JSON.stringify(rev.line_endings)}`)
    );
    item.addEventListener("click", () => selectRevision(rev.revision_id));
    detail.append(item);
  }
  await refreshDocuments();
}

async function selectRevision(revisionId) {
  state.selectedRevision = revisionId;
  $("slice-rev").value = revisionId;
  $("build-source").value = revisionId;
  $("rep-index").value = "";
  const revision = await api(`/api/revisions/${revisionId}`);
  const detail = $("revision-detail");
  detail.replaceChildren();
  detail.append(el("h3", null, `Revision ${revision.revision_id}`));
  const table = el("table", "kv");
  const rows = [
    ["sha256", revision.sha256],
    ["bytes", revision.byte_length],
    ["characters", revision.char_length],
    ["multi-byte chars", revision.multibyte_chars],
    ["astral chars", revision.astral_chars],
    ["line endings", JSON.stringify(revision.line_endings)],
    ["BOM", revision.has_bom ? "yes" : "no"],
  ];
  for (const [key, value] of rows) {
    const tr = el("tr");
    tr.append(el("th", null, key), el("td", null, String(value)));
    table.append(tr);
  }
  detail.append(table);

  const indexes = await api(`/api/indexes?source_revision_id=${encodeURIComponent(revisionId)}`).catch(() => ({ indexes: [] }));
  if (indexes.indexes.length) {
    const near = latestIndex(indexes.indexes);
    detail.append(el("p", "hint", `Latest index for this revision: ${near.index_id}`));
    // Prefill the citation target from the revision being viewed, so the
    // reports tab always has a valid index without an extra manual step.
    $("rep-index").value = near.index_id;
    if (!state.selectedIndex) {
      state.selectedIndex = near.index_id;
      $("compare-index").value = near.index_id;
    }
  } else {
    detail.append(el("p", "hint", "No index built yet for this revision — build one in step 3."));
  }
  await refreshDocuments();
}

function latestIndex(indexes) {
  return [...indexes].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
}

function fillBuildSource() { /* kept for symmetry */ }

/* --------------------------------------------------------- byte vs char */
async function doSlice() {
  const revisionId = $("slice-rev").value.trim();
  if (!revisionId) return flash("Enter a revision id", true);
  try {
    const unit = $("slice-unit").value;
    const result = await post(`/api/revisions/${revisionId}/slice`, {
      unit,
      start: Number($("slice-start").value || 0),
      end: Number($("slice-end").value || 0),
    });
    $("slice-out").textContent = [
      `unit=${result.unit} requested=[${result.requested.start}, ${result.requested.end})`,
      `chars [${result.char_start}, ${result.char_end})  bytes [${result.byte_start}, ${result.byte_end})`,
      `char_length=${result.char_length}  byte_length=${result.byte_length}`,
      `text=${JSON.stringify(result.text)}`,
    ].join("\n");
  } catch (error) {
    $("slice-out").textContent = `ERROR ${error.message}\n\n${pretty(error.payload)}`;
  }
}
$("slice-go").addEventListener("click", doSlice);
$("slice-probe").addEventListener("click", async () => {
  try {
    const revisionId = $("slice-rev").value.trim();
    const revision = await api(`/api/revisions/${revisionId}`);
    const text = await (await fetch(`/api/revisions/${revisionId}/source`)).text();
    const encoder = new TextEncoder();
    let bytePos = 0;
    let charPos = 0;
    for (const ch of text) {
      const width = encoder.encode(ch).length;
      if (width > 1) { bytePos += 1; charPos += 0; break; }
      bytePos += width; charPos += 1;
    }
    $("slice-unit").value = "byte";
    $("slice-start").value = String(bytePos);
    $("slice-end").value = String(bytePos + 1);
    await doSlice();
  } catch (error) { flash(error.message, true); }
});

/* ---------------------------------------------------------------- rules */
$("rules-save").addEventListener("click", saveRules);
$("rules-add").addEventListener("click", () => {
  state.ruleDraft.push({ type: "collapse_spaces", chars: " \t", replacement: " " });
  renderRules();
});
$("rules-refresh").addEventListener("click", () => refreshRulesets().catch((e) => flash(e.message, true)));
$("rules-target").addEventListener("change", () => {
  const target = $("rules-target").value;
  if (!target) { $("rules-name").value = "默认清洗"; state.ruleDraft = defaultRules(); renderRules(); return; }
  // Use the revision list already fetched by refreshRulesets, so the editor
  // switches rulesets synchronously with no window to save a stale draft.
  const detail = (state.rulesetDetails || []).find((row) => row.ruleset_id === target);
  if (!detail) return flash("Reload the ruleset list and try again", true);
  const latest = (detail.revisions || [])[detail.revisions.length - 1];
  if (!latest) return flash("That ruleset has no revisions", true);
  $("rules-name").value = detail.name;
  state.ruleDraft = JSON.parse(JSON.stringify(latest.definition));
  renderRules();
});

function defaultRules() {
  return [
    { type: "normalize_newlines" },
    { type: "strip_chars", chars: "\u200b\u200c\u200d\ufeff" },
    { type: "trim_lines", chars: " \t" },
    { type: "collapse_spaces", chars: " \t", replacement: " " },
    { type: "merge_paragraphs" },
  ];
}

const RULE_FIELDS = {
  normalize_newlines: [],
  strip_chars: ["chars"],
  trim_lines: ["chars"],
  collapse_spaces: ["chars", "replacement"],
  merge_paragraphs: [],
};

function renderRules() {
  const box = $("rules-list");
  box.replaceChildren();
  state.ruleDraft.forEach((rule, index) => {
    const row = el("div", "rule");
    const type = el("select");
    for (const name of Object.keys(RULE_FIELDS)) {
      const option = el("option", null, name);
      option.value = name;
      option.selected = name === rule.type;
      type.append(option);
    }
    type.addEventListener("change", () => {
      rule.type = type.value;
      if (rule.type === "strip_chars") rule.chars = "\u200b\u200c\u200d\ufeff";
      if (rule.type === "trim_lines" || rule.type === "collapse_spaces") rule.chars = " \t";
      if (rule.type === "collapse_spaces") rule.replacement = " ";
      if (rule.type === "normalize_newlines" || rule.type === "merge_paragraphs") {
        delete rule.chars; delete rule.replacement;
      }
      renderRules();
    });
    row.append(type);
    const params = el("input");
    const fields = RULE_FIELDS[rule.type] || [];
    if (fields.includes("chars") && fields.includes("replacement")) {
      params.value = `chars=${JSON.stringify(rule.chars || " \t")} replacement=${JSON.stringify(rule.replacement || " ")}`;
      params.addEventListener("change", () => {
        rule.chars = " \t"; rule.replacement = " ";
      });
      params.title = "collapse_spaces uses chars=space+tab and a single space replacement";
    } else if (fields.includes("chars")) {
      params.value = JSON.stringify(rule.chars || "");
      params.addEventListener("change", () => {
        try {
          const parsed = JSON.parse(params.value);
          if (typeof parsed !== "string" || !parsed) throw new Error("chars must be a non-empty string");
          rule.chars = parsed;
        } catch (error) { flash(error.message, true); renderRules(); }
      });
    } else {
      params.value = "(no parameters)";
      params.disabled = true;
    }
    row.append(params);
    const del = el("button", "del", "×");
    del.title = "remove rule";
    del.addEventListener("click", () => { state.ruleDraft.splice(index, 1); renderRules(); });
    row.append(del);
    box.append(row);
  });
}

async function saveRules() {
  try {
    const name = $("rules-name").value.trim();
    const target = $("rules-target").value;
    let result;
    if (target) {
      result = await post(`/api/rulesets/${target}/revisions`, { name, rules: state.ruleDraft });
    } else {
      result = await post("/api/rulesets", { name, rules: state.ruleDraft });
    }
    // Refresh first: it repopulates the build select, and we must not let that
    // overwrite a choice the user makes after seeing the result.
    await refreshRulesets();
    $("build-ruleset").value = result.revision.ruleset_revision_id;
    $("rules-out").textContent = pretty({
      created: result.created !== false,
      ruleset_revision_id: result.revision.ruleset_revision_id,
      revision_number: result.revision.revision_number,
      definition_sha256: result.revision.definition_sha256,
      message: result.message || "Saved as a new immutable ruleset revision",
    });
    flash(`Ruleset revision ${result.revision.ruleset_revision_id} ready`);
  } catch (error) { flash(error.message, true); $("rules-out").textContent = error.message; }
}

async function refreshRulesets() {
  const data = await api("/api/rulesets");
  state.rulesets = data.rulesets;
  // Fetch each ruleset's revisions once and reuse them for the selects and the list.
  const details = [];
  for (const ruleset of data.rulesets) {
    details.push(await api(`/api/rulesets/${ruleset.ruleset_id}`));
  }
  state.rulesetDetails = details;

  const target = $("rules-target");
  const keep = target.value;
  target.replaceChildren(el("option", null, "— new ruleset —"));
  target.firstChild.value = "";
  for (const ruleset of data.rulesets) {
    const option = el("option", null, `${ruleset.name} (${ruleset.revision_count} rev)`);
    option.value = ruleset.ruleset_id;
    target.append(option);
  }
  target.value = keep;

  const select = $("build-ruleset");
  const keepRevision = select.value;
  select.replaceChildren();
  let anyOption = false;
  for (const detail of details) {
    for (const rev of detail.revisions || []) {
      const option = el("option", null, `${detail.name} · r${rev.revision_number} · ${rev.ruleset_revision_id}`);
      option.value = rev.ruleset_revision_id;
      select.append(option);
      anyOption = true;
    }
  }
  if (!anyOption) {
    const option = el("option", null, "— save a ruleset first —");
    option.value = "";
    select.append(option);
  }
  if (keepRevision) select.value = keepRevision;

  const list = $("rulesets");
  list.replaceChildren();
  for (const detail of details) {
    const card = el("div", "item");
    card.append(el("div", "title", `${detail.name} · ${detail.revision_count} revision(s)`));
    for (const rev of detail.revisions) {
      const row = el("div", "meta", `r${rev.revision_number}  ${rev.ruleset_revision_id}  ${rev.definition_sha256.slice(0, 12)}…`);
      row.append(el("div", null, rev.definition.map((rule) => rule.type).join(" → ")));
      card.append(row);
    }
    list.append(card);
  }
}

/* -------------------------------------------------------------- indexes */
$("build-go").addEventListener("click", async () => {
  try {
    clearInterval(jobTimer); jobTimer = null;
    const source = $("build-source").value.trim();
    const ruleset = $("build-ruleset").value;
    if (!source || !ruleset) return flash("Choose source revision and ruleset revision", true);
    const result = await post("/api/builds", {
      source_revision_id: source,
      ruleset_revision_id: ruleset,
    });
    if (result.index) {
      await refreshJobs();
      await selectIndex(result.index.index_id);
      $("build-out").textContent = pretty({
        status: "already_published",
        index_id: result.index.index_id,
        message: result.message,
        index: {
          paragraph_count: result.index.paragraph_count,
          clean_char_length: result.index.clean_char_length,
          clean_sha256: result.index.clean_sha256,
        },
      });
      await refreshIndexes();
      return;
    }
    if (result.job) {
      $("build-out").textContent = pretty({
        job_id: result.job.job_id,
        status: result.job.status,
        stage: result.job.stage,
        message: result.message || result.job.message,
      });
      await refreshJobs();
      watchJob(result.job.job_id);
      return;
    }
    $("build-out").textContent = pretty(result);
  } catch (error) { flash(error.message, true); $("build-out").textContent = error.message; }
});

$("build-interrupt").addEventListener("click", async () => {
  try {
    clearInterval(jobTimer); jobTimer = null;
    const source = $("build-source").value.trim();
    const ruleset = $("build-ruleset").value;
    if (!source || !ruleset) return flash("Choose source revision and ruleset revision", true);
    const result = await post("/api/builds", {
      source_revision_id: source,
      ruleset_revision_id: ruleset,
      simulate: "interrupt",
    });
    if (!result.job) {
      $("build-out").textContent = pretty(result);
      return flash("This source + ruleset pair is already indexed; pick a different ruleset revision to model a rebuild", true);
    }
    $("build-out").textContent = pretty({
      job_id: result.job.job_id,
      status: result.job.status,
      stage: result.job.stage,
      message: result.job.message,
      created: result.created,
    });
    flash("Interrupted build recorded; no partial index should be visible");
    await refreshJobs();
    await refreshIndexes();
  } catch (error) { flash(error.message, true); }
});

let jobTimer = null;
function watchJob(jobId) {
  if (!jobId) return;
  clearInterval(jobTimer);
  jobTimer = setInterval(async () => {
    try {
      const job = await api(`/api/builds/${jobId}`);
      $("build-out").textContent = pretty({
        job_id: job.job_id, status: job.status, stage: job.stage,
        progress: job.progress, message: job.message, error: job.error_message,
        index: job.index
          ? {
              index_id: job.index.index_id,
              paragraph_count: job.index.paragraph_count,
              clean_char_length: job.index.clean_char_length,
              clean_sha256: job.index.clean_sha256,
            }
          : null,
      });
      await refreshJobs();
      if (["succeeded", "failed", "interrupted", "cancelled"].includes(job.status)) {
        clearInterval(jobTimer); jobTimer = null;
        await refreshJobs();
        await refreshIndexes();
        if (job.status === "succeeded" && job.index) await selectIndex(job.index.index_id);
      }
    } catch (error) { clearInterval(jobTimer); jobTimer = null; }
  }, 500);
}

async function refreshJobs() {
  const data = await api("/api/builds?limit=30");
  const box = $("jobs");
  box.replaceChildren();
  if (!data.jobs.length) box.append(el("p", "hint", "No builds yet."));
  for (const job of data.jobs) {
    const item = el("div", "item");
    const badgeClass = { succeeded: "ok", published: "ok", running: "warn", queued: "", failed: "bad", interrupted: "bad" }[job.status] || "";
    const title = el("div", "title", job.job_id);
    title.append(el("span", `badge ${badgeClass}`, job.status));
    item.append(
      title,
      el("div", "meta", `${job.stage} · ${Math.round(job.progress * 100)}% · attempts ${job.attempts}`),
      el("div", "meta", job.message || job.error_message || "")
    );
    if (["failed", "interrupted"].includes(job.status)) {
      const retry = el("button", "ghost small", "Retry build");
      retry.addEventListener("click", async (event) => {
        event.stopPropagation();
        try {
          const result = await post(`/api/builds/${job.job_id}/retry`, {});
          flash("Retry started");
          await refreshJobs();
          watchJob(result.job.job_id);
        } catch (error) { flash(error.message, true); }
      });
      item.append(retry);
    }
    box.append(item);
  }
}

$("build-refresh").addEventListener("click", () => refreshJobs().catch((e) => flash(e.message, true)));
$("index-refresh").addEventListener("click", () => refreshIndexes().catch((e) => flash(e.message, true)));

async function refreshIndexes() {
  const data = await api("/api/indexes");
  const box = $("indexes");
  box.replaceChildren();
  if (!data.indexes.length) box.append(el("p", "hint", "No indexes yet."));
  for (const index of data.indexes) {
    const item = el("div", "item" + (index.index_id === state.selectedIndex ? " selected" : ""));
    item.append(
      el("div", "title", index.index_id),
      el("div", "meta", `source ${index.source_revision_id} · ruleset ${index.ruleset_revision_id}`),
      el("div", "meta", `${index.paragraph_count} paragraphs · ${index.clean_char_length} clean chars · map ${index.mapping_bytes} B`)
    );
    item.addEventListener("click", () => selectIndex(index.index_id));
    box.append(item);
  }
}

async function selectIndex(indexId) {
  state.selectedIndex = indexId;
  $("compare-index").value = indexId;
  $("rep-index").value = indexId;
  const index = await api(`/api/indexes/${indexId}`);
  const detail = $("index-detail");
  detail.replaceChildren();
  detail.append(el("h3", null, `Index ${indexId}`));
  const table = el("table", "kv");
  const rows = [
    ["source revision", index.source_revision_id],
    ["ruleset revision", index.ruleset_revision_id],
    ["source sha256", index.source_sha256],
    ["clean sha256", index.clean_sha256],
    ["source bytes / chars", `${index.source_byte_length} / ${index.source_char_length}`],
    ["clean bytes / chars", `${index.clean_byte_length} / ${index.clean_char_length}`],
    ["paragraphs", index.paragraph_count],
    ["mapping segments", index.mapping_segments],
    ["mapping bytes", index.mapping_bytes],
    ["deleted ranges", (index.deleted_ranges || []).length],
    ["build attempts", index.build_attempts],
  ];
  for (const [key, value] of rows) {
    const tr = el("tr");
    tr.append(el("th", null, key), el("td", null, String(value)));
    table.append(tr);
  }
  detail.append(table);
  const stats = el("pre", "out", pretty(index.rule_stats));
  detail.append(stats);
  await refreshIndexes();
  await loadParagraphs(indexId);
}

$("compare-load").addEventListener("click", () => loadParagraphs($("compare-index").value.trim()));

async function loadParagraphs(indexId) {
  if (!indexId) return;
  try {
    const data = await api(`/api/indexes/${indexId}/paragraphs?limit=1000`);
    const box = $("paragraphs");
    box.replaceChildren();
    for (const para of data.paragraphs) {
      const card = el("div", "para");
      card.append(el("div", "offsets",
        `¶${para.ordinal} · clean chars [${para.clean_char_start}, ${para.clean_char_end}) · ` +
        `clean bytes [${para.clean_byte_start}, ${para.clean_byte_end}) · ` +
        `source chars [${para.source_char_start}, ${para.source_char_end})`));
      card.append(el("div", "text", para.text));
      box.append(card);
    }
  } catch (error) { flash(error.message, true); }
}

/* -------------------------------------------------------------- reports */
$("rep-autofill").addEventListener("click", async () => {
  const indexId = $("rep-index").value.trim();
  if (!indexId) return flash("Enter an index id", true);
  try {
    const data = await api(`/api/indexes/${indexId}/paragraphs?limit=1`);
    if (!data.paragraphs.length) return flash("Index has no paragraphs", true);
    const text = data.paragraphs[0].text;
    $("rep-quote").value = text.length > 80 ? text.slice(0, 80) : text;
  } catch (error) { flash(error.message, true); }
});

$("rep-create").addEventListener("click", async () => {
  try {
    const body = {
      title: $("rep-title").value.trim(),
      index_id: $("rep-index").value.trim(),
      annotations: [{ quote: $("rep-quote").value, note: $("rep-note").value }],
    };
    const report = await post("/api/reports", body);
    $("rep-out").textContent = pretty({
      report_id: report.report.report_id,
      index_id: report.report.index_id,
      citations: report.citations.map((c) => ({
        citation_id: c.citation_id,
        status: c.status,
        clean: [c.clean_char_start, c.clean_char_end],
        source_chars: [c.source_char_start, c.source_char_end],
        source_bytes: [c.source_byte_start, c.source_byte_end],
      })),
    });
    flash("Report stored with pinned citations");
    await refreshReports();
    if (report.citations[0]) await selectCitation(report.citations[0].citation_id);
    await refreshReports();
  } catch (error) { flash(error.message, true); $("rep-out").textContent = error.message; }
});

$("rep-refresh").addEventListener("click", () => refreshReports().catch((e) => flash(e.message, true)));

async function refreshReports() {
  const data = await api("/api/reports");
  const box = $("reports");
  box.replaceChildren();
  if (!data.reports.length) {
    box.append(el("p", "hint", "No reports yet. Create one on the left."));
  }
  for (const report of data.reports) {
    const item = el("div", "item");
    item.append(
      el("div", "title", report.title),
      el("div", "meta", `${report.report_id} · ${report.citation_count} citation(s)`),
      el("div", "meta", `pinned source ${report.source_revision_id} · index ${report.index_id}`)
    );
    item.dataset.reportId = report.report_id;
    item.addEventListener("click", () => openReport(report.report_id));
    box.append(item);
  }
  if (!refreshReports._bound) {
    refreshReports._bound = true;
  }
}

async function openReport(reportId) {
  const data = await api(`/api/reports/${reportId}`);
  const detail = $("report-detail");
  detail.replaceChildren();
  detail.append(el("h3", null, data.report.title));
  detail.append(el("p", "hint",
    `Pinned to source ${data.report.source_revision_id} · index ${data.report.index_id} · ruleset ${data.report.ruleset_revision_id}`));
  for (const citation of data.citations) {
    const item = el("div", "item");
    const badge = { resolved: "ok", ambiguous: "warn", unresolved: "bad", source_mismatch: "bad" }[citation.status] || "";
    const title = el("div", "title");
    title.append(el("span", "citation-click", `“${citation.quote.slice(0, 40)}”`), el("span", `badge ${badge}`, citation.status));
    item.append(title);
    item.append(el("div", "meta",
      `clean [${citation.clean_char_start}, ${citation.clean_char_end}) · ` +
      `source chars [${citation.source_char_start}, ${citation.source_char_end}) · ` +
      `source bytes [${citation.source_byte_start}, ${citation.source_byte_end})`));
    item.addEventListener("click", () => selectCitation(citation.citation_id));
    detail.append(item);
  }
}

async function selectCitation(citationId) {
  state.selectedCitation = citationId;
  try {
    const citation = await api(`/api/citations/${citationId}`);
    $("citation-target").value = "";
    $("citation-out").textContent = pretty(citation);
    if (citation.source_revision_id) {
      const siblings = await api(`/api/indexes?source_revision_id=${encodeURIComponent(citation.source_revision_id)}`);
      const other = siblings.indexes.find((row) => row.index_id !== citation.index_id);
      if (other) $("citation-target").value = other.index_id;
    }
    $("citation-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) { flash(error.message, true); }
}

$("citation-verify").addEventListener("click", async () => {
  if (!state.selectedCitation) return flash("Select a citation first", true);
  try {
    const result = await fetch(`/api/citations/${state.selectedCitation}/verify`).then(async (response) => ({
      status: response.status, body: await response.json(),
    }));
    $("citation-out").textContent = pretty({
      http_status: result.status,
      verified: result.body.verified,
      status: result.body.status,
      checks: result.body.checks,
      pinned_sha256: result.body.pinned_sha256,
      server_sha256: result.body.server_sha256,
      source_at_pinned_span: result.body.source_at_pinned_span,
      message: result.body.message,
    });
  } catch (error) { flash(error.message, true); }
});

$("citation-context").addEventListener("click", async () => {
  if (!state.selectedCitation) return flash("Select a citation first", true);
  try {
    const result = await api(`/api/citations/${state.selectedCitation}/context?pad=60`);
    $("citation-out").textContent = pretty({
      pinned_span: result.pinned_span,
      slice_chars: [result.slice.char_start, result.slice.char_end],
      slice_bytes: [result.slice.byte_start, result.slice.byte_end],
      text: result.slice.text,
    });
  } catch (error) { flash(error.message, true); }
});

$("citation-compare").addEventListener("click", async () => {
  if (!state.selectedCitation) return flash("Select a citation first", true);
  try {
    const target = $("citation-target").value.trim();
    const result = await post(`/api/citations/${state.selectedCitation}/compare`, target ? { index_id: target } : {});
    $("citation-out").textContent = pretty(result);
  } catch (error) { flash(error.message, true); $("citation-out").textContent = pretty(error.payload || error.message); }
});

$("citation-reanchor").addEventListener("click", async () => {
  if (!state.selectedCitation) return flash("Select a citation first", true);
  const target = $("citation-target").value.trim();
  if (!target) return flash("Enter a target index id", true);
  try {
    const result = await post(`/api/citations/${state.selectedCitation}/reanchor`, { index_id: target });
    $("citation-out").textContent = pretty(result);
    flash("New citation created; original kept for audit");
    await refreshReports();
  } catch (error) { flash(error.message, true); $("citation-out").textContent = pretty(error.payload || error.message); }
});

/* ----------------------------------------------------------------- cost */
$("cost-refresh").addEventListener("click", () => loadCost().catch((e) => flash(e.message, true)));

async function loadCost() {
  const data = await api("/api/storage-cost");
  const box = $("cost");
  box.replaceChildren();
  const stats = [
    ["source objects", `${data.intake.source_object_bytes} B`],
    ["source metadata db", `${data.intake.metadata_db_bytes} B`],
    ["index mapping files", `${data.indexer.mapping_file_bytes} B`],
    ["index metadata db", `${data.indexer.metadata_db_bytes} B`],
    ["report/citation db", `${data.reports.db_bytes} B`],
    ["total", `${data.total_bytes} B`],
  ];
  for (const [key, value] of stats) {
    const card = el("div", "stat");
    card.append(el("div", "k", key), el("div", "v", value));
    box.append(card);
  }
  for (const index of data.indexer.indexes) {
    const card = el("div", "stat");
    card.append(
      el("div", "k", index.index_id),
      el("div", "v", `${index.mapping_bytes_per_source_byte}×`),
      el("div", "meta", `${index.mapping_file_bytes} B mapping for ${index.source_byte_length} B source · ${index.mapping_segments} segments`)
    );
    box.append(card);
  }
  const readCost = el("pre", "out", pretty({ read_cost: data.read_cost, tradeoff: data.tradeoff }));
  box.append(readCost);
}

/* ----------------------------------------------------------------- boot */
(async function boot() {
  state.ruleDraft = defaultRules();
  renderRules();
  try {
    await refreshDocuments();
    await refreshRulesets();
    await refreshIndexes();
    await refreshJobs();
    await refreshReports();
    if (!state.selectedIndex) {
      const all = await api("/api/indexes");
      if (all.indexes.length) await selectIndex(latestIndex(all.indexes).index_id);
    }
  } catch (error) { flash(error.message, true); }
})();
