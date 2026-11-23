/* Keepstack single-page client. Vanilla JS, no build step. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const state = {
  token: localStorage.getItem("keepstack_token"), user: null,
  q: "", media: null, includeTags: [], excludeTags: [],
  sort: "newest", collection: null, collectionName: "",
  page: 1, pageSize: 24, total: 0,
};

// ---------- API helpers ----------
function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  return headers;
}

async function errorText(res) {
  const text = await res.text();
  try {
    const body = JSON.parse(text);
    if (body && body.detail) {
      return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch { /* not JSON */ }
  return text || res.statusText || ("HTTP " + res.status);
}

async function api(path, opts = {}) {
  const headers = authHeaders(opts.headers || {});
  const init = { ...opts, headers };
  delete init.json;
  if (opts.json) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
    // Every JSON call in this client is a write; without an explicit method,
    // fetch would send a GET with a body and the browser rejects it.
    init.method = opts.method || "POST";
  }
  const res = await fetch(path, init);
  if (res.status === 401) { logout(); throw new Error("Session expired. Please sign in again."); }
  if (!res.ok) throw new Error(await errorText(res));
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
}

function toast(msg, kind = "ok") {
  const t = $("#toast"); t.textContent = msg; t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 2600);
}
const fmtBytes = (b) => { if (!b) return "0 B"; const u = ["B","KB","MB","GB","TB"]; const i = Math.floor(Math.log(b)/Math.log(1024)); return (b/Math.pow(1024,i)).toFixed(i?1:0)+" "+u[i]; };
const esc = (s) => (s ?? "").toString().replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ---------- auth ----------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/auth/login", { json: { username: $("#login-user").value, password: $("#login-pass").value } });
    state.token = r.token; localStorage.setItem("keepstack_token", r.token);
    state.user = r.user; enterApp();
  } catch { $("#login-error").textContent = "Invalid credentials"; $("#login-error").classList.remove("hidden"); }
});
$("#btn-logout").addEventListener("click", logout);
function logout() {
  librarySeq++;  // ignore any in-flight list request
  localStorage.removeItem("keepstack_token"); state.token = null; state.user = null;
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
}

function enterApp() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  $("#user-label").textContent = `${state.user.username} (${state.user.role})`;
  const isAdmin = state.user.role === "admin";
  $$(".admin-only").forEach(el => el.classList.toggle("hidden", !isAdmin));
  $("#search").value = state.q;
  $("#sort").value = state.sort;
  $("#page-size").value = String(state.pageSize);
  refreshFilterOptions();
  renderTagPickers();
  renderActiveConditions();
  loadLibrary();
}

// ---------- navigation ----------
$$(".nav-item").forEach(btn => btn.addEventListener("click", () => {
  $$(".nav-item").forEach(b => b.classList.remove("active")); btn.classList.add("active");
  const view = btn.dataset.view;
  $$(".view").forEach(v => v.classList.add("hidden"));
  $("#view-" + (view === "library" ? "library" : view)).classList.remove("hidden");
  if (view === "library") loadLibrary();
  if (view === "collections") loadCollections();
  if (view === "dashboard") loadDashboard();
  if (view === "users") loadUsers();
  if (view === "audit") loadAudit();
}));

// ---------- library filters ----------
function hasActiveFilters() {
  return !!(state.q.trim() || state.media || state.collection ||
    state.includeTags.length || state.excludeTags.length);
}

function buildParams(paged) {
  const p = new URLSearchParams();
  const q = state.q.trim();
  if (q) p.set("q", q);
  if (state.media) p.set("media_type", state.media);
  if (state.collection) p.set("collection_id", String(state.collection));
  state.includeTags.forEach(t => p.append("tags", t));
  state.excludeTags.forEach(t => p.append("exclude_tags", t));
  p.set("sort", state.sort);
  if (paged) {
    p.set("limit", String(state.pageSize));
    p.set("offset", String((state.page - 1) * state.pageSize));
  }
  return p;
}

