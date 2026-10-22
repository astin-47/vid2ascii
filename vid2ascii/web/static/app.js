// vid2ascii web interface. No framework, no build step.
// Preview rules (spec "no lag"): the newest request wins, at most one request in flight,
// drafts while dragging, a full frame on release, and an image is swapped in only after it decodes.

const $ = (id) => document.getElementById(id);
const api = (path) => path;
const fileUrl = (id, tail) => `/api/files/${encodeURIComponent(id)}/${tail}`;

const ASPECT_LABELS = {
  source: "Same as the video", reel: "Reel 9:16", square: "Square 1:1",
  wide: "Wide 16:9", cinematic: "Cinematic 2.39:1", custom: "Custom size…",
};
const COLOR_LABELS = { original: "Original", green: "Green", amber: "Amber", mono: "Mono" };
const CHARSET_LABELS = { standard: "Standard", detailed: "Detailed", blocks: "Blocks", binary: "Binary" };
const FPS_CHOICES = [12, 24, 30];
const MIN_RANGE = 0.5;

const state = {
  config: null,          // /api/options
  options: null,         // current settings (engine Options names)
  clip: null,            // selected source item
  duration: 0,
  start: 0,
  end: 0,
  t: 0,                  // playhead, seconds into the video
  grid: null,            // "120x36" from the last preview
  output: null,          // "1920x1080" from the last preview
  job: null,
  result: null,          // {id, name, width, height, duration}
  mode: "preview",
};

// ---------------------------------------------------------------- formatting
function clock(seconds) {
  const s = Math.max(0, seconds || 0);
  const m = Math.floor(s / 60);
  const rest = (s - m * 60).toFixed(1).padStart(4, "0");
  return `${m}:${rest}`;
}
function secs(value) { return `${(Math.round(value * 10) / 10).toFixed(1)} s`; }
function times(text) { return text.replace("x", " × "); }
function short(seconds) {
  const s = Math.round(seconds || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function rangeLength() { return Math.max(0, state.end - state.start); }
const knownLength = () => state.duration > 0;
function renderSeconds() {
  return knownLength() ? Math.min(rangeLength(), state.config.limits.max_duration) : null;
}
function frameCount() {
  const seconds = renderSeconds() ?? state.config.limits.max_duration;
  return Math.max(1, Math.round(seconds * state.options.fps));
}

// ---------------------------------------------------------------- toasts
let toastTimer = null;
function toast(message, kind = "info") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind === "error" ? "error" : ""}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === "error" ? 7000 : 4000);
}

