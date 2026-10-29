import { StreamHub } from "./sse.js";

const $ = (sel) => document.querySelector(sel);
const STAGES = [
  ["download", "下载"],
  ["asr", "识别"],
  ["translate", "翻译"],
  ["render", "配音"],
];
const TERMINAL = new Set(["done", "failed", "canceled"]);

const state = {
  health: null,
  file: null,
  bgmFile: null,
  url: "",
  meta: null,
  voice: localStorage.getItem("vd.voice") || "zh-CN-XiaoxiaoNeural",
  jobs: [],
  live: {},
  busy: false,
  scope: "single",   // single = 只做这一集；playlist = 整条合集
  limit: 0,          // 合集只处理前 N 集，0 = 全部
};

const hub = new StreamHub({
  onEvent: (id, ev) => onStreamEvent(id, ev),
  onClose: () => refreshJobs(),
});

/* ------------------------------------------------------------------ 工具 */
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
};

const fmtSize = (b) => {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let n = b;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

let toastTimer = null;
function toast(msg, ms = 3200) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch { /* 忽略 */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

/* ------------------------------------------------------------------ 初始化 */
async function init() {
  bindEvents();
  await loadHealth();
  await refreshJobs();
  setInterval(() => {
    if (document.hidden) return;
    if (state.jobs.some((j) => j.state === "running" || j.state === "queued")) {
      refreshJobs();
    }
  }, 6000);
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    renderEnv(h);
    renderVoices(h.voices || []);
    if (h.first_run) showModelBanner(h);
    // 后端进程比源码旧：页面是新刷的，功能却还是老的。这条比「要下模型」更急，
    // 直接盖掉它——不说明白的话，用户只会觉得「改了怎么没生效」。
    if (h.code_stale) showStaleBanner();
  } catch (e) {
    $("#pill-ffmpeg").textContent = "后端未就绪";
    $("#pill-ffmpeg").className = "pill err";
  }
}

function showStaleBanner() {
  $("#model-banner-text").textContent =
    "程序代码已更新，但当前运行的还是旧版本，新功能不会出现。关掉窗口后重新打开即可生效。";
  $("#model-banner").hidden = false;
  $("#model-banner").classList.add("stale");
}

function renderEnv(h) {
  const gpu = $("#pill-gpu");
  if (h.gpu) {
    gpu.textContent = `${h.gpu.replace(/NVIDIA\s*/i, "")} · ${h.vram_gb}G`;
    gpu.className = "pill ok";
  } else {
    gpu.textContent = "CPU 模式（较慢）";
    gpu.className = "pill warn";
  }

  const ff = $("#pill-ffmpeg");
  ff.textContent = h.ffmpeg_ok ? "ffmpeg ✓" : "ffmpeg ✗";
  ff.className = `pill ${h.ffmpeg_ok ? "" : "err"}`;
  ff.title = h.ffmpeg_detail || "";

  const md = $("#pill-model");
  const asr = h.models?.asr;
  const mt = h.models?.mt;
  if (asr?.cached) {
    md.textContent = `${asr.name} ✓`;
    md.className = "pill ok";
    md.title = mt?.cached ? "识别与翻译模型均已缓存" : "翻译走 API 或需下载本地模型";
  } else {
    md.textContent = `需下载 ${asr?.size_gb ?? 2.9}G`;
    md.className = "pill warn";
    md.title = "首次使用需下载识别模型";
  }

  $("#queue").textContent = h.queue_depth
    ? `队列 ${h.queue_depth}/${h.queue_max}`
    : "队列空闲";
}

function showModelBanner(h) {
  const asr = h.models?.asr;
  const mt = h.models?.mt;
  const parts = [];
  if (!asr?.cached) parts.push(`识别模型 ${asr?.size_gb ?? 2.9} GB`);
  if (!mt?.cached) parts.push(`翻译模型 ${mt?.size_gb ?? 5.8} GB`);
  if (!parts.length) return;
  $("#model-banner-text").textContent =
    `首次使用需下载 ${parts.join(" + ")}（仅一次）。用 API 翻译可省掉翻译模型。`;
  $("#model-banner").hidden = false;
}

function renderVoices(voices) {
  const box = $("#voices");
  if (!voices.length) return;
  box.innerHTML = voices
    .map(
      (v) => `<button class="voice${v.id === state.voice ? " active" : ""}"
        data-voice="${esc(v.id)}" title="${esc(v.desc || "")}">
        <span class="play" data-play="${esc(v.id)}" title="试听">▶</span>
        <span class="nm">${esc(v.name)}</span>
        <span class="gd">${esc(v.gender)}${v.experimental ? " ·实验" : ""}</span>
      </button>`
    )
    .join("");
}

/* ------------------------------------------------------------------ 事件绑定 */
function bindEvents() {
  const dz = $("#dropzone");
  const fileInput = $("#file-input");

  dz.addEventListener("click", () => fileInput.click());
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  ["dragenter", "dragover"].forEach((t) =>
    dz.addEventListener(t, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((t) =>
    dz.addEventListener(t, (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) pickFile(f);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files?.[0]) pickFile(fileInput.files[0]);
  });

  $("#url-input").addEventListener("input", (e) => {
    state.url = e.target.value.trim();
    state.meta = null;
    renderScope(null);
    updateSubmitState();
  });
  $("#url-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") probeUrl();
  });
  $("#btn-probe").addEventListener("click", probeUrl);

  $("#scope").addEventListener("change", (e) => {
    if (e.target.name === "scope") {
      state.scope = e.target.value;
      $("#scope-limit-wrap").hidden = state.scope !== "playlist";
      // 集数超过单次上限时替用户把上限填好。否则按钮上写着「· 150 集」，
      // 点下去只会被服务端打回一条错误——白等一轮。填好之后按钮改写成
      // 「· 100 集」，提交的就是眼前这个数字，所见即所得。
      const cap = state.meta?.max_batch || 100;
      const count = state.meta?.count || 0;
      if (state.scope === "playlist" && count > cap && !state.limit) {
        $("#scope-limit").value = String(cap);
        state.limit = cap;
      }
      updateSubmitState();
    }
  });
  $("#scope-limit").addEventListener("input", (e) => {
    const n = parseInt(e.target.value, 10);
    state.limit = Number.isFinite(n) && n > 0 ? n : 0;
    updateSubmitState();
  });

  $("#voices").addEventListener("click", (e) => {
    const playBtn = e.target.closest("[data-play]");
    if (playBtn) {
      e.stopPropagation();
      previewVoice(playBtn.dataset.play);
      return;
    }
    const card = e.target.closest("[data-voice]");
    if (!card) return;
    state.voice = card.dataset.voice;
    localStorage.setItem("vd.voice", state.voice);
    document.querySelectorAll(".voice").forEach((el) =>
      el.classList.toggle("active", el.dataset.voice === state.voice)
    );
  });

  const rate = $("#p-rate");
  rate.addEventListener("input", () => {
    const v = Number(rate.value);
    $("#rate-val").textContent = `${v >= 0 ? "+" : ""}${v}%`;
  });
  const tempo = $("#p-tempo");
  tempo.addEventListener("input", () => {
    $("#tempo-val").textContent = `${(Number(tempo.value) / 100).toFixed(2)}×`;
  });
  const offset = $("#p-offset");
  offset.addEventListener("input", () => {
    $("#offset-val").textContent = `${(Number(offset.value) / 10).toFixed(1)}s`;
  });

  $("#p-bgm").addEventListener("change", (e) => {
    if (e.target.value === "__file__") {
      $("#bgm-input").click();
    } else {
      state.bgmFile = null;
    }
  });
  $("#bgm-input").addEventListener("change", (e) => {
    state.bgmFile = e.target.files?.[0] || null;
    if (state.bgmFile) toast(`伴奏文件：${state.bgmFile.name}`);
  });

  $("#btn-submit").addEventListener("click", submit);
  $("#btn-refresh").addEventListener("click", refreshJobs);
  $("#btn-banner-close").addEventListener("click", () => {
    $("#model-banner").hidden = true;
  });

  $("#job-list").addEventListener("click", onJobAction);
}