function updateResultSummary(busy = false, failed = false) {
  if (busy) { $("#result-sub").textContent = "Loading…"; return; }
  if (failed) { $("#result-sub").textContent = "Could not load results."; return; }
  const sortLabel = ($("#sort").selectedOptions[0]?.textContent || state.sort).toLowerCase();
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#result-sub").textContent =
    `${state.total} match${state.total === 1 ? "" : "es"} · sorted by ${sortLabel} · page ${state.page} of ${pages}`;
}

let librarySeq = 0;
async function loadLibrary() {
  const seq = ++librarySeq;
  if (state.page < 1) state.page = 1;
  $("#library-error").classList.add("hidden");
  updateResultSummary(true);
  try {
    const data = await api("/api/assets?" + buildParams(true).toString());
    if (seq !== librarySeq) return;  // a newer request won the race
    const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
    if (data.total === 0) {
      state.page = 1;
    } else if (state.page > pages) {
      // The match set shrank while we were on a later page (for example an
      // item was edited out); jump to the last valid page and reload once.
      state.page = pages;
      return loadLibrary();
    }
    state.total = data.total;
    renderFacets(data.facets);
    renderGrid(data.items);
    renderPager(data.total);
    renderActiveConditions();
    updateResultSummary();
  } catch (err) {
    if (seq !== librarySeq) return;
    if (String(err.message || "").includes("sign in")) return;
    showLibraryError(err.message || "Failed to load assets");
    $("#grid").innerHTML = "";
    $("#empty").classList.add("hidden");
    renderPager(0);
    updateResultSummary(false, true);
  }
}

function showLibraryError(message) {
  $("#library-error-text").textContent = message;
  $("#library-error").classList.remove("hidden");
}

function renderGrid(items) {
  const filtering = hasActiveFilters();
  $("#result-title").textContent = `${filtering ? "Results" : "Library"} (${state.total})`;
  $("#empty").textContent = filtering
    ? "No assets match the current filters."
    : "No assets yet. Click Upload to get started.";
  $("#empty").classList.toggle("hidden", items.length > 0);
  $("#grid").innerHTML = items.map(a => `
    <div class="card" data-uuid="${a.uuid}">
      <img class="thumb" loading="lazy" src="/api/assets/${a.uuid}/thumb" alt="${esc(a.alt_text || a.title)}">
      <div class="meta">
        <span class="title" title="${esc(a.title)}">${esc(a.title)}</span>
        <span class="sub"><span class="badge">${esc(a.media_type)}</span><span>${fmtBytes(a.size)}</span></span>
      </div>
    </div>`).join("");
  $$("#grid .card").forEach(c => c.addEventListener("click", () => openAsset(c.dataset.uuid)));
}

function renderFacets(facets) {
  const media = facets.media_types || [];
  $("#facet-media").innerHTML = media.map(m =>
    `<li class="${state.media === m.value ? "active" : ""}" data-media="${esc(m.value)}"><span>${esc(m.value)}</span><span class="count">${m.count}</span></li>`).join("");
  $("#facet-tags").innerHTML = (facets.tags || []).slice(0, 18).map(t =>
    `<li class="${state.includeTags.includes(t.value) ? "active" : ""}" data-tag="${esc(t.value)}"><span>${esc(t.value)}</span><span class="count">${t.count}</span></li>`).join("");

  // Keep the explicit media select in sync; it is also the accessible
  // alternative to the sidebar facet links.
  const filterMedia = $("#filter-media");
  const currentMedia = state.media || "";
  const mediaOptions = ['<option value="">All media types</option>'].concat(
    media.map(m => `<option value="${esc(m.value)}">${esc(m.value)} (${m.count})</option>`)
  );
  if (currentMedia && !media.some(m => m.value === currentMedia)) {
    mediaOptions.push(`<option value="${esc(currentMedia)}">${esc(currentMedia)}</option>`);
  }
  filterMedia.innerHTML = mediaOptions.join("");
  filterMedia.value = currentMedia;

  $$("#facet-media li").forEach(li => li.addEventListener("click", () => {
    state.media = state.media === li.dataset.media ? null : li.dataset.media;
    state.page = 1; renderActiveConditions(); loadLibrary();
  }));
  $$("#facet-tags li").forEach(li => li.addEventListener("click", () => {
    toggleTag("include", li.dataset.tag);
  }));
}