async function getJSON(path, init) {
  const res = await fetch(api(path), init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

// ---------------------------------------------------------------- settings controls
function segmented(container, choices, current, onPick, role = "radio") {
  container.replaceChildren();
  for (const [value, label] of choices) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", role);
    b.dataset.value = String(value);
    b.textContent = label;
    b.setAttribute("aria-checked", String(String(value) === String(current)));
    b.addEventListener("click", () => onPick(value));
    container.append(b);
  }
}
function markSegment(container, current) {
  for (const b of container.querySelectorAll("button")) {
    b.setAttribute("aria-checked", String(b.dataset.value === String(current)));
  }
}

function buildSettings() {
  const cfg = state.config;
  segmented($("color-seg"), cfg.colors.map((c) => [c, COLOR_LABELS[c] || c]), state.options.color,
    (v) => setOption("color", v));
  segmented($("charset-seg"), Object.keys(cfg.charsets).map((c) => [c, CHARSET_LABELS[c] || c]),
    state.options.charset, (v) => { $("charset-custom").value = ""; setOption("charset", v); });
  segmented($("contrast-seg"), [["auto", "Auto"], ["none", "Off"], ["factor", "Custom"]],
    contrastMode(), (v) => {
      if (v === "factor") {
        $("contrast-factor").hidden = false;
        setOption("contrast", $("contrast-range").value);
      } else {
        $("contrast-factor").hidden = true;
        setOption("contrast", v);
      }
    });
  segmented($("fps-seg"), FPS_CHOICES.map((f) => [f, String(f)]), state.options.fps,
    (v) => setOption("fps", Number(v)));

  const select = $("aspect-select");
  select.replaceChildren();
  for (const a of [...cfg.aspects, "custom"]) {
    const o = document.createElement("option");
    o.value = a;
    o.textContent = ASPECT_LABELS[a] || a;
    select.append(o);
  }
  const [lo, hi] = cfg.limits.cols;
  for (const el of [$("cols-range"), $("cols-num")]) { el.min = lo; el.max = hi; }
  syncControls();
}

function contrastMode() {
  const c = String(state.options.contrast);
  return c === "auto" || c === "none" ? c : "factor";
}

function syncControls() {
  const o = state.options;
  $("cols-range").value = o.cols;
  $("cols-num").value = o.cols;
  markSegment($("color-seg"), o.color);
  const custom = !(o.charset in state.config.charsets);
  markSegment($("charset-seg"), custom ? "" : o.charset);
  $("charset-custom").value = custom ? o.charset : "";
  markSegment($("contrast-seg"), contrastMode());
  $("contrast-factor").hidden = contrastMode() !== "factor";
  if (contrastMode() === "factor") $("contrast-range").value = o.contrast;
  $("contrast-value").textContent = `${Number($("contrast-range").value).toFixed(1)}×`;
  const aspect = o.aspect || "source";
  const known = state.config.aspects.includes(aspect);
  $("aspect-select").value = known ? aspect : "custom";
  $("aspect-custom").hidden = known;
  if (!known) $("aspect-custom").value = aspect;
  markSegment($("fps-seg"), o.fps);
  $("fps-input").value = o.fps;
  $("beat-switch").checked = !!o.beat;
  renderRamp();
}

function setOption(key, value, { draft = false } = {}) {
  state.options = { ...state.options, [key]: value };
  syncControls();
  updateFinding();
  if (draft) {
    preview("draft");
    settleFull();
  } else {
    preview("full");
  }
}

// ---------------------------------------------------------------- the character ramp
function activeChars() {
  const o = state.options;
  return state.config.charsets[o.charset] ?? o.charset;
}
function renderRamp() {
  const ramp = $("ramp");
  const chars = [...activeChars()];
  const shown = chars.length > 24 ? chars.filter((_, i) => i % Math.ceil(chars.length / 24) === 0) : chars;
  ramp.classList.toggle("dense", shown.length > 16);
  ramp.replaceChildren(...shown.map((ch, i) => {
    const s = document.createElement("span");
    s.textContent = ch === " " ? " " : ch;
    if (i === shown.length - 1) s.className = "lit";
    s.title = i === 0 ? "darkest" : i === shown.length - 1 ? "brightest" : "";
    return s;
  }));
}

// ---------------------------------------------------------------- validation
function validity() {
  const custom = $("charset-custom").value;
  const charsetOk = !custom || [...custom].length >= 2;
  $("charset-custom").classList.toggle("invalid", !charsetOk);
  const aspectText = $("aspect-custom").value.trim();
  const aspectOk = $("aspect-custom").hidden || /^\s*\d+\s*[x×]\s*\d+\s*$/i.test(aspectText);
  $("aspect-custom").classList.toggle("invalid", !aspectOk);
  const colsOk = Number(state.options.cols) >= state.config.limits.cols[0];
  const fpsOk = Number(state.options.fps) > 0;
  $("fps-input").classList.toggle("invalid", !fpsOk);
  return charsetOk && aspectOk && colsOk && fpsOk && !!state.clip && !state.rangeError;
}

function requestOptions() {
  return {
    ...state.options,
    start: round3(state.start),
    duration: knownLength() ? round3(renderSeconds()) : null,
  };
}
const round3 = (v) => Math.round(v * 1000) / 1000;

// ---------------------------------------------------------------- preview pipeline
const pv = { inflight: null, pending: null, seq: 0, shown: 0, url: null };
let fullTimer = null;

function settleFull(delay = 200) {
  clearTimeout(fullTimer);
  fullTimer = setTimeout(() => preview("full"), delay);
}

function preview(size) {
  if (!state.clip || !validity()) { updateRenderButton(); return; }
  updateRenderButton();
  if (state.mode !== "preview") setMode("preview");
  const req = {
    seq: ++pv.seq,
    body: { id: state.clip.id, t: round3(state.t), size, options: requestOptions() },
  };
  if (size === "full") {
    clearTimeout(fullTimer);
    if (pv.inflight) pv.inflight.ctrl.abort();
    pv.pending = null;
    send(req);
  } else if (pv.inflight) {
    pv.pending = req;           // the newest draft replaces any queued one
  } else {
    send(req);
  }
}

async function send(req) {
  const ctrl = new AbortController();
  const mine = { ctrl, req };
  pv.inflight = mine;
  try {
    const res = await fetch(api("/api/preview"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req.body), signal: ctrl.signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `preview failed (${res.status})`);
    }
    const grid = res.headers.get("x-grid");
    const output = res.headers.get("x-output");
    const ms = res.headers.get("x-render-ms");
    const url = URL.createObjectURL(await res.blob());
    const img = new Image();
    try {
      await new Promise((resolve, reject) => {
        img.onload = resolve;
        img.onerror = () => reject(new Error("the preview image could not be read"));
        img.src = url;
      });
      // decode before the swap so the stage never flashes; an occluded window may defer
      // decoding indefinitely, so wait at most 150 ms for it
      await Promise.race([img.decode().catch(() => {}), new Promise((r) => setTimeout(r, 150))]);
    } catch (err) {
      URL.revokeObjectURL(url);
      throw err;
    }
    if (req.seq > pv.shown) {
      pv.shown = req.seq;
      const el = $("preview-img");
      el.src = url;
      el.hidden = false;
      $("stage-empty").hidden = true;
      if (pv.url) URL.revokeObjectURL(pv.url);
      pv.url = url;
      state.grid = grid;
      state.output = output;
      $("render-ms").textContent = ms ? `${req.body.size === "draft" ? "draft" : "frame"} ${ms} ms` : "";
      setFrameAspect(output);
      updateFinding();
    } else {
      URL.revokeObjectURL(url);
    }
  } catch (err) {
    if (err.name !== "AbortError") toast(err.message, "error");
  } finally {
    if (pv.inflight === mine) {
      pv.inflight = null;
      if (pv.pending) {
        const next = pv.pending;
        pv.pending = null;
        send(next);
      }
    }
  }
}

