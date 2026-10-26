const state = {
  filters: new Map(),
  categories: new Set(),
  selected: null,
  mode: "grid",
  dimensions: [],
  aiDimensions: [],
  vocabulary: {},
  counts: {},
  categoryCounts: {},
  categoryLeafCounts: {},
  editingTags: {},
  editingDescription: {},
  editingTranscript: {},
  editingCategory: {},
  jobTimer: null,
  lastJobStatus: "",
  afterScan: false,
  requestToken: 0,
};

const RECENT_PATHS_KEY = "material-library.recent-paths";
const MAX_RECENT_PATHS = 8;

const $ = (selector) => document.querySelector(selector);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `请求失败 (${response.status})`);
  }
  return payload;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 2800);
}

function fmt(value) {
  return Number(value || 0).toFixed(2);
}

function tagKey(dimension, name) {
  return `${dimension}:${name}`;
}

function leafName(category) {
  return String(category || "").split(" / ").pop();
}

function categoryLabel(category) {
  const name = String(category || "");
  // The same leaf can exist under different folder branches (for example
  // "疼点" and "中远景 / 疼点"). Keep the compact tree when it is unique,
  // but show the path when a short label would be ambiguous.
  return (state.categoryLeafCounts[leafName(name)] || 0) > 1 ? name : leafName(name);
}

function fileName(path) {
  return String(path || "").split(/[\\/]/).pop() || "片段";
}

/* ------------------------------------------------------------------ header */

function renderMetrics(stats) {
  const clips = Object.values(stats.clips || {}).reduce((a, b) => a + b, 0);
  const sources = Object.values(stats.sources || {}).reduce((a, b) => a + b, 0);
  const values = $("#metrics").querySelectorAll(".metric-value");
  values[0].textContent = clips;
  values[1].textContent = sources;
  values[2].textContent = Math.round(stats.clip_seconds || 0).toLocaleString();
  const chip = $("#ai-chip");
  if (stats.ai && stats.ai.enabled) {
    chip.textContent = `AI · ${stats.ai.model || "ready"}`;
    chip.classList.add("is-on");
  } else {
    chip.textContent = "AI 未配置";
    chip.classList.remove("is-on");
  }
}

/* ----------------------------------------------------------------- filters */

function renderCategories() {
  const container = $("#category-groups");
  container.replaceChildren();
  const entries = Object.entries(state.categoryCounts);
  state.categoryLeafCounts = {};
  for (const [name] of entries) {
    const leaf = leafName(name);
    state.categoryLeafCounts[leaf] = (state.categoryLeafCounts[leaf] || 0) + 1;
  }
  if (!entries.length) {
    container.append(el("p", {
      class: "muted tiny",
      text: "入库后，子文件夹名会出现在这里。",
    }));
    return;
  }
  for (const [name, count] of entries) {
    const depth = name.split(" / ").length - 1;
    const isParent = entries.some(([candidate]) =>
      candidate.startsWith(`${name} / `));
    container.append(el("button", {
      class: `category-pill depth-${Math.min(depth, 2)}${isParent ? " is-parent" : ""}${state.categories.has(name) ? " is-on" : ""}`,
      title: name,
      "data-category": name,
      onclick: () => {
        if (state.categories.has(name)) state.categories.delete(name);
        else state.categories.add(name);
        renderCategories();
        renderActiveFilters();
        loadClips();
      },
    },
      el("span", { class: "category-name", text: categoryLabel(name) }),
      el("span", { class: "count", text: count }),
    ));
  }
}