async function refreshFilterOptions() {
  try {
    const [tagData, collectionData] = await Promise.all([api("/api/tags"), api("/api/collections")]);
    const tags = (tagData.tags || []).map(t => t.name);
    $("#tag-options").innerHTML = tags.map(t => `<option value="${esc(t)}"></option>`).join("");

    const collections = collectionData.collections || [];
    const select = $("#filter-collection");
    const current = state.collection ? String(state.collection) : "";
    select.innerHTML = '<option value="">All collections</option>' + collections.map(c =>
      `<option value="${c.id}">${esc(c.name)} (${c.asset_count})</option>`).join("");
    const found = collections.find(c => String(c.id) === current);
    if (current && found) {
      select.value = current;
      state.collectionName = found.name;
    } else if (current) {
      state.collection = null; state.collectionName = ""; select.value = "";
    }
  } catch { /* options are a convenience; the library still works without them */ }
}

function renderTagPickers() {
  const chip = (name, kind, cls) => `<span class="filter-chip ${cls}">${esc(name)}<button type="button" class="chip-x" data-kind="${kind}" data-tag="${esc(name)}" aria-label="Remove ${esc(name)}">&times;</button></span>`;
  $("#include-chips").innerHTML = state.includeTags.map(t => chip(t, "include", "inc")).join("");
  $("#exclude-chips").innerHTML = state.excludeTags.map(t => chip(t, "exclude", "exc")).join("");

  const conflict = state.includeTags.some(t => state.excludeTags.includes(t));
  $("#filter-conflict").classList.toggle("hidden", !conflict);
}

function addTag(kind, raw) {
  const name = String(raw || "").trim().toLowerCase();
  if (!name) return;
  const list = kind === "include" ? state.includeTags : state.excludeTags;
  if (!list.includes(name)) list.push(name);
  renderTagPickers();
  state.page = 1;
  renderActiveConditions();
  loadLibrary();
}

function toggleTag(kind, name) {
  const list = kind === "include" ? state.includeTags : state.excludeTags;
  const idx = list.indexOf(name);
  if (idx >= 0) list.splice(idx, 1); else list.push(name);
  renderTagPickers();
  state.page = 1;
  renderActiveConditions();
  loadLibrary();
}

function removeTag(kind, name) {
  const list = kind === "include" ? state.includeTags : state.excludeTags;
  const idx = list.indexOf(name);
  if (idx >= 0) list.splice(idx, 1);
  renderTagPickers();
  state.page = 1;
  renderActiveConditions();
  loadLibrary();
}

function renderActiveConditions() {
  const items = [];
  const chip = (kind, label, value = "") =>
    `<span class="cond-chip ${kind}">${esc(label)}<button type="button" class="cond-x" data-kind="${kind}" data-value="${esc(value)}" aria-label="Remove condition">&times;</button></span>`;
  if (state.q.trim()) items.push(chip("q", `Keyword: ${state.q.trim()}`));
  if (state.collection) items.push(chip("collection", `Collection: ${state.collectionName || ("#" + state.collection)}`));
  state.includeTags.forEach(t => items.push(chip("include", `+ ${t}`, t)));
  state.excludeTags.forEach(t => items.push(chip("exclude", `− ${t}`, t)));
  if (state.media) items.push(chip("media", `Type: ${state.media}`));

  $("#active-conditions").innerHTML = items.length
    ? `<span class="cond-label">Active filters:</span>${items.join("")}`
    : '<span class="muted">No filters applied.</span>';
  $("#btn-clear-filters").classList.toggle("hidden", items.length === 0);
}

function clearFilters() {
  state.q = ""; state.media = null; state.collection = null; state.collectionName = "";
  state.includeTags = []; state.excludeTags = [];
  $("#search").value = "";
  $("#filter-collection").value = "";
  $("#filter-media").value = "";
  state.page = 1;
  renderTagPickers();
  renderActiveConditions();
  loadLibrary();
}