// ---------------------------------------------------------------- stage sizing
let frameRatio = 16 / 9;
function setFrameAspect(output) {
  if (!output) return;
  const [w, h] = output.split("x").map(Number);
  if (w > 0 && h > 0 && Math.abs(w / h - frameRatio) > 1e-3) {
    frameRatio = w / h;
    fitFrame();
  }
}
function fitFrame() {
  // the frame fills the stage and the picture is contained in it, so only the phone's stage height follows the aspect
  if (window.matchMedia("(max-width: 1100px)").matches) {
    $("stage").style.height = `${Math.round($("stage").clientWidth / frameRatio)}px`;
  } else {
    $("stage").style.height = "";
  }
}
new ResizeObserver(fitFrame).observe($("stage-panel") || document.body);

function updateFinding() {
  const el = $("finding");
  if (!state.clip) { el.textContent = "Choose a video to see it as characters."; return; }
  el.replaceChildren();
  const parts = [];
  if (state.grid) parts.push([times(state.grid), " characters"]);
  if (state.output) parts.push([times(state.output), ""]);
  if (knownLength()) {
    parts.push([secs(renderSeconds()), ""]);
    parts.push([`${frameCount().toLocaleString()} frames`, ""]);
  } else {
    parts.push(["length unknown", ", renders up to 60 s"]);
  }
  parts.forEach(([strong, rest], i) => {
    if (i) el.append(i === parts.length - 1 ? " → " : " · ");
    const b = document.createElement("strong");
    b.textContent = strong;
    el.append(b, rest);
  });
}

// ---------------------------------------------------------------- timeline
const track = $("track");
function pctOf(t) { return state.duration > 0 ? (t / state.duration) * 100 : 0; }
function timeAt(clientX) {
  const r = track.getBoundingClientRect();
  return Math.min(1, Math.max(0, (clientX - r.left) / r.width)) * state.duration;
}
function drawTimeline() {
  const a = pctOf(state.start), b = pctOf(state.end);
  $("shade-left").style.width = `${a}%`;
  $("shade-right").style.width = `${100 - b}%`;
  $("range").style.left = `${a}%`;
  $("range").style.width = `${b - a}%`;
  const p = pctOf(state.t);
  const head = $("playhead");
  head.style.left = `${p}%`;
  head.classList.toggle("flip", p > 85);
  $("playhead-label").textContent = clock(state.t);
  $("t-end").textContent = knownLength() ? clock(state.duration) : "–";
  $("range").hidden = !knownLength();
  const editable = !!state.clip && knownLength();
  for (const id of ["start-input", "end-input"]) $(id).disabled = !editable;
  if (!state.clip) {
    $("start-input").value = $("end-input").value = "";
    $("t-range").textContent = "";
    return;
  }
  if (!knownLength()) {
    $("start-input").value = "0:00.0";
    $("end-input").value = "";
    $("t-range").textContent = "This file does not store its length; the render runs to its end, 60 s at most";
    return;
  }
  // typed values stay as typed while the field has focus
  if (document.activeElement !== $("start-input")) $("start-input").value = clock(state.start);
  if (document.activeElement !== $("end-input")) $("end-input").value = clock(state.end);
  if (!state.rangeError) {
    $("t-range").classList.remove("invalid");
    $("t-range").textContent = `= ${secs(rangeLength())}`;
  }
}