function renderFilters() {
  const container = $("#filter-groups");
  container.replaceChildren();
  const dimensions = (state.dimensions.length
    ? state.dimensions
    : Object.keys(state.vocabulary)
  ).filter((name) => name !== "分类");
  for (const dimension of dimensions) {
    const options = state.counts[dimension];
    if (!options || !Object.keys(options).length) continue;
    // 光线/色彩/构图 are manual-only and cost no AI tokens, but they add
    // sidebar noise, so they stay collapsed and visibly marked.
    const systemOnly = dimension === "素材类型";
    const manualOnly = !systemOnly && state.aiDimensions.length
      && !state.aiDimensions.includes(dimension);
    const details = el("details", { class: "filter-group" });
    details.open = !manualOnly && ["分类", "动作"].includes(dimension);
    const summary = el("summary", {}, dimension,
      systemOnly
        ? el("span", { class: "filter-hint", text: "系统" })
        : manualOnly ? el("span", { class: "filter-hint", text: "手动" }) : null);
    const list = el("div", { class: "filter-options" });
    for (const [name, count] of Object.entries(options)) {
      const key = tagKey(dimension, name);
      list.append(el("button", {
        class: `tag-pill${state.filters.has(key) ? " is-on" : ""}`,
        onclick: (event) => {
          event.stopPropagation();
          if (state.filters.has(key)) state.filters.delete(key);
          else state.filters.set(key, { dimension, name });
          renderFilters();
          renderActiveFilters();
          loadClips();
        },
      }, name, el("span", { class: "count", text: count })));
    }
    details.append(summary, list);
    container.append(details);
  }
  if (!container.children.length) {
    container.append(el("p", {
      class: "muted tiny",
      text: "分析完成后，AI 标签会出现在这里。",
    }));
  }
}

function renderActiveFilters() {
  const bar = $("#active-filters");
  const chips = [];
  for (const name of state.categories) {
    chips.push(el("button", {
      class: "active-chip",
      title: `移除分类 ${name}`,
      onclick: () => {
        state.categories.delete(name);
        renderCategories();
        renderActiveFilters();
        loadClips();
      },
    }, `分类 · ${categoryLabel(name)}`));
  }
  for (const [key, item] of state.filters) {
    chips.push(el("button", {
      class: "active-chip",
      title: `移除标签 ${item.dimension}:${item.name}`,
      onclick: () => {
        state.filters.delete(key);
        renderFilters();
        renderActiveFilters();
        loadClips();
      },
    }, `${item.dimension} · ${item.name}`));
  }
  bar.replaceChildren(...chips);
  bar.hidden = chips.length === 0;
}

/* ----------------------------------------------------------------- results */

function snippet(text) {
  return text && text.trim() ? text : "未生成描述 · 待人工补充";
}

function cardFor(clip, index) {
  const thumbs = clip.thumbnails && clip.thumbnails.length
    ? clip.thumbnails
    : [null, null, null];
  const strip = el("div", { class: "strip" });
  thumbs.slice(0, 3).forEach((thumb, thumbIndex) => {
    if (thumb) {
      strip.append(el("img", {
        src: `/api/clips/${clip.id}/thumbnail?index=${thumbIndex}`,
        alt: "",
        loading: "lazy",
      }));
    } else {
      strip.append(el("div", { class: "strip-blank" }));
    }
  });
  const tagChips = [];
  let shown = 0;
  for (const [dimension, values] of Object.entries(clip.tags || {})) {
    for (const value of values) {
      if (shown >= 5) break;
      tagChips.push(el("span", { class: "chip", text: value }));
      shown += 1;
    }
    if (shown >= 5) break;
  }
  if (!tagChips.length) {
    tagChips.push(el("span", { class: "chip dim", text: "无标签" }));
  }
  return el("article", {
    class: `card${state.selected === clip.id ? " is-selected" : ""}`,
    style: `animation-delay:${Math.min(index * 18, 260)}ms`,
    "data-id": clip.id,
    onclick: () => selectClip(clip.id),
  },
    el("span", {
      class: `badge ${clip.status}`,
      text: clip.status === "ready" ? "已标注" : clip.status === "needs_review" ? "待复核" : clip.status,
    }),
    strip,
    el("div", { class: "card-body" },
      el("div", { class: "card-time" },
        el("span", { text: `${fmt(clip.start)} – ${fmt(clip.end)}s` }),
        el("span", { text: `${fmt(clip.duration)}s` }),
      ),
      clip.category
        ? el("p", { class: "card-cat", text: clip.category, title: clip.category })
        : null,
      el("p", { class: "card-desc", text: snippet(clip.description) }),
      el("div", { class: "card-tags" }, ...tagChips),
    ),
  );
}