/* ------------------------------------------------------------------ 输入 */
function pickFile(file) {
  state.file = file;
  state.url = "";
  $("#url-input").value = "";
  state.meta = null;
  renderScope(null);
  renderSummary({
    kind: "file",
    title: file.name,
    size: file.size,
  });
  updateSubmitState();
}

async function probeUrl() {
  const url = $("#url-input").value.trim();
  if (!url) return;
  state.file = null;
  state.url = url;
  const btn = $("#btn-probe");
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const meta = await api("/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!meta.ok) {
      toast(meta.error || "解析失败");
      renderSummary({ kind: "url", title: url, error: meta.error });
      renderScope(null);
    } else {
      state.meta = meta;
      renderSummary({ kind: "url", ...meta });
      renderScope(meta);
    }
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "读取";
    updateSubmitState();
  }
}

/* 合集选择块：只有探测到合集时出现 */
function renderScope(meta) {
  const box = $("#scope");
  if (!meta?.ok || !meta.is_playlist || !meta.count) {
    box.hidden = true;
    state.scope = "single";
    state.limit = 0;
    $("#scope-limit").value = "";
    return;
  }

  box.hidden = false;
  $("#scope-name").textContent = meta.playlist_title || "未命名合集";
  $("#scope-count").textContent = `共 ${meta.count} 集`;

  const cur = meta.entries?.find((e) => e.index === meta.current_index);
  const curDur = cur?.duration || meta.duration || 0;
  $("#scope-single-meta").textContent = curDur
    ? `当前这一集 · ${fmtDur(curDur)}`
    : "当前打开的这一集";

  const total = meta.total_duration || 0;
  $("#scope-all-meta").textContent = total
    ? `${meta.count} 集 · 共 ${fmtDur(total)} · 约 ${fmtEta(total * 0.33)}`
    : `${meta.count} 集 · 逐集依次处理`;

  // 超过服务端单次上限，或集数很多时，提醒分批
  const cap = meta.max_batch || 100;
  const tip = $("#scope-tip");
  const tips = [];
  if (meta.count > cap) tips.push(`单次最多提交 ${cap} 集，请填「最多处理前 ${cap} 集」分批来`);
  else if (total > 3 * 3600) tips.push("合集较长，会按集依次处理，可以先去忙别的");
  tip.textContent = tips.join("；");
  tip.hidden = tips.length === 0;
  $("#scope-limit").max = String(cap);

  state.scope = "single";
  state.limit = 0;
  $("#scope-limit").value = "";
  $("#scope-limit-wrap").hidden = true;
  box.querySelector('input[value="single"]').checked = true;
}