// "1:10", "1:10.5", "0:01:10" or "70" -> seconds; NaN when it is not a time
function parseTime(text) {
  const parts = String(text).trim().replace(",", ".").split(":");
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d*)?$/.test(p))) return NaN;
  return parts.reduce((total, p) => total * 60 + Number(p), 0);
}

function applyTypedRange() {
  if (!state.clip || !knownLength()) return;
  const s = parseTime($("start-input").value);
  const e = parseTime($("end-input").value);
  const max = state.config.limits.max_duration;
  let error = null;
  if (Number.isNaN(s) || Number.isNaN(e)) error = "Use a time like 1:10 or 70";
  else if (e > state.duration + 0.05) error = `The video ends at ${clock(state.duration)}`;
  else if (e - s < MIN_RANGE) error = "The end must come after the start";
  else if (e - s > max + 1e-6) error = `A render can be ${max} s at most`;
  $("start-input").classList.toggle("invalid", !!error && (Number.isNaN(s) || e - s < MIN_RANGE || e - s > max));
  $("end-input").classList.toggle("invalid", !!error);
  state.rangeError = error;
  if (error) {
    $("t-range").textContent = error;
    $("t-range").classList.add("invalid");
    updateRenderButton();
    return;
  }
  $("start-input").classList.remove("invalid");
  state.start = s;
  state.end = Math.min(e, state.duration);
  state.t = state.start;
  drawTimeline();
  updateFinding();
  preview("full");
}

let rangeTimer = null;
for (const id of ["start-input", "end-input"]) {
  $(id).addEventListener("input", () => { clearTimeout(rangeTimer); rangeTimer = setTimeout(applyTypedRange, 600); });
  $(id).addEventListener("change", () => { clearTimeout(rangeTimer); applyTypedRange(); });
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(rangeTimer); applyTypedRange(); } });
  $(id).addEventListener("blur", () => { if (!state.rangeError) drawTimeline(); });
}

let drag = null;
let dragLength = 0;
track.addEventListener("pointerdown", (e) => {
  if (!state.clip || !knownLength()) return;
  const handle = e.target.closest(".handle");
  drag = handle ? (handle.id === "handle-start" ? "start" : "end") : "playhead";
  if (drag !== "playhead") clearRangeError();
  dragLength = Math.max(MIN_RANGE, state.end - state.start);
  track.setPointerCapture(e.pointerId);
  moveDrag(e.clientX);
});
track.addEventListener("pointermove", (e) => { if (drag) moveDrag(e.clientX); });
const endDrag = () => {
  if (!drag) return;
  drag = null;
  preview("full");
};
track.addEventListener("pointerup", endDrag);
track.addEventListener("pointercancel", endDrag);

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

function clearRangeError() {
  state.rangeError = null;
  for (const id of ["start-input", "end-input"]) $(id).classList.remove("invalid");
  $("t-range").classList.remove("invalid");
}

function moveDrag(x) {
  const t = timeAt(x);
  const max = state.config.limits.max_duration;
  const d = state.duration;
  if (drag === "start") {
    state.start = clamp(t, 0, Math.max(0, d - MIN_RANGE));
    // a start pushed past the end carries the end along, keeping the range's length
    if (state.end < state.start + MIN_RANGE) state.end = state.start + dragLength;
    state.end = clamp(state.end, Math.min(d, state.start + MIN_RANGE), Math.min(d, state.start + max));
    state.t = state.start;
  } else if (drag === "end") {
    state.end = clamp(t, Math.min(MIN_RANGE, d), d);
    if (state.start > state.end - MIN_RANGE) state.start = state.end - dragLength;
    state.start = clamp(state.start, Math.max(0, state.end - max), Math.max(0, state.end - MIN_RANGE));
    state.t = state.end;
  } else {
    state.t = t;
  }
  drawTimeline();
  updateFinding();
  preview("draft");
}

// ---------------------------------------------------------------- sources
function clipButton(item, sub, onClick) {
  const li = document.createElement("li");
  const b = document.createElement("button");
  b.type = "button";
  b.className = "clip";
  b.dataset.id = item.id;
  const img = document.createElement("img");
  img.alt = "";
  img.loading = "lazy";
  img.src = fileUrl(item.id, "thumb");
  img.onerror = () => { img.replaceWith(Object.assign(document.createElement("span"), { className: "thumb-blank" })); };
  const text = document.createElement("span");
  const name = document.createElement("span");
  name.className = "clip-name";
  name.textContent = item.name;
  name.title = item.name;
  const s = document.createElement("span");
  s.className = "clip-sub num";
  s.textContent = sub;
  text.append(name, s);
  b.append(img, text);
  b.addEventListener("click", () => onClick(item));
  const trash = document.createElement("button");
  trash.type = "button";
  trash.className = "trash";
  trash.title = "Move to the Recycle Bin";
  trash.setAttribute("aria-label", `Move ${item.name} to the Recycle Bin`);
  trash.innerHTML = TRASH_ICON;  // a fixed icon, never data
  trash.addEventListener("click", (e) => { e.stopPropagation(); deleteItem(item); });
  li.append(b, trash);
  return li;
}