async function loadClips() {
  const token = ++state.requestToken;
  const params = new URLSearchParams();
  const keyword = $("#query").value.trim();
  if (keyword) params.set("q", keyword);
  const minDuration = $("#min-duration").value;
  const maxDuration = $("#max-duration").value;
  if (minDuration) params.set("min_duration", minDuration);
  if (maxDuration) params.set("max_duration", maxDuration);
  if (state.filters.size) {
    params.set("tags", [...state.filters.values()]
      .map((item) => tagKey(item.dimension, item.name)).join(","));
  }
  if (state.categories.size) {
    params.set("category", [...state.categories].join(","));
  }
  params.set("limit", "120");
  try {
    const data = await api(`/api/clips?${params}`);
    // A slower earlier request must not overwrite a newer result set.
    if (token !== state.requestToken) return;
    const grid = $("#grid");
    grid.className = `grid${state.mode === "list" ? " is-list" : ""}`;
    grid.replaceChildren(...data.items.map(cardFor));
    const filtered = state.filters.size || state.categories.size || keyword
      || minDuration || maxDuration;
    $("#result-title").textContent = filtered ? "检索结果" : "全部片段";
    $("#result-meta").textContent =
      `${data.total} 个片段 · 按标签/文本/动作/时长/画质加权排序`;
    $("#empty").hidden = data.items.length > 0;
    $("#empty .empty-title").textContent = filtered
      ? "没有匹配的片段"
      : "还没有可用的片段";
    if (state.selected) {
      const still = data.items.find((item) => item.id === state.selected);
      if (still) renderInspector(still);
    }
  } catch (error) {
    if (token === state.requestToken) toast(error.message);
  }
}

/* --------------------------------------------------------------- inspector */

function renderInspector(clip) {
  $("#inspector-placeholder").hidden = true;
  const body = $("#inspector-body");
  body.hidden = false;
  if (!state.editingTags[clip.id]) {
    state.editingTags[clip.id] = JSON.parse(JSON.stringify(clip.tags || {}));
  }
  if (state.editingDescription[clip.id] == null) {
    state.editingDescription[clip.id] = clip.description || "";
  }
  if (state.editingTranscript[clip.id] == null) {
    state.editingTranscript[clip.id] = clip.transcript || "";
  }
  if (state.editingCategory[clip.id] == null) {
    state.editingCategory[clip.id] = clip.category || "";
  }
  const chips = [];
  for (const [dimension, values] of Object.entries(clip.tags || {})) {
    for (const value of values) {
      chips.push(el("span", { class: "chip", text: `${dimension} · ${value}` }));
    }
  }
  const categoryOptions = Object.keys(state.categoryCounts);
  body.replaceChildren(
    el("div", { class: "inspector-head" },
      el("h2", { text: fileName(clip.path), title: clip.path }),
      el("span", { class: `badge ${clip.status}`, text: clip.status }),
    ),
    el("p", {
      class: "timecode",
      text: `${fmt(clip.start)} – ${fmt(clip.end)}s · ${fmt(clip.duration)}s · 动作 ${fmt(clip.action_score)} · 画质 ${fmt(clip.quality_score)}`,
    }),
    el("video", {
      class: "player",
      controls: "",
      preload: "metadata",
      src: `/api/clips/${clip.id}/preview`,
    }),
    el("hr", { class: "section-rule" }),
    el("label", { class: "field" },
      el("span", { text: "目录分类（写入源文件，影响该文件的所有片段）" }),
      el("input", {
        type: "text",
        id: "edit-category",
        list: "category-options",
        spellcheck: "false",
        value: state.editingCategory[clip.id],
        oninput: (event) => { state.editingCategory[clip.id] = event.target.value; },
      }),
      el("datalist", { id: "category-options" },
        ...categoryOptions.map((name) => el("option", { value: name })),
      ),
    ),
    el("label", { class: "field" },
      el("span", { text: "描述" }),
      el("textarea", {
        id: "edit-description",
        oninput: (event) => { state.editingDescription[clip.id] = event.target.value; },
      }, state.editingDescription[clip.id] || ""),
    ),
    el("label", { class: "field" },
      el("span", { text: "字幕 / 文案（可选，辅助分类和搜索）" }),
      el("textarea", {
        id: "edit-transcript",
        oninput: (event) => { state.editingTranscript[clip.id] = event.target.value; },
      }, state.editingTranscript[clip.id] || ""),
    ),
    el("div", { class: "field" },
      el("span", { text: "标签（点击移除）" }),
      el("div", { class: "tag-editor" }, ...tagEditorRows(clip)),
      addTagRow(clip),
    ),
    chips.length ? el("div", { class: "chips current-tags" }, ...chips) : null,
    el("div", { class: "row-actions" },
      el("button", { class: "primary", onclick: () => saveClip(clip) }, "保存修改"),
      el("button", {
        class: "ghost",
        onclick: () => markReviewed(clip, !clip.reviewed),
      }, clip.reviewed ? "取消锁定" : "确认并锁定"),
      el("button", { class: "ghost", onclick: () => relabel(clip) }, "重新 AI 标注"),
    ),
    el("hr", { class: "section-rule" }),
    el("p", { class: "path-note", text: clip.path }),
    el("p", { class: "path-note", text: `clip id: ${clip.id}` }),
  );
}