const fmtEta = (sec) => {
  if (!sec) return "—";
  const m = Math.round(sec / 60);
  if (m < 60) return `${Math.max(m, 1)} 分钟`;
  const h = Math.floor(m / 60);
  return `${h} 小时${m % 60 ? ` ${m % 60} 分` : ""}`;
};

function renderSummary(info) {
  const box = $("#summary");
  const rows = [];
  if (info.title) rows.push(["标题", info.title]);
  if (info.duration) rows.push(["时长", fmtDur(info.duration)]);
  if (info.size) rows.push(["体积", fmtSize(info.size)]);
  if (info.uploader) rows.push(["来源", info.uploader]);
  if (info.is_playlist) rows.push(["合集", info.playlist_title || "是"]);

  if (info.duration) {
    const eta = Math.max(20, Math.round(info.duration * 0.33));
    rows.push(["预计耗时", `${Math.ceil(eta / 60)} 分钟`]);
  }
  if (info.error) {
    rows.push(["状态", `<span class="warnline">${esc(info.error)}</span>`]);
  }

  box.innerHTML = rows
    .map(
      ([k, v]) =>
        `<div class="row"><span class="k">${esc(k)}</span><span class="v">${
          k === "状态" ? v : esc(v)
        }</span></div>`
    )
    .join("");
  box.hidden = rows.length === 0;
}

function scopeCount() {
  const total = state.meta?.count || 0;
  return state.limit > 0 ? Math.min(state.limit, total) : total;
}

function submitLabel() {
  if (state.meta?.is_playlist && state.scope === "playlist") {
    return `开始配音 · ${scopeCount()} 集`;
  }
  return "开始配音";
}

function updateSubmitState() {
  const ready = Boolean(state.file || state.url);
  const btn = $("#btn-submit");
  btn.disabled = !ready || state.busy;
  if (!state.busy) btn.textContent = submitLabel();
  $("#submit-hint").textContent = state.busy
    ? "处理中…"
    : !ready
    ? "先拖入文件或粘贴链接"
    : state.meta?.is_playlist && state.scope === "playlist"
    ? `会按顺序处理 ${scopeCount()} 集，队列里可以随时取消`
    : "准备好就开始";
}