const TRASH_ICON = '<svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11M6.5 4V2.5h3V4M4 4l.7 9.5h6.6L12 4M6.8 6.8v4.4M9.2 6.8v4.4"/></svg>';

function confirmDelete(item) {
  const dialog = $("confirm-dialog");
  $("confirm-text").textContent = `“${item.name}” will be moved to the Recycle Bin. You can restore it from there.`;
  $("confirm-note").hidden = !item.id.startsWith("lib:");
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.returnValue = "cancel";
    dialog.showModal();
    $("confirm-ok").focus();
  });
}

async function deleteItem(item) {
  if (!(await confirmDelete(item))) return;
  try {
    await getJSON(`/api/files/${encodeURIComponent(item.id)}`, { method: "DELETE" });
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  if (state.clip && state.clip.id === item.id) clearClip();
  if (state.result && state.result.id === item.id) clearResult();
  await Promise.all([loadLibrary(), loadRenders()]);
  toast(`Moved ${item.name} to the Recycle Bin.`);
}

function clearClip() {
  clearRangeError();
  state.clip = null;
  state.duration = state.start = state.end = state.t = 0;
  state.grid = state.output = null;
  pv.seq++;                         // ignore any preview still on its way
  pv.shown = pv.seq;
  if (pv.url) URL.revokeObjectURL(pv.url);
  pv.url = null;
  $("preview-img").removeAttribute("src");
  $("clip-title").textContent = "No video selected";
  document.title = "vid2ascii";
  $("strip").style.backgroundImage = "";
  $("render-ms").textContent = "";
  setStatus(null);
  drawTimeline();
  setMode(state.result ? state.mode : "preview");
  updateRenderButton();
}

function clearResult() {
  state.result = null;
  $("result-video").pause();
  $("result-video").removeAttribute("src");
  $("result-video").load();
  $("result-gif").removeAttribute("src");
  $("download-btn").removeAttribute("href");
  $("download-btn").setAttribute("aria-disabled", "true");
  $("stage-mode").hidden = true;
  updateRenderButton();
  setMode("preview");
}
const sourceSub = (i) => `${short(i.duration)} · ${i.width}×${i.height}${i.has_audio ? "" : " · silent"}`;
const isGif = (item) => !!item && item.id.toLowerCase().endsWith(".gif");
const renderSub = (i) => `${isGif(i) ? "GIF · " : ""}${short(i.duration)} · ${i.width}×${i.height}`;

function markCurrent() {
  for (const b of document.querySelectorAll(".clip")) {
    const current = (state.mode === "result" && state.result && b.dataset.id === state.result.id)
      || (b.closest("#library-list, #uploads-list") && state.clip && b.dataset.id === state.clip.id);
    b.setAttribute("aria-current", String(!!current));
  }
}

async function loadLibrary(selectFirst = false) {
  const list = $("library-list");
  list.replaceChildren(...[1, 2, 3].map(() => Object.assign(document.createElement("li"), { className: "skeleton" })));
  try {
    const data = await getJSON("/api/library");
    const folder = data.folder || "";
    $("library-path").textContent = folder.split(/[\\/]/).filter(Boolean).slice(-1)[0] || folder;
    $("library-path").title = folder;
    list.replaceChildren(...data.items.map((i) => clipButton(i, sourceSub(i), selectClip)));
    const hint = $("library-hint");
    hint.hidden = !data.warning && data.items.length > 0;
    hint.textContent = data.warning || "No videos in this folder yet. Drop one above.";
    $("uploads-section").hidden = data.uploads.length === 0;
    $("uploads-list").replaceChildren(...data.uploads.map((i) => clipButton(i, sourceSub(i), selectClip)));
    if (selectFirst && !state.clip) {
      const first = data.items[0] || data.uploads[0];
      if (first) selectClip(first);
    }
    markCurrent();
  } catch (err) {
    list.replaceChildren();
    $("library-hint").hidden = false;
    $("library-hint").textContent = `Couldn't load the library: ${err.message}`;
  }
}

async function loadRenders() {
  try {
    const data = await getJSON("/api/outputs");
    $("renders-list").replaceChildren(...data.items.slice(0, 8).map((i) => clipButton(i, renderSub(i), showResult)));
    $("renders-empty").hidden = data.items.length > 0;
    markCurrent();
  } catch (err) {
    $("renders-empty").hidden = false;
    $("renders-empty").textContent = `Couldn't load renders: ${err.message}`;
  }
}

function selectClip(item) {
  clearRangeError();
  state.clip = item;
  state.duration = item.duration || 0;
  state.start = 0;
  state.end = Math.min(state.duration, state.config.limits.max_duration);
  state.t = Math.min(state.duration * 0.25, state.end);
  state.grid = state.output = null;
  $("clip-title").textContent = item.name;
  document.title = `${item.name} · vid2ascii`;
  $("strip").style.backgroundImage = `url("${fileUrl(item.id, "strip")}")`;
  const silent = !item.has_audio;
  $("beat-switch").disabled = silent;
  $("beat-help").textContent = silent
    ? "This clip has no sound, so there are no beats to follow."
    : "Characters flash and swap on the music's beats.";
  if (silent && state.options.beat) state.options = { ...state.options, beat: false };
  syncControls();
  setStatus(state.job ? null : "ready");
  drawTimeline();
  setMode("preview");
  markCurrent();
  preview("full");
}

// ---------------------------------------------------------------- uploads
const drop = $("dropzone");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });
$("file-input").addEventListener("change", (e) => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = ""; });
// the whole window accepts a dropped video too
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer?.files[0]) upload(e.dataTransfer.files[0]); });