function tagEditorRows(clip) {
  const editable = state.editingTags[clip.id] || {};
  const rows = [];
  for (const [dimension, values] of Object.entries(editable)) {
    rows.push(el("div", { class: "tag-row" },
      el("span", { text: dimension }),
      el("div", { class: "chips" }, ...values.map((value) =>
        el("span", {
          class: "chip removable",
          text: value,
          onclick: () => {
            editable[dimension] = values.filter((item) => item !== value);
            if (!editable[dimension].length) delete editable[dimension];
            renderInspector(clip);
          },
        }),
      )),
    ));
  }
  if (!rows.length) rows.push(el("p", { class: "muted", text: "尚无标签，使用下方选择器补充。" }));
  return rows;
}

function addTagRow(clip) {
  const dimensions = (state.dimensions.length
    ? state.dimensions
    : Object.keys(state.vocabulary)
  ).filter((dimension) => dimension !== "素材类型");
  const select = el("select", {}, el("option", { value: "", text: "维度" }),
    ...dimensions.map((dimension) => el("option", { value: dimension, text: dimension })),
  );
  const input = el("input", { list: "tag-suggestions", placeholder: "标签名" });
  const datalist = el("datalist", { id: "tag-suggestions" },
    ...Object.entries(state.vocabulary).flatMap(([dimension, names]) =>
      names.map((name) => el("option", { value: name })),
    ),
  );
  const add = () => {
    const name = input.value.trim();
    if (!name) return;
    let dimension = select.value;
    if (dimension) {
      if (!(state.vocabulary[dimension] || []).includes(name)) {
        toast(`「${name}」不在${dimension}词表内`);
        return;
      }
    } else {
      const found = Object.entries(state.vocabulary)
        .find(([, names]) => names.includes(name));
      if (!found) {
        toast(`「${name}」不在标签词表内`);
        return;
      }
      dimension = found[0];
    }
    const editable = state.editingTags[clip.id] || {};
    editable[dimension] = [...new Set([...(editable[dimension] || []), name])].slice(0, 4);
    renderInspector(clip);
  };
  return el("div", { class: "add-tag" },
    select,
    input,
    datalist,
    el("button", { onclick: add, type: "button" }, "添加"),
  );
}