$("#active-conditions").addEventListener("click", (e) => {
  const btn = e.target.closest(".cond-x");
  if (!btn) return;
  const kind = btn.dataset.kind, value = btn.dataset.value;
  if (kind === "q") { state.q = ""; $("#search").value = ""; }
  else if (kind === "collection") { state.collection = null; state.collectionName = ""; $("#filter-collection").value = ""; }
  else if (kind === "media") { state.media = null; $("#filter-media").value = ""; }
  else if (kind === "include") { state.includeTags = state.includeTags.filter(t => t !== value); }
  else if (kind === "exclude") { state.excludeTags = state.excludeTags.filter(t => t !== value); }
  state.page = 1;
  renderTagPickers();
  renderActiveConditions();
  loadLibrary();
});
$("#include-chips").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip-x"); if (btn) removeTag("include", btn.dataset.tag);
});
$("#exclude-chips").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip-x"); if (btn) removeTag("exclude", btn.dataset.tag);
});

let searchTimer;
$("#search").addEventListener("input", (e) => {
  state.q = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.page = 1; renderActiveConditions(); loadLibrary(); }, 250);
});
$("#sort").addEventListener("change", (e) => {
  state.sort = e.target.value;
  state.page = 1; renderActiveConditions(); loadLibrary();
});
$("#page-size").addEventListener("change", (e) => {
  state.pageSize = Number(e.target.value) || 24;
  state.page = 1; loadLibrary();
});
$("#filter-collection").addEventListener("change", (e) => {
  const value = e.target.value;
  state.collection = value ? Number(value) : null;
  const option = e.target.selectedOptions[0];
  state.collectionName = state.collection ? (option?.textContent || "").replace(/\s*\(\d+\)$/, "") : "";
  state.page = 1; renderActiveConditions(); loadLibrary();
});
$("#filter-media").addEventListener("change", (e) => {
  state.media = e.target.value || null;
  state.page = 1; renderActiveConditions(); loadLibrary();
});
$("#btn-clear-filters").addEventListener("click", clearFilters);
$("#btn-retry").addEventListener("click", loadLibrary);

function wireTagInput(inputId, kind) {
  const input = $(inputId);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addTag(kind, input.value);
      input.value = "";
    } else if (e.key === "Backspace" && !input.value) {
      const list = kind === "include" ? state.includeTags : state.excludeTags;
      if (list.length) removeTag(kind, list[list.length - 1]);
    } else if (e.key === "Escape") {
      input.value = "";
    }
  });
}
wireTagInput("#include-input", "include");
wireTagInput("#exclude-input", "exclude");

function renderPager(total) {
  const pages = Math.max(1, Math.ceil(total / state.pageSize));
  $("#pager").classList.toggle("hidden", total === 0);
  $("#page-info").textContent = total
    ? `Page ${state.page} of ${pages} · ${total} match${total === 1 ? "" : "es"}`
    : "";
  $("#prev-page").disabled = state.page <= 1;
  $("#next-page").disabled = state.page >= pages;
}
$("#prev-page").addEventListener("click", () => {
  if (state.page > 1) { state.page--; loadLibrary(); }
});
$("#next-page").addEventListener("click", () => {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  if (state.page < pages) { state.page++; loadLibrary(); }
});