function upload(file) {
  const max = state.config.limits.max_upload_bytes;
  if (file.size > max) { toast(`${file.name} is larger than ${(max / 1024 ** 3).toFixed(0)} GB.`, "error"); return; }
  const bar = drop.querySelector(".upload-bar");
  const fill = bar.firstElementChild;
  bar.hidden = false;
  fill.style.width = "0%";
  drop.querySelector(".drop-main").textContent = `Uploading ${file.name}`;
  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", api("/api/upload"));
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = `${(e.loaded / e.total) * 100}%`; };
  xhr.onload = async () => {
    bar.hidden = true;
    drop.querySelector(".drop-main").textContent = "Drop a video here";
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
    if (xhr.status !== 200) { toast(data.error || `Upload failed (${xhr.status})`, "error"); return; }
    await loadLibrary();
    selectClip(data);
  };
  xhr.onerror = () => {
    bar.hidden = true;
    drop.querySelector(".drop-main").textContent = "Drop a video here";
    toast("Upload failed: the server could not be reached.", "error");
  };
  xhr.send(form);
}

// ---------------------------------------------------------------- render jobs
function setStatus(kind, text) {
  const pill = $("status-pill");
  pill.hidden = !state.clip && !kind;
  pill.className = "pill";
  if (kind === "running") { pill.classList.add("live"); pill.textContent = text; }
  else if (kind === "done") { pill.classList.add("positive"); pill.textContent = "Saved"; }
  else if (kind === "error") { pill.classList.add("negative"); pill.textContent = "Failed"; }
  else if (state.clip) { pill.textContent = "Ready"; }
}

function updateRenderButton() {
  const btn = $("render-btn");
  const busy = !!state.job;
  btn.disabled = busy || !state.clip || !validity();
  btn.textContent = busy && state.jobKind === "render" ? "Rendering…" : "Render";
  $("gif-btn").disabled = busy || !state.result || isGif(state.result);
}

async function startRender() {
  if (!validity() || state.job) return;
  try {
    const { job_id } = await getJSON("/api/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: state.clip.id, options: requestOptions() }),
    });
    watchJob(job_id, state.clip.name);
  } catch (err) {
    toast(err.message, "error");
  }
}

function watchJob(jobId, name, kind = "render") {
  state.job = jobId;
  state.jobKind = kind;
  updateRenderButton();
  $("jobbar").hidden = false;
  setProgress(0, frameCount(), 0, null, name, "queued");
  setStatus("running", "Queued");
  const events = new EventSource(api(`/api/jobs/${jobId}/events`));
  events.addEventListener("progress", (e) => {
    const d = JSON.parse(e.data);
    setProgress(d.done, d.total || frameCount(), d.fps, d.eta_s, name, d.state);
  });
  const finish = () => { events.close(); state.job = null; $("jobbar").hidden = true; updateRenderButton(); };
  events.addEventListener("done", async (e) => {
    const d = JSON.parse(e.data);
    finish();
    setStatus("done");
    await loadRenders();
    const item = { id: d.output_id, name: d.output_id.slice(4), width: d.width, height: d.height, duration: d.duration };
    state.jobKind = null;
    showResult(item);
    toast(`Saved ${item.name} to the outputs folder.`);
  });
  events.addEventListener("error", (e) => {
    if (!e.data) {
      if (events.readyState === EventSource.CLOSED) {   // the job is gone (the server restarted)
        finish();
        setStatus("error");
        toast("Lost contact with the render. The server may have restarted; nothing was saved.", "error");
      } else {
        $("job-text").textContent = "Reconnecting to the server…";
      }
      return;
    }
    finish();
    setStatus("error");
    toast(JSON.parse(e.data).message, "error");
  });
  events.addEventListener("cancelled", () => {
    finish();
    setStatus("ready");
    toast(kind === "gif" ? "GIF export cancelled. Nothing was saved." : "Render cancelled. Nothing was saved.");
  });
}