function collectParams() {
  const p = {
    voice: state.voice,
    rate: `${Number($("#p-rate").value) >= 0 ? "+" : ""}${Number($("#p-rate").value)}%`,
    max_tempo: Number($("#p-tempo").value) / 100,
    offset: Number($("#p-offset").value) / 10,
    asr_model: $("#p-asr").value,
    mt_backend: $("#p-mt").value,
  };
  return p;
}

/* ------------------------------------------------------------------ 提交 */
function resetForm() {
  state.file = null;
  state.bgmFile = null;
  state.url = "";
  state.meta = null;
  state.scope = "single";
  state.limit = 0;
  $("#url-input").value = "";
  $("#summary").hidden = true;
  $("#file-input").value = "";
  renderScope(null);
}

function submit() {
  if (state.busy) return;
  const fd = new FormData();
  if (state.file) fd.append("file", state.file);
  else fd.append("url", state.url);
  if (state.meta?.title) fd.append("title", state.meta.title);
  fd.append("params", JSON.stringify(collectParams()));
  if (state.meta?.is_playlist) {
    fd.append("scope", state.scope);
    fd.append("limit", String(state.scope === "playlist" ? state.limit : 0));
  }
  if (state.bgmFile) fd.append("bgm", state.bgmFile);

  state.busy = true;
  updateSubmitState();
  const btn = $("#btn-submit");
  btn.textContent = "上传中…";

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      btn.textContent = `上传 ${pct}%`;
    }
  };
  xhr.onload = () => {
    state.busy = false;
    btn.textContent = "开始配音";
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const data = JSON.parse(xhr.responseText);
        resetForm();
        if (data.jobs?.length) {
          toast(`已加入队列：${data.count} 集`);
          // 只给第一集挂进度流：一百集就是一百条 SSE 连接，浏览器扛不住。
          // 后面的集数靠轮询，轮到谁跑时刷新逻辑会自动给它连上。
          hub.ensure(data.jobs[0].id);
        } else {
          toast("已加入队列");
          if (data.id) hub.ensure(data.id);
        }
        updateSubmitState();
        refreshJobs();
      } catch {
        toast("返回内容异常");
      }
    } else {
      let msg = `提交失败（${xhr.status}）`;
      try {
        msg = JSON.parse(xhr.responseText).detail || msg;
      } catch { /* 忽略 */ }
      toast(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    updateSubmitState();
  };
  xhr.onerror = () => {
    state.busy = false;
    btn.textContent = "开始配音";
    updateSubmitState();
    toast("网络错误，提交失败");
  };
  xhr.send(fd);
}

/* ------------------------------------------------------------------ 任务列表 */
async function refreshJobs() {
  try {
    const jobs = await api("/api/jobs?limit=40");
    state.jobs = jobs;
    renderJobs();

    const active = jobs.filter((j) => j.state === "running");
    active.forEach((j) => hub.ensure(j.id));
    for (const id of Object.keys(state.live)) {
      if (!jobs.some((j) => j.id === id)) delete state.live[id];
    }
    const h = await api("/api/health");
    $("#queue").textContent = h.queue_depth
      ? `队列 ${h.queue_depth}/${h.queue_max}`
      : "队列空闲";
  } catch (e) {
    console.warn("刷新任务失败", e);
  }
}

