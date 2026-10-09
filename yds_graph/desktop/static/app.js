const state = { threadId: null, outboxPath: null };

function $(id) {
  return document.getElementById(id);
}

function clear(node) {
  node.replaceChildren();
}

function el(tag, props, kids) {
  const node = document.createElement(tag);
  Object.entries(props || {}).forEach(function (pair) {
    const key = pair[0];
    const value = pair[1];
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value == null ? "" : String(value);
    else node.setAttribute(key, value);
  });
  (kids || []).forEach(function (kid) {
    node.append(kid);
  });
  return node;
}

function setBanner(kind, message) {
  const box = $("banner");
  if (!message) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.className = "banner " + kind;
  box.textContent = message;
}

function showView(name) {
  document.querySelectorAll(".view").forEach(function (view) {
    view.hidden = view.dataset.view !== name;
  });
  document.querySelectorAll("nav button").forEach(function (tab) {
    tab.setAttribute("aria-current", tab.dataset.view === name ? "page" : "false");
  });
}

async function api(path, body) {
  const options = {};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error("This page could not reach the app on this computer.");
  }
  const data = await response.json().catch(function () { return {}; });
  if (!response.ok) {
    throw new Error(data.error || "The request failed.");
  }
  return data;
}

function modeLabel(mode) {
  return mode === "anthropic" ? "Anthropic" : "Offline stub";
}

async function loadStatus() {
  const info = await api("/api/settings");
  $("mode").textContent = modeLabel(info.effective_mode);
  $("version").textContent = info.version || "";
  $("storage-label").textContent = info.storage_label || "";
  $("data-dir").textContent = info.data_dir || "";
  const picked = document.querySelector('input[name="mode"][value="' + info.mode + '"]');
  if (picked) picked.checked = true;
  return info;
}

function summaryLine(item) {
  const bits = [];
  if (item.source_file) bits.push(item.source_file);
  if (item.coach) bits.push("label " + item.coach);
  const range = item.provenance && item.provenance.date_range;
  if (range && range.length) bits.push(range.join(" to "));
  if (item.kind === "message_review") bits.push("draft message");
  return bits.join(", ");
}

function renderPending(items) {
  const list = $("pending-list");
  clear(list);
  $("pending-empty").hidden = items.length !== 0;
  items.forEach(function (item) {
    const open = el("button", { type: "button", text: "Open" });
    open.addEventListener("click", function () { openReview(item.thread_id); });
    const card = el("article", { class: "card" }, [
      el("h3", { text: item.source_file || item.kind || "Waiting" }),
      el("p", { text: item.paused_because }),
      el("p", { class: "meta", text: summaryLine(item) }),
      open,
    ]);
    list.append(card);
  });
}

async function loadPending() {
  const items = await api("/api/pending");
  renderPending(items);
}

function addNotes(parent, title, text) {
  parent.append(el("h3", { text: title }));
  parent.append(el("p", { class: "note", text: text || "" }));
}

function renderReview(item) {
  setBanner("", "");
  state.threadId = item.thread_id;
  state.outboxPath = null;
  $("result").hidden = true;
  $("decision").hidden = false;
  $("approve").disabled = false;
  $("reject").disabled = false;
  $("reject-reason").value = "";
  $("open-pdf").hidden = true;
  if (item.kind === "message_review") {
    $("review-title").textContent = "Draft to read";
  } else {
    $("review-title").textContent = item.source_file || "Report";
  }
  $("review-reason").hidden = false;
  $("review-reason").textContent = item.paused_because || "";
  $("review-meta").textContent = summaryLine(item);
  const body = $("review-body");
  clear(body);
  const notes = item.draft_notes || {};
  Object.keys(notes).sort().forEach(function (pitcher) {
    addNotes(body, pitcher, notes[pitcher]);
  });
  if (item.answer) addNotes(body, "Answer", item.answer);
  (item.drafts || []).forEach(function (draft, index) {
    addNotes(body, "Draft " + (index + 1), draft);
  });
  if (item.note) addNotes(body, "Note", item.note);
  showView("review");
}