function setProgress(done, total, fps, eta, name, jobState) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  $("progress").querySelector("span").style.width = `${pct}%`;
  $("progress").setAttribute("aria-valuenow", String(pct));
  const text = $("job-text");
  text.replaceChildren();
  const strong = document.createElement("strong");
  if (jobState === "queued") {
    strong.textContent = "Waiting";
    text.append(strong, ` for the render before ${name}`);
    return;
  }
  if (state.jobKind === "gif") {
    strong.textContent = "Making the GIF";
    text.append(strong, done ? ` · ${pct}%` : " · building its colour palette");
    setStatus("running", "Making GIF");
    return;
  }
  strong.textContent = `${pct}%`;
  const eta_s = eta == null ? "" : ` · ${Math.ceil(eta)} s left`;
  text.append(strong, ` · ${done.toLocaleString()} of ${total.toLocaleString()} frames · ${fps ? fps.toFixed(1) : "–"} frames/s${eta_s}`);
  setStatus("running", `Rendering ${pct}%`);
}

$("cancel-btn").addEventListener("click", async () => {
  if (!state.job) return;
  try { await getJSON(`/api/jobs/${state.job}/cancel`, { method: "POST" }); }
  catch (err) { toast(err.message, "error"); }
});

// ---------------------------------------------------------------- result playback
function showResult(item) {
  state.result = item;
  const video = $("result-video");
  if (isGif(item)) {
    video.pause();
    $("result-gif").src = fileUrl(item.id, "video");
  } else if (!video.src.endsWith(fileUrl(item.id, "video"))) {
    video.src = fileUrl(item.id, "video");
  }
  const dl = $("download-btn");
  dl.href = fileUrl(item.id, "download");
  dl.setAttribute("aria-disabled", "false");
  $("stage-mode").hidden = false;
  updateRenderButton();
  if (item.width && item.height) {
    frameRatio = item.width / item.height;
    fitFrame();
  }
  setMode("result");
}

function setMode(mode) {
  state.mode = mode;
  const result = mode === "result" && state.result;
  $("result-video").hidden = !result || isGif(state.result);
  $("result-gif").hidden = !result || !isGif(state.result);
  $("preview-img").hidden = !!result || !pv.url;
  $("stage-empty").hidden = !!result || !!pv.url;
  $("ramp").style.visibility = result ? "hidden" : "visible";
  for (const b of $("stage-mode").querySelectorAll("button")) {
    b.setAttribute("aria-selected", String(b.dataset.mode === mode));
  }
  if (result) {
    $("render-ms").textContent = "";
    $("finding").textContent = `${state.result.name} · ${state.result.width} × ${state.result.height} · ${secs(state.result.duration)}`;
  } else {
    $("result-video").pause();
    if (state.output) setFrameAspect(state.output);
    updateFinding();
  }
  markCurrent();
}
$("stage-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.mode === "preview" && state.clip) { setMode("preview"); fitFrame(); }
  else if (b.dataset.mode === "result" && state.result) setMode("result");
});