function renderJobs() {
  const list = $("#job-list");
  const empty = $("#empty-state");
  if (!state.jobs.length) {
    list.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  list.innerHTML = state.jobs.map(renderCard).join("");
}

function stageChips(job) {
  return STAGES.map(([key, label], i) => {
    const live = state.live[job.id];
    let st = job.stages?.[key]?.state || "pending";
    if (live?.stages?.[key]) st = live.stages[key];
    if (st === "done" && job.stages?.[key]?.elapsed) {
      /* 保留 done 语义，耗时在别处显示 */
    }
    const cls =
      st === "done" ? "done" : st === "active" ? "active" : st === "failed" ? "failed" : "";
    const mark = st === "done" ? "✓" : st === "failed" ? "✗" : "";
    const arrow = i < STAGES.length - 1 ? '<span class="stage-arrow">›</span>' : "";
    return `<span class="stage ${cls}"><span class="dot"></span>${label}${
      mark ? " " + mark : ""
    }</span>${arrow}`;
  }).join("");
}

function renderCard(job) {
  const live = state.live[job.id];
  const pct = live?.overall ?? (job.state === "done" ? 100 : 0);
  const msg = live?.message || job.error?.message || "";
  const running = job.state === "running";
  const queued = job.state === "queued";
  const done = job.state === "done";
  const failed = job.state === "failed";

  const mediaUrl = `/api/media/${job.id}/dubbed.mp4`;
  const audioUrl = `/api/media/${job.id}/dubbed_audio.m4a`;

  const downloads = [];
  if (job.has_video) {
    downloads.push(`<a class="primary-link" href="${mediaUrl}" download>下载成片</a>`);
  }
  if (job.has_audio) {
    downloads.push(`<a href="${audioUrl}" download>只要音轨</a>`);
  }
  downloads.push(
    `<button class="ghost small" data-act="text" data-id="${job.id}">看译文</button>`,
    `<button class="ghost small" data-act="open" data-id="${job.id}">打开文件夹</button>`
  );

  const report = job.report || {};
  const reportBits = [];
  if (report.placed) reportBits.push(`铺入 <b>${report.placed}</b> 句`);
  if (report.tempo_compressed !== undefined)
    reportBits.push(`压缩 <b>${report.tempo_compressed}</b> 句`);
  if (report.drift_max !== undefined)
    reportBits.push(`最大漂移 <b>${report.drift_max}</b> 秒`);
  if (job.size_mb) reportBits.push(`<b>${job.size_mb}</b> MB`);

  const logs = live?.logs || [];

  const pl = job.playlist || {};
  const ep = pl.index
    ? `<span class="ep-tag" title="来自合集：${esc(pl.title || "")}">第 ${pl.index} 集</span>`
    : "";

  return `
  <article class="job ${job.state}" data-job="${job.id}">
    <div class="job-top">
      ${ep}
      <span class="job-title" title="${esc(job.title)}">${esc(job.title)}</span>
      <span class="job-meta">${job.duration ? fmtDur(job.duration) : ""}</span>
      <span class="badge ${job.state}">${
        queued && job.queue_position ? `排队 #${job.queue_position}` : esc(job.state_label)
      }</span>
    </div>

    <div class="stages">${stageChips(job)}</div>

    ${
      running || (queued && job.queue_position > 1)
        ? `<div class="progress"><i style="width:${pct}%"></i></div>
           <div class="progress-msg"><span>${
             queued && !running ? "等待前面的任务" : esc(msg || "处理中")
           }</span><span>${Math.round(pct)}%</span></div>`
        : ""
    }

    ${
      done && job.has_video
        ? `<video class="player" controls preload="metadata" src="${mediaUrl}"></video>
           <div class="downloads">${downloads.join("")}</div>
           ${reportBits.length ? `<div class="report">${reportBits.join(" · ")}</div>` : ""}`
        : ""
    }

    ${
      done && !job.has_video
        ? `<div class="errorbox">任务标记完成但没找到成片文件，可能被手动删除。
             <span class="hint">点「重跑配音」重新生成。</span></div>`
        : ""
    }

    ${
      failed
        ? `<div class="errorbox">${esc(job.error?.message || "任务失败")}
             ${job.error?.hint ? `<span class="hint">${esc(job.error.hint)}</span>` : ""}
           </div>`
        : ""
    }

    ${
      logs.length
        ? `<details ${failed ? "open" : ""}><summary class="hint" style="cursor:pointer">
             日志（${logs.length}）</summary>
             <div class="logbox">${logs
               .slice(-80)
               .map((l) => `<div class="${l.level || ""}">${esc(l.text)}</div>`)
               .join("")}</div></details>`
        : ""
    }

    <div class="job-actions">
      ${running || queued ? `<button class="danger" data-act="cancel" data-id="${job.id}">取消</button>` : ""}
      ${failed || (done && !job.has_video) ? `<button class="ghost small" data-act="retry" data-id="${job.id}">重跑配音</button>` : ""}
      ${!running && !queued ? `<button class="ghost small" data-act="delete" data-id="${job.id}">删除</button>` : ""}
    </div>
  </article>`;
}

async function onJobAction(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  btn.disabled = true;
  try {
    if (act === "cancel") {
      await api(`/api/jobs/${id}/cancel`, { method: "POST" });
      toast("已请求取消");
      hub.drop(id);
      await refreshJobs();
    } else if (act === "retry") {
      await api(`/api/jobs/${id}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from: "render" }),
      });
      delete state.live[id];
      toast("已排队重跑配音");
      hub.ensure(id);
      await refreshJobs();
    } else if (act === "delete") {
      if (!confirm("删除这个任务？成片和中间文件都会删掉。")) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      hub.drop(id);
      delete state.live[id];
      await refreshJobs();
    } else if (act === "open") {
      await api(`/api/open/${id}`, { method: "POST" });
    } else if (act === "text") {
      await showText(id);
    }
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function showText(jobId) {
  const data = await api(`/api/jobs/${jobId}/text?kind=dialogue`);
  const lines = (Array.isArray(data) ? data : []).map(
    (x, i) => `${String(i + 1).padStart(3, " ")}. [${fmtDur(x.start)}] ${x.zh}`
  );
  const w = window.open("", "_blank");
  if (!w) {
    toast("浏览器拦截了弹窗");
    return;
  }
  w.document.write(
    `<!doctype html><meta charset="utf-8"><title>译文</title>
     <style>body{font:14px/1.9 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     max-width:820px;margin:32px auto;padding:0 20px;white-space:pre-wrap;color:#222}</style>
     <h2>译文（${lines.length} 条）</h2><pre>${esc(lines.join("\n"))}</pre>`
  );
  w.document.close();
}