async function openReview(threadId) {
  setBanner("", "");
  try {
    const item = await api("/api/runs/" + encodeURIComponent(threadId));
    renderReview(item);
  } catch (err) {
    setBanner("bad", err.message);
  }
}

function showResult(data) {
  $("decision").hidden = true;
  $("review-reason").hidden = true;
  $("result").hidden = false;
  $("result-message").textContent = data.message || "Done.";
  const path = data.outbox_path || "";
  $("result-path").textContent = path;
  state.outboxPath = path;
  $("open-pdf").hidden = !path;
  if (path) setBanner("good", "The PDF was saved.");
  else setBanner("good", data.message || "Recorded.");
}

async function onApprove() {
  if (!state.threadId) return;
  $("approve").disabled = true;
  $("reject").disabled = true;
  setBanner("info", "Saving...");
  try {
    const data = await api("/api/runs/" + encodeURIComponent(state.threadId) + "/approve", {});
    showResult(data);
    await loadPending();
  } catch (err) {
    setBanner("bad", err.message);
    $("approve").disabled = false;
    $("reject").disabled = false;
  }
}

async function onReject() {
  if (!state.threadId) return;
  const reason = $("reject-reason").value.trim();
  if (!reason) {
    setBanner("bad", "Add a reason before rejecting.");
    return;
  }
  $("approve").disabled = true;
  $("reject").disabled = true;
  setBanner("info", "Holding the report...");
  try {
    const data = await api("/api/runs/" + encodeURIComponent(state.threadId) + "/reject", { reason: reason });
    showResult(data);
    await loadPending();
  } catch (err) {
    setBanner("bad", err.message);
    $("approve").disabled = false;
    $("reject").disabled = false;
  }
}

async function onOpenPdf() {
  if (!state.outboxPath) return;
  try {
    const data = await api("/api/open", { path: state.outboxPath });
    if (!data.opened) setBanner("info", data.message || "The PDF is saved at the path below.");
  } catch (err) {
    setBanner("bad", err.message);
  }
}

function chosenMode() {
  const picked = document.querySelector('input[name="mode"]:checked');
  return picked ? picked.value : "stub";
}

function fileToPayload(file) {
  return new Promise(function (resolve, reject) {
    const reader = new FileReader();
    reader.onload = function () {
      const text = String(reader.result || "");
      const comma = text.indexOf(",");
      resolve({ filename: file.name, content_base64: comma >= 0 ? text.slice(comma + 1) : text });
    };
    reader.onerror = function () { reject(new Error("That file could not be read.")); };
    reader.readAsDataURL(file);
  });
}

function renderHold(parent, data) {
  clear(parent);
  parent.append(el("div", { class: "callout" }, [
    el("p", { text: data.message || "This file was held. Nothing was delivered." }),
  ]));
  (data.coach_notes || []).forEach(function (note) {
    parent.append(el("p", { text: note }));
  });
  if (data.hold_note) parent.append(el("pre", { class: "note", text: data.hold_note }));
}

async function afterStart(data) {
  const box = $("start-result");
  if (data.status === "paused" && data.review) {
    clear(box);
    renderReview(data.review);
    return;
  }
  renderHold(box, data);
  setBanner("info", data.message || "This file was held. Nothing was delivered.");
}

async function startSample() {
  $("start-sample").disabled = true;
  setBanner("info", "Running the sample file...");
  try {
    const data = await api("/api/runs", { use_sample: true, coach: $("coach").value });
    await afterStart(data);
    await loadPending();
  } catch (err) {
    setBanner("bad", err.message);
  } finally {
    $("start-sample").disabled = false;
  }
}

async function startFile() {
  const input = $("csv");
  const file = input.files && input.files[0];
  if (!file) {
    setBanner("bad", "Choose a CSV file first, or use the sample file.");
    return;
  }
  $("start-file").disabled = true;
  setBanner("info", "Running that file...");
  try {
    const payload = await fileToPayload(file);
    payload.coach = $("coach").value;
    const data = await api("/api/runs", payload);
    await afterStart(data);
    await loadPending();
  } catch (err) {
    setBanner("bad", err.message);
  } finally {
    $("start-file").disabled = false;
  }
}