// ---------------------------------------------------------------- control wiring
$("cols-range").addEventListener("input", (e) => setOption("cols", Number(e.target.value), { draft: true }));
$("cols-range").addEventListener("change", () => preview("full"));
let colsTimer = null;
$("cols-num").addEventListener("input", (e) => {
  clearTimeout(colsTimer);
  colsTimer = setTimeout(() => {
    const [lo, hi] = state.config.limits.cols;
    const v = Math.round(Number(e.target.value));
    if (Number.isFinite(v) && v >= lo && v <= hi) setOption("cols", v);
  }, 200);
});
let charsetTimer = null;
$("charset-custom").addEventListener("input", (e) => {
  clearTimeout(charsetTimer);
  charsetTimer = setTimeout(() => {
    const v = e.target.value;
    if (!v) { setOption("charset", "standard"); return; }
    if ([...v].length < 2) { validity(); updateRenderButton(); return; }
    state.options = { ...state.options, charset: v };
    markSegment($("charset-seg"), "");
    renderRamp();
    preview("full");
  }, 200);
});
$("contrast-range").addEventListener("input", (e) => setOption("contrast", e.target.value, { draft: true }));
$("aspect-select").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v === "custom") {
    $("aspect-custom").hidden = false;
    $("aspect-custom").focus();
    validity();
    updateRenderButton();
    return;
  }
  $("aspect-custom").hidden = true;
  setOption("aspect", v === "source" ? null : v);
});
let aspectTimer = null;
$("aspect-custom").addEventListener("input", (e) => {
  clearTimeout(aspectTimer);
  aspectTimer = setTimeout(() => {
    const v = e.target.value.trim().replace(/\s+/g, "").replace("×", "x");
    if (/^\d+x\d+$/i.test(v)) {
      state.options = { ...state.options, aspect: v };
      preview("full");
    } else {
      validity();
      updateRenderButton();
    }
  }, 200);
});
let fpsTimer = null;
$("fps-input").addEventListener("input", (e) => {
  clearTimeout(fpsTimer);
  fpsTimer = setTimeout(() => {
    const v = Number(e.target.value);
    if (v > 0 && v <= 120) setOption("fps", v);
    else { validity(); updateRenderButton(); }
  }, 200);
});
$("beat-switch").addEventListener("change", (e) => setOption("beat", e.target.checked));
$("reset-btn").addEventListener("click", () => {
  state.options = { ...state.config.defaults };
  if (state.clip) {
    state.start = 0;
    state.end = Math.min(state.duration, state.config.limits.max_duration);
    drawTimeline();
  }
  syncControls();
  preview("full");
});
$("render-btn").addEventListener("click", startRender);

const GIF_LABELS = { small: "Small", medium: "Medium", full: "Full size" };
let gifChoice = "small";
function buildGifOptions() {
  const box = $("gif-options");
  box.replaceChildren();
  for (const [name, { width, fps }] of Object.entries(state.config.gif_sizes)) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "gif-option";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(name === gifChoice));
    const w = width || state.result.width;
    const h = Math.round((state.result.height / state.result.width) * w);
    const label = document.createElement("strong");
    label.textContent = GIF_LABELS[name] || name;
    const detail = document.createElement("span");
    detail.className = "gif-detail";
    detail.textContent = `${w} × ${h} · ${fps} fps`;
    b.append(label, detail);
    b.addEventListener("click", () => {
      gifChoice = name;
      for (const o of box.children) o.setAttribute("aria-checked", String(o === b));
    });
    box.append(b);
  }
}
$("gif-btn").addEventListener("click", () => {
  if (!state.result || isGif(state.result) || state.job) return;
  gifChoice = "small";
  buildGifOptions();
  const dialog = $("gif-dialog");
  dialog.returnValue = "cancel";
  dialog.showModal();
});
$("gif-dialog").addEventListener("close", async () => {
  if ($("gif-dialog").returnValue !== "ok" || !state.result) return;
  const source = state.result;
  try {
    const { job_id } = await getJSON("/api/export/gif", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: source.id, size: gifChoice }),
    });
    watchJob(job_id, source.name, "gif");
  } catch (err) {
    toast(err.message, "error");
  }
});
$("download-btn").addEventListener("click", (e) => {
  if ($("download-btn").getAttribute("aria-disabled") === "true") e.preventDefault();
});

// keyboard: space plays the result; arrows move the playhead a frame, shift+arrows a second
window.addEventListener("keydown", (e) => {
  const typing = e.target.closest("input, select, textarea");
  if (typing && !(e.target.type === "range")) return;
  if (e.key === " " && state.mode === "result" && state.result && !isGif(state.result)) {
    e.preventDefault();
    const v = $("result-video");
    if (v.paused) v.play(); else v.pause();
  } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && state.clip && e.target.type !== "range") {
    e.preventDefault();
    const step = e.shiftKey ? 1 : 1 / state.options.fps;
    state.t = Math.min(state.duration, Math.max(0, state.t + (e.key === "ArrowLeft" ? -step : step)));
    drawTimeline();
    preview("draft");
    settleFull(150);
  }
});

// ---------------------------------------------------------------- start
async function init() {
  try {
    state.config = await getJSON("/api/options");
  } catch (err) {
    toast(`The server did not answer: ${err.message}`, "error");
    return;
  }
  state.options = { ...state.config.defaults };
  buildSettings();
  setStatus(null);
  updateRenderButton();
  await Promise.all([loadLibrary(true), loadRenders()]);
}
init();