// ---------- CSV export ----------
$("#btn-export").addEventListener("click", async () => {
  const btn = $("#btn-export");
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "Exporting…";
  try {
    const res = await fetch("/api/assets/export.csv?" + buildParams(false).toString(), {
      headers: authHeaders(),
    });
    if (res.status === 401) { logout(); throw new Error("Session expired. Please sign in again."); }
    if (!res.ok) throw new Error(await errorText(res));
    const total = res.headers.get("X-Total-Count") ?? "?";
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = match ? match[1] : "assets.csv";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${total} row${String(total) === "1" ? "" : "s"}`);
  } catch (err) {
    toast("Export failed: " + (err.message || "unknown error"), "err");
  } finally {
    btn.disabled = false; btn.textContent = original;
  }
});

// ---------- asset detail drawer ----------
const drawer = $("#drawer");
$("#drawer-close").addEventListener("click", closeDrawer);
$(".drawer-backdrop").addEventListener("click", closeDrawer);
function closeDrawer() { drawer.classList.add("hidden"); document.body.classList.remove("modal-open"); }

async function openAsset(uuid) {
  drawer.classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("#drawer-body").innerHTML = '<p class="muted">Loading...</p>';
  const [d, meta, sim] = await Promise.all([
    api("/api/assets/" + uuid),
    api("/api/assets/" + uuid + "/metadata"),
    api("/api/assets/" + uuid + "/similar"),
  ]);
  const a = d.asset;
  const canEdit = ["admin","editor","contributor"].includes(state.user.role);
  const preview = a.media_type === "image"
    ? `<img class="detail-preview" src="/api/assets/${uuid}/preview" alt="${esc(a.alt_text||a.title)}">`
    : `<img class="detail-preview" src="/api/assets/${uuid}/thumb" alt="${esc(a.title)}">`;
  const dc = a.dublin_core || {};
  $("#drawer-body").innerHTML = `
    ${preview}
    <h2 class="detail-title">${esc(a.title)}</h2>
    <p class="muted">${a.media_type} &middot; ${a.ext?.toUpperCase()} &middot; ${fmtBytes(a.size)} ${a.width?`&middot; ${a.width}&times;${a.height}`:""}</p>
    <div class="btn-row">
      <a class="btn" href="/api/assets/${uuid}/file" download>Download</a>
      ${canEdit?`<button class="btn" id="d-share">Create share link</button>`:""}
      ${["admin","editor"].includes(state.user.role)?`<button class="btn btn-danger" id="d-delete">Delete</button>`:""}
    </div>

    ${canEdit ? `<div class="detail-section"><h4>Edit metadata</h4>
      <div class="edit-grid">
        <label>Title<input id="e-title" value="${esc(a.title)}"></label>
        <label>Description<textarea id="e-description" rows="2">${esc(a.description)}</textarea></label>
        <label>Alt text (accessibility)<input id="e-alt_text" value="${esc(a.alt_text)}"></label>
        <label>Creator<input id="e-dc_creator" value="${esc(a.dc_creator)}"></label>
        <label>Rights / license<input id="e-rights_statement" value="${esc(a.rights_statement)}"></label>
        <label>Retention until<input id="e-retention_until" type="date" value="${esc((a.retention_until||'').slice(0,10))}"></label>
      </div>
      <div class="btn-row"><button class="btn btn-primary" id="d-save">Save changes</button></div></div>` : ""}

    <div class="detail-section"><h4>Tags</h4>
      <div class="tag-row" id="tag-row">
        ${d.tags.map(t => `<span class="tag ${t.source==='ai'?'ai':''}">${esc(t.name)}${canEdit?`<span class="rm" data-tag="${esc(t.name)}">&times;</span>`:""}</span>`).join("")}
        ${canEdit?`<input class="tag-add" id="tag-add" placeholder="+ add tag">`:""}
      </div></div>

    ${sim.items.length?`<div class="detail-section"><h4>Similar assets</h4>
      <div class="similar-strip">${sim.items.slice(0,8).map(s=>`<img src="/api/assets/${s.uuid}/thumb" data-uuid="${s.uuid}" title="${esc(s.title)}">`).join("")}</div></div>`:""}

    <div class="detail-section"><h4>Dublin Core</h4>
      ${Object.entries(dc).filter(([,v])=>v).map(([k,v])=>`<div class="field-row"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join("")}
      <div class="btn-row"><a class="btn btn-ghost" href="/oai?verb=GetRecord&metadataPrefix=oai_dc&identifier=oai::${a.uuid}" target="_blank">View OAI-PMH XML</a></div>
    </div>

    ${Object.keys(meta.exif||{}).length||Object.keys(meta.iptc||{}).length?`<div class="detail-section"><h4>Embedded metadata (EXIF / IPTC)</h4>
      <div class="kv-scroll">${[...Object.entries(meta.iptc||{}),...Object.entries(meta.exif||{})].slice(0,40).map(([k,v])=>`<div class="field-row"><span class="k">${esc(k)}</span><span class="v">${esc(typeof v==='object'?JSON.stringify(v):v)}</span></div>`).join("")}</div></div>`:""}

    ${d.versions.length?`<div class="detail-section"><h4>Versions</h4>${d.versions.map(v=>`<div class="field-row"><span class="k">v${v.version_no}</span><span class="v">${esc(v.note||'')} &middot; ${fmtBytes(v.size)} &middot; ${(v.created_at||'').slice(0,10)}</span></div>`).join("")}</div>`:""}
  `;

  // wire detail actions
  $$("#drawer-body .similar-strip img").forEach(img => img.addEventListener("click", () => openAsset(img.dataset.uuid)));
  if (canEdit) {
    $("#d-save")?.addEventListener("click", async () => {
      const body = {};
      ["title","description","alt_text","dc_creator","rights_statement","retention_until"].forEach(f => { const el = $("#e-"+f); if (el) body[f] = el.value; });
      try {
        await api("/api/assets/"+uuid, { method: "PATCH", json: body });
        toast("Saved"); await openAsset(uuid); loadLibrary();
      } catch (err) { toast("Save failed: " + err.message, "err"); }
    });
    $("#tag-add")?.addEventListener("keydown", async (e) => {
      if (e.key === "Enter" && e.target.value.trim()) {
        try {
          await api("/api/assets/"+uuid+"/tags", { json: { name: e.target.value.trim() } });
          await openAsset(uuid); refreshFilterOptions(); loadLibrary();
        } catch (err) { toast("Tag update failed: " + err.message, "err"); }
      }
    });
    $$("#tag-row .rm").forEach(rm => rm.addEventListener("click", async () => {
      try {
        await api("/api/assets/"+uuid+"/tags/"+encodeURIComponent(rm.dataset.tag), { method: "DELETE" });
        await openAsset(uuid); refreshFilterOptions(); loadLibrary();
      } catch (err) { toast("Tag update failed: " + err.message, "err"); }
    }));
    $("#d-share")?.addEventListener("click", async () => {
      const r = await api("/api/shares", { json: { target_type: "asset", target_id: a.id, permission: "download" } });
      await navigator.clipboard?.writeText(r.url).catch(()=>{});
      toast("Share link copied: " + r.url);
    });
  }
  $("#d-delete")?.addEventListener("click", async () => {
    if (!confirm("Delete this asset?")) return;
    await api("/api/assets/"+uuid, { method: "DELETE" });
    toast("Deleted"); closeDrawer(); loadLibrary();
  });
}

// ---------- upload ----------
const uploadModal = $("#upload-modal");
$("#btn-upload").addEventListener("click", () => { uploadModal.classList.remove("hidden"); document.body.classList.add("modal-open"); });
$("#upload-cancel").addEventListener("click", () => { uploadModal.classList.add("hidden"); document.body.classList.remove("modal-open"); refreshFilterOptions(); loadLibrary(); });
$(".modal-backdrop").addEventListener("click", () => { uploadModal.classList.add("hidden"); document.body.classList.remove("modal-open"); refreshFilterOptions(); loadLibrary(); });
const dz = $("#dropzone"), fileInput = $("#file-input");
dz.addEventListener("click", () => fileInput.click());
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", e => { e.preventDefault(); dz.classList.remove("drag"); uploadFiles(e.dataTransfer.files); });
fileInput.addEventListener("change", () => uploadFiles(fileInput.files));

async function uploadFiles(files) {
  const list = $("#upload-list");
  for (const file of files) {
    const row = document.createElement("div");
    row.className = "upload-item"; row.innerHTML = `<span>${esc(file.name)}</span><span>uploading...</span>`;
    list.prepend(row);
    const fd = new FormData(); fd.append("files", file); fd.append("run_ai", $("#ai-toggle").checked);
    try {
      const r = await api("/api/assets", { method: "POST", body: fd });
      const created = r.created[0];
      row.classList.add(created.duplicate ? "dup" : "ok");
      row.lastChild.textContent = created.duplicate ? "duplicate (deduped)" : "done";
    } catch (err) { row.classList.add("err"); row.lastChild.textContent = "failed"; }
  }
}

// ---------- collections ----------
async function loadCollections() {
  const data = await api("/api/collections");
  $("#collection-list").innerHTML = data.collections.map(c =>
    `<div class="collection-card" data-id="${c.id}" data-name="${esc(c.name)}"><span class="n">${esc(c.name)}</span><span class="muted">${c.asset_count} assets</span><span class="muted">${esc(c.description||"")}</span></div>`).join("")
    || '<p class="muted">No collections yet.</p>';
  $$("#collection-list .collection-card").forEach(card => card.addEventListener("click", () => {
    state.collection = Number(card.dataset.id);
    state.collectionName = card.dataset.name;
    state.media = null; state.includeTags = []; state.excludeTags = []; state.page = 1;
    $$(".nav-item").forEach(b => b.classList.remove("active"));
    $('.nav-item[data-view="library"]').classList.add("active");
    $$(".view").forEach(v => v.classList.add("hidden")); $("#view-library").classList.remove("hidden");
    renderTagPickers(); renderActiveConditions();
    refreshFilterOptions().then(() => { $("#filter-collection").value = String(state.collection || ""); });
    loadLibrary();
  }));
}
$("#btn-new-collection").addEventListener("click", async () => {
  const name = prompt("Collection name"); if (!name) return;
  await api("/api/collections", { json: { name } }); loadCollections(); refreshFilterOptions();
});

// ---------- dashboard ----------
async function loadDashboard() {
  const s = await api("/api/stats");
  const cards = [
    ["Total assets", s.total_assets], ["Storage used", fmtBytes(s.total_bytes)],
    ["Saved by dedup", fmtBytes(s.dedup_saved_bytes)], ["Tags", s.tags], ["Collections", s.collections],
    ...Object.entries(s.by_type).map(([k,v]) => [k.charAt(0).toUpperCase()+k.slice(1), v]),
  ];
  $("#stat-cards").innerHTML = cards.map(([l,n]) => `<div class="stat-card"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
}
$("#btn-fixity").addEventListener("click", async () => {
  $("#fixity-result").textContent = "Checking...";
  const r = await api("/api/admin/fixity", { method: "POST" });
  $("#fixity-result").textContent = r.ok ? `All ${r.checked} blobs verified. Integrity intact.` : `${r.failures.length} of ${r.checked} failed fixity.`;
});

// ---------- users ----------
async function loadUsers() {
  const data = await api("/api/users");
  $("#user-rows").innerHTML = data.users.map(u =>
    `<tr><td>${esc(u.username)}</td><td>${esc(u.email||"")}</td><td>${u.role}</td><td>${u.is_active?"Yes":"No"}</td><td>${(u.last_login||"").slice(0,16).replace("T"," ")}</td></tr>`).join("");
}
$("#btn-new-user").addEventListener("click", async () => {
  const username = prompt("Username"); if (!username) return;
  const password = prompt("Temporary password"); if (!password) return;
  const role = prompt("Role (admin/editor/contributor/viewer)", "contributor") || "viewer";
  await api("/api/users", { json: { username, password, role } }); loadUsers();
});

// ---------- audit ----------
async function loadAudit() {
  const data = await api("/api/audit");
  $("#audit-rows").innerHTML = data.entries.map(e =>
    `<tr><td>${(e.ts||"").slice(0,19).replace("T"," ")}</td><td>${esc(e.username||"-")}</td><td>${esc(e.action)}</td>
     <td>${esc(e.target_type||"")} ${esc((e.target_id||"").slice(0,8))}</td><td>${esc(e.detail||"")}</td><td>${esc(e.ip||"")}</td></tr>`).join("");
}

// ---------- boot ----------
(async function boot() {
  if (state.token) {
    try { state.user = await api("/api/auth/me"); enterApp(); return; } catch {}
  }
  $("#login").classList.remove("hidden");
})();