/* ------------------------------------------------------------------ 实时事件 */
function liveOf(id) {
  if (!state.live[id]) {
    state.live[id] = {
      overall: 0,
      stage: "",
      message: "",
      stages: {},
      logs: [],
      done: false,
    };
  }
  return state.live[id];
}

function onStreamEvent(id, ev) {
  const live = liveOf(id);

  if (ev.kind === "stage") {
    const st = ev.metrics?.state;
    if (st === "start") live.stages[ev.stage] = "active";
    if (st === "done") {
      live.stages[ev.stage] = "done";
      if (ev.message) live.message = ev.message;
    }
    live.overall = ev.overall_percent ?? live.overall;
  } else if (ev.kind === "progress") {
    live.stages[ev.stage] = "active";
    live.stage = ev.stage;
    live.overall = ev.overall_percent ?? live.overall;
    live.message = ev.message || live.message;
  } else if (ev.kind === "log") {
    live.logs.push({ text: ev.message, level: ev.level });
    if (live.logs.length > 200) live.logs.shift();
  } else if (ev.kind === "done") {
    live.done = true;
    live.overall = 100;
    for (const [k] of STAGES) live.stages[k] = "done";
  } else if (ev.kind === "error") {
    if (ev.metrics?.code === "CANCELLED") {
      live.message = "已取消";
    } else if (ev.stage) {
      live.stages[ev.stage] = "failed";
    }
  }

  patchCard(id, live);
  if (ev.kind === "done" || ev.kind === "error") {
    setTimeout(refreshJobs, 350);
  }
}

function patchCard(id, live) {
  const card = document.querySelector(`[data-job="${id}"]`);
  if (!card) return;

  const bar = card.querySelector(".progress > i");
  if (bar) bar.style.width = `${Math.round(live.overall)}%`;

  const msgEl = card.querySelector(".progress-msg");
  if (msgEl) {
    msgEl.children[0].textContent = live.message || "处理中";
    msgEl.children[1].textContent = `${Math.round(live.overall)}%`;
  }

  const chips = card.querySelectorAll(".stage");
  STAGES.forEach(([key], i) => {
    const st = live.stages[key];
    if (!st) return;
    const el = chips[i];
    if (!el) return;
    el.classList.remove("done", "active", "failed");
    if (st === "done" || st === "active" || st === "failed") el.classList.add(st);
  });
}

/* ------------------------------------------------------------------ 音色试听 */
let previewAudio = null;
async function previewVoice(voiceId) {
  if (!previewAudio) previewAudio = $("#preview-audio");
  try {
    previewAudio.pause();
    const res = await api("/api/voices/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: voiceId, rate: `${$("#p-rate").value >= 0 ? "+" : ""}${$("#p-rate").value}%` }),
    });
    previewAudio.src = res.url;
    await previewAudio.play();
  } catch (e) {
    toast(e.message || "试听需要联网（微软朗读接口）");
  }
}

/* ------------------------------------------------------------------ 启动 */
init();