function renderAudit(rows) {
  const box = $("audit-table");
  clear(box);
  if (!rows.length) {
    box.append(el("p", { class: "empty", text: "No audit rows yet." }));
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("tr");
  ["When", "Kind", "Outcome", "Label", "Source", "Version", "Reason"].forEach(function (name) {
    head.append(el("th", { text: name }));
  });
  table.append(head);
  rows.forEach(function (row) {
    const tr = document.createElement("tr");
    [row.run_at, row.kind, row.outcome, row.coach, row.source_file, row.pipeline_version, row.reason].forEach(function (value) {
      tr.append(el("td", { text: value || "" }));
    });
    table.append(tr);
  });
  box.append(table);
}

async function loadAudit() {
  const rows = await api("/api/audit?limit=50");
  renderAudit(rows);
}

async function ask(path, fieldId, outputId) {
  const question = $(fieldId).value.trim();
  if (!question) {
    setBanner("bad", "Type a question first.");
    return;
  }
  setBanner("info", "Working...");
  try {
    const data = await api(path, { question: question });
    const box = $(outputId);
    clear(box);
    box.append(el("p", { class: "note", text: data.answer || data.message || "" }));
    if (data.status === "paused" && data.review) {
      const open = el("button", { type: "button", class: "primary", text: "Read it in Waiting" });
      open.addEventListener("click", function () { renderReview(data.review); });
      box.append(open);
      await loadPending();
    }
    if (data.passages && data.passages.length) {
      box.append(el("p", { class: "meta", text: "Passages: " + data.passages.join(", ") }));
    }
    setBanner("", "");
  } catch (err) {
    setBanner("bad", err.message);
  }
}

async function saveSettings(clearKey) {
  const payload = { mode: chosenMode(), clear_key: clearKey };
  const typed = $("api-key").value;
  if (!clearKey && typed.trim()) payload.api_key = typed;
  try {
    const info = await api("/api/settings", payload);
    $("api-key").value = "";
    $("mode").textContent = modeLabel(info.effective_mode);
    $("storage-label").textContent = info.storage_label || "";
    const picked = document.querySelector('input[name="mode"][value="' + info.mode + '"]');
    if (picked) picked.checked = true;
    setBanner("good", clearKey ? "The saved key was removed. The app is on the offline stub." : "Settings saved.");
  } catch (err) {
    setBanner("bad", err.message);
  }
}

document.querySelectorAll("nav button").forEach(function (tab) {
  tab.addEventListener("click", function () {
    setBanner("", "");
    const name = tab.dataset.view;
    showView(name);
    if (name === "pending") loadPending().catch(function (err) { setBanner("bad", err.message); });
    if (name === "audit") loadAudit().catch(function (err) { setBanner("bad", err.message); });
    if (name === "settings") loadStatus().catch(function (err) { setBanner("bad", err.message); });
  });
});

$("back").addEventListener("click", function () {
  setBanner("", "");
  showView("pending");
  loadPending().catch(function (err) { setBanner("bad", err.message); });
});
$("approve").addEventListener("click", onApprove);
$("reject").addEventListener("click", onReject);
$("open-pdf").addEventListener("click", onOpenPdf);
$("start-sample").addEventListener("click", startSample);
$("start-file").addEventListener("click", startFile);
$("refresh-audit").addEventListener("click", function () {
  loadAudit().catch(function (err) { setBanner("bad", err.message); });
});
$("ask-go").addEventListener("click", function () { ask("/api/ask", "ask-question", "ask-answer"); });
$("library-go").addEventListener("click", function () { ask("/api/lookup", "library-question", "library-answer"); });
$("save-settings").addEventListener("click", function () { saveSettings(false); });
$("clear-key").addEventListener("click", function () { saveSettings(true); });

loadStatus()
  .then(loadPending)
  .catch(function (err) { setBanner("bad", err.message); });