async function saveClip(clip) {
  const description = $("#edit-description").value;
  const transcript = $("#edit-transcript").value;
  const tags = state.editingTags[clip.id] || {};
  const category = (state.editingCategory[clip.id] || "").trim();
  const payload = { description, transcript, tags, status: "ready" };
  const categoryChanged = category !== (clip.category || "");
  if (categoryChanged) payload.category = category;
  try {
    const updated = await api(`/api/clips/${clip.id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    state.editingDescription[clip.id] = updated.description || "";
    state.editingTranscript[clip.id] = updated.transcript || "";
    state.editingTags[clip.id] = JSON.parse(JSON.stringify(updated.tags || {}));
    state.editingCategory[clip.id] = updated.category || "";
    renderInspector(updated);
    toast(categoryChanged ? "已保存，分类同步到该源文件" : "已保存");
    await loadFacets();
    await loadClips();
  } catch (error) {
    toast(error.message);
  }
}

async function markReviewed(clip, reviewed) {
  try {
    const updated = await api(`/api/clips/${clip.id}`, {
      method: "PATCH",
      body: JSON.stringify({ reviewed }),
    });
    state.editingDescription[clip.id] = updated.description || "";
    state.editingTranscript[clip.id] = updated.transcript || "";
    state.editingTags[clip.id] = JSON.parse(JSON.stringify(updated.tags || {}));
    state.editingCategory[clip.id] = updated.category || "";
    renderInspector(updated);
    await loadClips();
    toast(reviewed ? "已锁定，重新分析不会覆盖" : "已解锁");
  } catch (error) {
    toast(error.message);
  }
}

async function relabel(clip) {
  try {
    toast("正在请求 AI 标注…");
    const data = await api(`/api/clips/${clip.id}/relabel`, { method: "POST" });
    if (data.clip) {
      const next = data.clip;
      state.editingDescription[next.id] = next.description || "";
      state.editingTranscript[next.id] = next.transcript || "";
      state.editingTags[next.id] = JSON.parse(JSON.stringify(next.tags || {}));
      state.editingCategory[next.id] = next.category || "";
      renderInspector(next);
    }
    await loadFacets();
    await loadClips();
    toast("AI 标注完成");
  } catch (error) {
    toast(error.message);
  }
}

async function selectClip(id) {
  try {
    const clip = await api(`/api/clips/${id}`);
    state.selected = id;
    state.editingDescription[id] = clip.description || "";
    state.editingTranscript[id] = clip.transcript || "";
    state.editingTags[id] = JSON.parse(JSON.stringify(clip.tags || {}));
    state.editingCategory[id] = clip.category || "";
    document.querySelectorAll(".card").forEach((card) => {
      card.classList.toggle("is-selected", card.dataset.id === id);
    });
    renderInspector(clip);
  } catch (error) {
    toast(error.message);
  }
}

/* ------------------------------------------------------------------ facets */

async function loadFacets() {
  try {
    const data = await api("/api/tags");
    state.vocabulary = data.vocabulary || {};
    state.dimensions = data.dimensions || Object.keys(state.vocabulary);
    state.aiDimensions = data.ai_dimensions || [];
    state.counts = data.counts || {};
    state.categoryCounts = data.category_counts || {};
    // Drop filters whose category no longer exists after a rescan.
    for (const name of [...state.categories]) {
      if (!(name in state.categoryCounts)) state.categories.delete(name);
    }
    renderCategories();
    renderFilters();
    renderActiveFilters();
    const stats = await api("/api/stats");
    renderMetrics(stats);
  } catch (error) {
    toast(error.message);
  }
}

/* --------------------------------------------------------------------- job */

function jobChipLabel(job) {
  const verb = job.kind === "scan" ? "扫描" : "分析";
  if (job.status === "running") {
    return job.total ? `${verb} ${job.current}/${job.total}` : `${verb}中…`;
  }
  if (job.status === "done") return "任务完成";
  if (job.status === "failed") return "任务失败";
  return "";
}

function updateJobUI(job) {
  const chip = $("#job-chip");
  const label = jobChipLabel(job);
  chip.hidden = !label;
  chip.textContent = label;
  chip.classList.toggle("is-on", job.status === "running");
  chip.classList.toggle("is-error", job.status === "failed");

  const percent = job.total ? Math.round((job.current / job.total) * 100) : 0;
  $("#jobbar-fill").style.width = `${percent}%`;
  $("#job-text").textContent = job.status === "idle"
    ? "空闲"
    : (job.error || job.message || label);
  const running = job.status === "running";
  $("#run-scan").disabled = running;
  $("#run-scan-analyze").disabled = running;
  $("#ingest-path").disabled = running;
  $("#ingest-recursive").disabled = running;
  $("#ingest-material-type").disabled = running;
}

function stopPolling() {
  if (state.jobTimer) {
    clearInterval(state.jobTimer);
    state.jobTimer = null;
  }
}

function ensurePolling() {
  if (!state.jobTimer) state.jobTimer = setInterval(pollJob, 900);
}

async function pollJob() {
  let job;
  try {
    job = await api("/api/job");
  } catch {
    return;
  }
  const previous = state.lastJobStatus;
  state.lastJobStatus = job.status;
  updateJobUI(job);
  if (job.status === "running") {
    ensurePolling();
    return;
  }
  stopPolling();
  if (job.status === "failed") {
    state.afterScan = false;
    toast(job.error || "任务失败");
    return;
  }
  if (job.status === "done" && previous === "running") {
    await afterJobDone(job);
    clearTimeout(state.jobChipTimer);
    state.jobChipTimer = setTimeout(() => {
      const chip = $("#job-chip");
      if (!chip.classList.contains("is-on")) chip.hidden = true;
    }, 6000);
  }
}

async function afterJobDone(job) {
  const result = job.result || {};
  if (state.afterScan) {
    state.afterScan = false;
    const ids = result.source_ids || [];
    if (!ids.length) {
      toast("没有发现新素材，索引已是最新");
    } else {
      toast(`新增/变更 ${ids.length} 个素材，开始分析`);
      await startJob("analyze", { source_ids: ids });
      return;
    }
  } else if (job.kind === "analyze") {
    const analyzed = result.analyzed ?? 0;
    const failed = result.failed ?? 0;
    toast(failed ? `分析完成：成功 ${analyzed}，失败 ${failed}` : `分析完成：${analyzed} 个素材`);
  }
  await loadFacets();
  await loadClips();
}

async function startJob(kind, body) {
  try {
    const response = await api(kind === "scan" ? "/api/scan" : "/api/analyze", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (response && response.started === false) {
      state.afterScan = false;
      toast(response.reason || "没有需要处理的任务");
      return;
    }
    state.lastJobStatus = "running";
    updateJobUI({ kind, status: "running", current: 0, total: 0, message: "已提交" });
    ensurePolling();
    pollJob();
  } catch (error) {
    state.afterScan = false;
    toast(error.message);
  }
}

/* ------------------------------------------------------------ ingest dialog */

function readRecentPaths() {
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_PATHS_KEY) || "[]");
    return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function renderRecentPaths() {
  $("#recent-paths").replaceChildren(
    ...readRecentPaths().map((path) => el("option", { value: path })),
  );
}

function rememberPath(path) {
  const recent = [path, ...readRecentPaths().filter((item) => item !== path)]
    .slice(0, MAX_RECENT_PATHS);
  localStorage.setItem(RECENT_PATHS_KEY, JSON.stringify(recent));
  renderRecentPaths();
}

function ingestPath() {
  const input = $("#ingest-path");
  // Explorer's "copy as path" adds quotes; strip them so the scan does not fail.
  input.value = input.value.trim().replace(/^"+|"+$/g, "");
  return input.value;
}

/* --------------------------------------------------------------------- bind */

function bind() {
  $("#search-button").addEventListener("click", loadClips);
  for (const id of ["#query", "#min-duration", "#max-duration"]) {
    $(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadClips();
    });
  }
  $("#clear-filters").addEventListener("click", () => {
    state.filters.clear();
    renderFilters();
    renderActiveFilters();
    loadClips();
  });
  $("#clear-categories").addEventListener("click", () => {
    state.categories.clear();
    renderCategories();
    renderActiveFilters();
    loadClips();
  });
  document.querySelectorAll(".view-toggle .ghost").forEach((button) => {
    button.addEventListener("click", () => {
      state.mode = button.dataset.mode;
      document.querySelectorAll(".view-toggle .ghost").forEach((item) =>
        item.classList.toggle("is-active", item === button));
      loadClips();
    });
  });

  const dialog = $("#ingest-dialog");
  $("#open-ingest").addEventListener("click", () => {
    renderRecentPaths();
    dialog.showModal();
    pollJob();
  });
  $("#close-ingest").addEventListener("click", () => dialog.close());
  // Clicking the backdrop closes; clicks inside the card must not.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("cancel", (event) => {
    dialog.close();
  });

  $("#run-scan").addEventListener("click", () => {
    const path = ingestPath();
    if (!path) return toast("请填写素材文件夹");
    rememberPath(path);
    state.afterScan = false;
    startJob("scan", {
      path,
      recursive: $("#ingest-recursive").checked,
      material_type: $("#ingest-material-type").value,
    });
  });
  $("#run-scan-analyze").addEventListener("click", () => {
    const path = ingestPath();
    if (!path) return toast("请填写素材文件夹");
    rememberPath(path);
    state.afterScan = true;
    startJob("scan", {
      path,
      recursive: $("#ingest-recursive").checked,
      material_type: $("#ingest-material-type").value,
    });
  });
  $("#ingest-path").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      $("#run-scan-analyze").click();
    }
  });
}

async function main() {
  bind();
  await loadFacets();
  await loadClips();
  // A job may still be running from a previous session; reflect it right away.
  pollJob();
}

main();
