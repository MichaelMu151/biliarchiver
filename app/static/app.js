const titles = {
  guide: ["使用指南", "四种采集分开用。每种都能从头开始，也能续跑中断的那一次。"],
  task: ["按 UP 主采集", "完整归档，或先用「选题预览」只采标题/标签/动态再人工筛选。"],
  academic: ["学术滚雪球", "从种子视频沿相关推荐扩样本，过门禁的才写入分析库。"],
  keyword: ["关键词采样", "全站搜索同一组关键词：导出带标题的 BV 列表，并可采集动态。不自动转写。"],
  transcribe: ["按视频号采集", "只处理你列出的 BV：评论、弹幕、转写与媒体，不扩散、不过门禁。"],
  monitor: ["运行监控", "看进度和日志。中断的任务在这里续跑；换条件请回到采集页从头开始。"],
  results: ["采集结果", "浏览账号快照、视频 Markdown 和动态 OCR。"],
  corpus: ["分析数据集", "作者、视频、评论、推荐边和门禁漏斗，可导出 CSV。"],
  settings: ["设置与登录", "扫码或粘贴 Cookie，调节间隔与转写。"],
};

const KIND_LABEL = {
  archive: "按 UP 主采集",
  scout: "选题预览",
  academic: "学术滚雪球",
  keyword: "关键词采样",
  transcribe: "按视频号采集",
};

const STATUS_LABEL = {
  queued: "排队",
  running: "进行中",
  cancelling: "暂停中",
  cancelled: "已暂停",
  interrupted: "已中断",
  done: "已完成",
  error: "出错",
};

let currentJobId = null;
let eventSource = null;
let selectedRange = "1y";
let pollTimer = null;
let capabilities = null;

const $ = (id) => document.getElementById(id);

document.querySelectorAll(".nav button").forEach((btn) => {
  btn.addEventListener("click", () => showPage(btn.dataset.page));
});

function showPage(name) {
  document.querySelectorAll(".nav button").forEach((b) => {
    const active = b.dataset.page === name;
    b.classList.toggle("active", active);
    if (active) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".page").forEach((p) => {
    const on = p.id === "page-" + name;
    p.classList.toggle("active", on);
    p.hidden = !on;
  });
  $("page-title").textContent = titles[name][0];
  $("page-sub").textContent = titles[name][1];
  if (name === "results") loadResults();
  if (name === "corpus") loadCorpus();
  if (name === "settings") loadSettings();
  if (name === "task") fillResumePick("task-resume-pick", ["archive", "scout"], "task-resume-hint");
  if (name === "academic") fillResumePick("academic-resume-pick", "academic", "academic-resume-hint");
  if (name === "keyword") {
    fillResumePick("keyword-resume-pick", "keyword", "keyword-resume-hint");
    enforceKeywordChoices();
    updateKeywordHints();
  }
  if (name === "transcribe") {
    loadMissingBvids();
    fillResumePick("transcribe-resume-pick", "transcribe", "transcribe-resume-hint");
    enforceTranscribeChoices();
  }
  if (name === "monitor") loadJobHistory();
}

document.querySelectorAll("#range-pills button").forEach((btn) => {
  btn.addEventListener("click", () => {
    selectedRange = btn.dataset.range;
    document.querySelectorAll("#range-pills button").forEach((b) => b.classList.toggle("on", b === btn));
  });
});

document.querySelectorAll(".choice-card input").forEach((input) => {
  input.addEventListener("change", () => {
    syncChoiceCards();
    enforceCompatibleChoices(input.name);
    enforceAcademicChoices(input.name);
    enforceKeywordChoices(input.name);
    enforceTranscribeChoices(input.name);
    updatePlanSummary();
    updateAcademicHints();
    updateKeywordHints();
  });
});

document.querySelectorAll(".preset-card").forEach((button) => {
  button.addEventListener("click", () => applyPreset(button.dataset.preset));
});

function radioValue(name) {
  return document.querySelector(`input[name="${name}"]:checked`)?.value || "";
}

function setRadio(name, value) {
  const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (input) input.checked = true;
  syncChoiceCards();
}

function syncChoiceCards() {
  document.querySelectorAll(".choice-card").forEach((card) => {
    card.classList.toggle("selected", Boolean(card.querySelector("input:checked")));
  });
}

function applyPreset(preset) {
  document.querySelectorAll(".preset-card").forEach((b) => b.classList.toggle("on", b.dataset.preset === preset));
  if (preset === "light") {
    setRadio("media-mode", "link");
    setRadio("transcribe-mode", "official");
    setRadio("media-keep", "delete_after_text");
    $("m-ocr").checked = true;
  } else if (preset === "text") {
    setRadio("media-mode", "audio");
    setRadio("transcribe-mode", "official_then_whisper");
    setRadio("media-keep", "upload_then_delete");
    $("m-ocr").checked = true;
  } else if (preset === "archive") {
    setRadio("media-mode", "video");
    setRadio("transcribe-mode", "official_then_whisper");
    setRadio("media-keep", "upload_then_delete");
    $("m-ocr").checked = true;
  }
  updatePlanSummary();
}

function enforceCompatibleChoices(changedName) {
  const media = radioValue("media-mode");
  const transcript = radioValue("transcribe-mode");
  const needsAudio = ["whisper", "official_then_whisper"].includes(transcript);
  if (changedName === "transcribe-mode" && needsAudio && !["audio", "video"].includes(media)) {
    setRadio("media-mode", "audio");
    toast("已自动切换为“下载音频”，供 Whisper 使用");
  } else if (changedName === "media-mode" && needsAudio && !["audio", "video"].includes(media)) {
    setRadio("transcribe-mode", "official");
    toast("未下载音频时，已改为只提取官方字幕");
  }
}

function enforceAcademicChoices(changedName) {
  if (!$("academic-start")) return;
  const wantTranscript = $("ac-transcript")?.checked !== false;
  if ($("ac-transcribe-card")) $("ac-transcribe-card").hidden = !wantTranscript;
  if (!wantTranscript) return;
  const media = radioValue("ac-media-mode") || "audio";
  const transcript = radioValue("ac-transcribe-mode") || "official_then_whisper";
  const needsAudio = ["whisper", "official_then_whisper"].includes(transcript);
  if (changedName === "ac-transcribe-mode" && needsAudio && !["audio", "video"].includes(media)) {
    setRadio("ac-media-mode", "audio");
    toast("学术滚雪球已改为下载音频，才能跑 Whisper");
  } else if (changedName === "ac-media-mode" && needsAudio && !["audio", "video"].includes(media)) {
    setRadio("ac-transcribe-mode", "official");
    toast("未下载音频时，学术滚雪球改为只取官方字幕");
  }
}

function enforceKeywordChoices(_changedName) {
  if (!$("keyword-start")) return;
  const wantDyn = $("kw-crawl-dyn")?.checked !== false;
  if ($("kw-dyn-card")) $("kw-dyn-card").hidden = !wantDyn;
  if (wantDyn) updateKeywordHints();
}

function updateKeywordHints() {
  if ($("kw-comments") && $("kw-comment-mode")) {
    $("kw-comment-mode").disabled = !$("kw-comments").checked;
    $("kw-comment-pages").disabled = !$("kw-comments").checked;
  }
}

function enforceTranscribeChoices(changedName) {
  if (!$("transcribe-start")) return;
  const media = radioValue("tr-media-mode") || "video";
  const transcript = radioValue("tr-transcribe-mode") || "official_then_whisper";
  const needsAudio = ["whisper", "official_then_whisper"].includes(transcript);
  if (changedName === "tr-transcribe-mode" && needsAudio && !["audio", "video"].includes(media)) {
    setRadio("tr-media-mode", "audio");
    toast("已改为下载音频，才能跑 Whisper");
  } else if (changedName === "tr-media-mode" && needsAudio && !["audio", "video"].includes(media)) {
    setRadio("tr-transcribe-mode", "official");
    toast("未下载音频时，改为只取官方字幕");
  }
  if ($("tr-comments") && $("tr-comment-mode")) {
    $("tr-comment-mode").disabled = !$("tr-comments").checked;
    $("tr-comment-pages").disabled = !$("tr-comments").checked;
  }
}

function updateAcademicHints() {
  const hint = $("ac-compute-hint");
  if (!hint) return;
  const transcript = radioValue("ac-transcribe-mode") || "official_then_whisper";
  const compute = radioValue("ac-compute-backend") || "local";
  if (!["whisper", "official_then_whisper"].includes(transcript)) {
    hint.textContent = "只取官方字幕时不需要 GPU。";
    return;
  }
  if (compute === "cloud") {
    hint.textContent = capabilities?.gpu_worker?.ready
      ? capabilities.gpu_worker.purpose
      : "尚未接入云端 GPU：到设置粘贴 AutoDL 的 SSH 指令和密码，点一键接入，测通后再选云端。";
  } else {
    hint.textContent = "本机没有 NVIDIA 时 Whisper 走 CPU，一条长视频可能要十几分钟。";
  }
}

function updatePlanSummary() {
  const media = radioValue("media-mode");
  const transcript = radioValue("transcribe-mode");
  const keep = radioValue("media-keep");
  const mediaText = {
    none: "不处理媒体",
    link: "仅保留永久页面链接",
    audio: "断点续传音频",
    video: "下载并合成完整视频",
  }[media];
  const transcriptText = {
    none: "不提取文字",
    url_only: "只写云端清单",
    official: "提取官方/AI 字幕",
    whisper: "全部使用 Whisper",
    official_then_whisper: "官方字幕优先，Whisper 兜底",
  }[transcript];
  const compute = radioValue("compute-backend") || "local";
  const computeText = compute === "cloud" ? "云端 GPU 工作机" : "本机";
  const keepText = {
    keep: "原始媒体留在本机",
    delete_after_text: "转写后删除媒体，只留链接",
    compress: "压缩后保留",
    upload_then_delete: "上传 Google Drive 后删除本地媒体",
  }[keep];
  $("plan-title").textContent = `${mediaText} · ${transcriptText}`;
  $("plan-summary").textContent = `视频：${mediaText}；语音：${transcriptText}；算力：${computeText}；空间：${keepText}；动态图片：${$("m-ocr").checked ? "下载并尝试 OCR" : "只下载，不 OCR"}。`;
  const readiness = [];
  if (["whisper", "official_then_whisper"].includes(transcript) && compute !== "cloud") {
    readiness.push(badge(capabilities?.whisper?.ready, "Whisper"));
  }
  if ((["whisper", "official_then_whisper"].includes(transcript) || $("m-ocr").checked) && compute === "cloud") {
    readiness.push(badge(capabilities?.gpu_worker?.ready, "云端 GPU"));
  }
  if (media === "video" || keep === "compress") readiness.push(badge(capabilities?.ffmpeg?.ready, "ffmpeg"));
  if ($("m-ocr").checked && compute !== "cloud") readiness.push(badge(capabilities?.ocr?.ready, "OCR"));
  if (keep === "upload_then_delete") readiness.push(badge(capabilities?.rclone?.ready, "rclone"));
  $("task-readiness").innerHTML = readiness.join("") || '<span class="ready-badge ok">无需额外组件</span>';
  const heavy = ["whisper", "official_then_whisper"].includes(transcript) || $("m-ocr").checked;
  if ($("compute-card")) $("compute-card").hidden = !heavy;
  if ($("compute-hint")) {
    if (compute === "cloud") {
      $("compute-hint").textContent = capabilities?.gpu_worker?.ready
        ? capabilities.gpu_worker.purpose
        : "尚未激活：到设置生成 Token、在 GPU 机器启动工作机，再点测试连接。";
    } else {
      $("compute-hint").textContent = "本机没有 NVIDIA 时 Whisper 走 CPU，一条长视频可能要十几分钟。";
    }
  }
}

function badge(ready, label) {
  return `<span class="ready-badge ${ready ? "ok" : "warn"}">${label} ${ready ? "已就绪" : "将自动降级"}</span>`;
}

$("task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    uids_text: $("uids").value,
    time_range: selectedRange,
    crawl_profile: $("m-profile").checked,
    crawl_videos: $("m-videos").checked,
    crawl_dynamics: $("m-dyn").checked,
    crawl_comments: $("m-comments").checked,
    crawl_danmaku: $("m-danmaku").checked,
    ocr_enabled: $("m-ocr").checked,
    resume: false,
    media_mode: radioValue("media-mode"),
    transcribe_mode: radioValue("transcribe-mode"),
    media_keep: radioValue("media-keep"),
    compute_backend: radioValue("compute-backend") || "local",
    kind: "archive",
  };
  try {
    const res = await api("/api/jobs", { method: "POST", body });
    currentJobId = res.id;
    $("job-id").textContent = "#" + res.id;
    $("log-view").textContent = "";
    showPage("monitor");
    listenJob(res.id);
    toast("已从头开始一次按 UP 主采集");
  } catch (err) {
    toast(err.message);
  }
});

$("scout-start")?.addEventListener("click", async () => {
  const body = {
    uids_text: $("uids").value,
    time_range: selectedRange,
    kind: "scout",
  };
  try {
    const res = await api("/api/jobs", { method: "POST", body });
    currentJobId = res.id;
    $("job-id").textContent = "#" + res.id;
    $("log-view").textContent = "";
    showPage("monitor");
    listenJob(res.id);
    toast("选题预览已开始：只采标题、标签和动态");
  } catch (err) {
    toast(err.message);
  }
});

async function pauseLiveJob(kinds) {
  const list = Array.isArray(kinds) ? kinds : [kinds];
  const data = await fetchJobs();
  const job = (data.live || []).find((item) =>
    ["queued", "running", "cancelling"].includes(item.status) && list.includes(jobKind(item))
  );
  if (!job) {
    toast("当前没有在跑的这类任务");
    return null;
  }
  if (job.status === "cancelling") {
    toast("正在暂停，稍等即可");
    return job;
  }
  await api("/api/jobs/" + job.id + "/cancel", { method: "POST" });
  currentJobId = job.id;
  if ($("job-id")) $("job-id").textContent = "#" + job.id;
  toast("正在暂停，等当前这条请求结束");
  for (let i = 0; i < 24; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const latest = await api("/api/jobs/" + job.id).catch(() => null);
    const status = latest?.status || "";
    if (status && !["queued", "running", "cancelling"].includes(status)) {
      toast("已暂停。进度已保留，回来后点「续跑」即可。");
      return latest;
    }
  }
  toast("已发出暂停。若监控里仍显示进行中，是当前请求还没返回，返回后会停下。");
  return job;
}

$("cancel-btn").addEventListener("click", async () => {
  try {
    const job = await pauseLiveJob(["archive", "scout", "academic", "keyword", "transcribe"]);
    if (!job && currentJobId) {
      await api("/api/jobs/" + currentJobId + "/cancel", { method: "POST" });
      toast("已暂停。进度已保留，回来后点「续跑」即可。");
    }
  } catch (err) {
    toast(err.message);
  }
});

$("task-pause")?.addEventListener("click", async () => {
  try {
    await pauseLiveJob(["archive", "scout"]);
    fillResumePick("task-resume-pick", ["archive", "scout"], "task-resume-hint");
  } catch (err) {
    toast(err.message);
  }
});

$("scout-pause")?.addEventListener("click", async () => {
  try {
    await pauseLiveJob("scout");
    fillResumePick("task-resume-pick", ["archive", "scout"], "task-resume-hint");
  } catch (err) {
    toast(err.message);
  }
});

$("academic-pause")?.addEventListener("click", async () => {
  try {
    await pauseLiveJob("academic");
    fillResumePick("academic-resume-pick", "academic", "academic-resume-hint");
  } catch (err) {
    toast(err.message);
  }
});

$("keyword-pause")?.addEventListener("click", async () => {
  try {
    await pauseLiveJob("keyword");
    fillResumePick("keyword-resume-pick", "keyword", "keyword-resume-hint");
  } catch (err) {
    toast(err.message);
  }
});

$("transcribe-pause")?.addEventListener("click", async () => {
  try {
    await pauseLiveJob("transcribe");
    fillResumePick("transcribe-resume-pick", "transcribe", "transcribe-resume-hint");
  } catch (err) {
    toast(err.message);
  }
});

function jobKind(job) {
  return (job?.config || {}).kind || "archive";
}

function jobProgressText(job) {
  const progress = job.progress || {};
  const frontier = progress.frontier || {};
  if (frontier.done != null && (progress.max_nodes || frontier.pending != null)) {
    const max = progress.max_nodes || "";
    return max ? `${frontier.done}/${max} 已完成` : `已完成 ${frontier.done}`;
  }
  if (progress.videos_done != null) return `视频 ${progress.videos_done}`;
  return progress.stage || "";
}

function jobOptionLabel(job) {
  const status = STATUS_LABEL[job.status] || job.status || "";
  const when = (job.created_at || "").replace("T", " ").slice(0, 16);
  return `#${job.id.slice(0, 8)} · ${status}${when ? " · " + when : ""} · ${jobProgressText(job)}`;
}

async function fetchJobs() {
  return api("/api/jobs");
}

function jobsOfKind(data, kind) {
  const kinds = Array.isArray(kind) ? kind : [kind];
  const seen = new Set();
  const rows = [];
  for (const job of [...(data.live || []), ...(data.history || [])]) {
    const k = jobKind(job);
    if (!kinds.includes(k) || seen.has(job.id)) continue;
    seen.add(job.id);
    rows.push(job);
  }
  return rows.slice(0, 30);
}

async function fillResumePick(selectId, kind, hintId) {
  const select = $(selectId);
  if (!select) return;
  const kinds = Array.isArray(kind) ? kind : [kind];
  const label = kinds.map((k) => KIND_LABEL[k] || k).join(" / ");
  try {
    const data = await fetchJobs();
    const rows = jobsOfKind(data, kinds).filter((job) => !["queued", "running", "cancelling"].includes(job.status));
    select.innerHTML = rows.length
      ? rows.map((job) => `<option value="${job.id}">${escapeHtml(jobOptionLabel(job))}</option>`).join("")
      : `<option value="">还没有可续跑的${label}</option>`;
    const hint = hintId ? $(hintId) : null;
    if (hint && !rows.length) hint.textContent = "还没有保存过这类任务。先从头开始一次。";
  } catch (err) {
    select.innerHTML = `<option value="">读取任务失败</option>`;
  }
}

async function resumeExistingJob(jobId) {
  if (!jobId) {
    toast("请先选择要续跑的任务");
    return;
  }
  const res = await api("/api/jobs/" + jobId + "/resume", { method: "POST" });
  currentJobId = res.id;
  $("job-id").textContent = "#" + res.id;
  $("log-view").textContent = "";
  showPage("monitor");
  listenJob(res.id);
  toast("已从检查点续跑");
}

$("resume-btn")?.addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await resumeExistingJob(currentJobId);
  } catch (err) {
    toast(err.message);
  }
});

$("task-resume")?.addEventListener("click", async () => {
  try {
    await resumeExistingJob($("task-resume-pick")?.value || "");
  } catch (err) {
    toast(err.message);
  }
});

$("academic-resume")?.addEventListener("click", async () => {
  try {
    await resumeExistingJob($("academic-resume-pick")?.value || "");
  } catch (err) {
    toast(err.message);
  }
});

$("keyword-resume")?.addEventListener("click", async () => {
  try {
    await resumeExistingJob($("keyword-resume-pick")?.value || "");
  } catch (err) {
    toast(err.message);
  }
});

$("transcribe-resume")?.addEventListener("click", async () => {
  try {
    await resumeExistingJob($("transcribe-resume-pick")?.value || "");
  } catch (err) {
    toast(err.message);
  }
});

$("academic-start")?.addEventListener("click", async () => {
  try {
    const wantTranscript = $("ac-transcript")?.checked !== false;
    let transcribe = wantTranscript ? (radioValue("ac-transcribe-mode") || "official_then_whisper") : "none";
    let media = wantTranscript ? (radioValue("ac-media-mode") || "audio") : "link";
    if (["whisper", "official_then_whisper"].includes(transcribe) && !["audio", "video"].includes(media)) {
      media = "audio";
    }
    const res = await api("/api/jobs/academic", {
      method: "POST",
      body: {
        seeds_text: $("academic-seeds").value,
        max_depth: Number($("ac-depth").value),
        max_nodes: Number($("ac-nodes").value),
        related_limit: Number($("ac-related").value),
        min_views: Number($("ac-min-views").value),
        min_replies: Number($("ac-min-replies").value),
        min_engagement: Number($("ac-min-eng").value),
        category_allow: $("ac-allow").value,
        category_deny: $("ac-deny").value,
        tag_terms: $("ac-tags").value,
        keyword: $("ac-keyword").value,
        time_range: $("ac-range").value,
        seeds_per_uid: Number($("ac-per-uid").value),
        crawl_comments: $("ac-comments").checked,
        crawl_danmaku: $("ac-danmaku").checked,
        transcribe_mode: transcribe,
        media_mode: media,
        media_keep: radioValue("ac-media-keep") || "delete_after_text",
        compute_backend: radioValue("ac-compute-backend") || "local",
        resume: false,
      },
    });
    currentJobId = res.id;
    $("job-id").textContent = "#" + res.id;
    $("log-view").textContent = "";
    showPage("monitor");
    listenJob(res.id);
    toast("已从头开始一次学术滚雪球");
  } catch (err) {
    toast(err.message);
  }
});

$("keyword-start")?.addEventListener("click", async () => {
  try {
    if (!$("kw-export-videos")?.checked && !$("kw-crawl-dyn")?.checked) {
      toast("请至少勾选「导出视频列表」或「采集动态」");
      return;
    }
    const res = await api("/api/jobs/keyword", {
      method: "POST",
      body: {
        keywords_text: $("kw-keywords").value,
        title_must_terms: $("kw-title-terms").value,
        title_match_mode: $("kw-title-mode").value || "any",
        date_from: $("kw-date-from").value || "",
        date_to: $("kw-date-to").value || "",
        order: $("kw-order").value || "pubdate",
        duration: Number($("kw-duration").value || 0),
        max_pages_per_keyword: Number($("kw-pages").value || 30),
        max_nodes: Number($("kw-nodes").value || 200),
        min_views: Number($("kw-min-views").value || 0),
        min_likes: Number($("kw-min-likes").value || 0),
        min_danmaku: Number($("kw-min-danmaku").value || 0),
        min_replies: Number($("kw-min-replies").value || 0),
        min_engagement: Number($("kw-min-eng").value || 0),
        category_allow: $("kw-allow").value,
        category_deny: $("kw-deny").value,
        export_video_list: Boolean($("kw-export-videos")?.checked),
        crawl_dynamics: Boolean($("kw-crawl-dyn")?.checked),
        dyn_min_likes: Number($("kw-dyn-likes")?.value || 0),
        dyn_min_comments: Number($("kw-dyn-comments")?.value || 0),
        dyn_min_forwards: Number($("kw-dyn-forwards")?.value || 0),
        crawl_comments: Boolean($("kw-comments")?.checked),
        comment_with_replies: ($("kw-comment-mode")?.value || "tree") === "tree",
        comment_max_pages: Number($("kw-comment-pages")?.value || 0),
        ocr_enabled: Boolean($("kw-ocr")?.checked),
        compute_backend: $("kw-compute-backend")?.value || "local",
        resume: false,
      },
    });
    currentJobId = res.id;
    $("job-id").textContent = "#" + res.id;
    $("log-view").textContent = "";
    showPage("monitor");
    listenJob(res.id);
    toast("已从头开始一次关键词采样");
  } catch (err) {
    toast(err.message);
  }
});

async function loadMissingBvids() {
  const hint = $("transcribe-missing-hint");
  try {
    const data = await api("/api/jobs/transcribe/missing?run_id=5ba1f9a96259");
    if (hint) {
      hint.textContent = data.count
        ? `学术滚雪球里还有 ${data.count} 条过门但没台词。点按钮填入后，用「从头开始」新开一批。`
        : "学术滚雪球里，过门视频都已有台词。仍可自己粘贴 BV，再从头开始。";
    }
    return data;
  } catch (err) {
    if (hint) hint.textContent = "读未完成列表失败：" + err.message;
    return null;
  }
}

$("transcribe-fill-missing")?.addEventListener("click", async () => {
  const data = await loadMissingBvids();
  if (!data) return;
  $("transcribe-bvids").value = data.text || "";
  toast(data.count ? `已填入 ${data.count} 个 BV` : "没有待补的 BV");
});

$("transcribe-start")?.addEventListener("click", async () => {
  try {
    const transcribeMode = radioValue("tr-transcribe-mode") || "official_then_whisper";
    let media = radioValue("tr-media-mode") || "video";
    if (["whisper", "official_then_whisper"].includes(transcribeMode) && !["audio", "video"].includes(media)) {
      media = "audio";
    }
    const res = await api("/api/jobs/transcribe", {
      method: "POST",
      body: {
        bvids_text: $("transcribe-bvids").value,
        transcribe_mode: transcribeMode,
        media_mode: media,
        media_keep: radioValue("tr-media-keep") || "upload_then_delete",
        resume: Boolean($("tr-resume")?.checked),
        compute_backend: radioValue("tr-compute-backend") || "cloud",
        crawl_comments: Boolean($("tr-comments")?.checked),
        comment_with_replies: ($("tr-comment-mode")?.value || "tree") === "tree",
        comment_max_pages: Number($("tr-comment-pages")?.value || 0),
        crawl_danmaku: Boolean($("tr-danmaku")?.checked),
        ocr_enabled: Boolean($("tr-ocr")?.checked),
        attach_run_id: "",
      },
    });
    currentJobId = res.id;
    $("job-id").textContent = "#" + res.id;
    $("log-view").textContent = "";
    showPage("monitor");
    listenJob(res.id);
    toast("已从头开始一次按视频号采集");
  } catch (err) {
    toast(err.message);
  }
});

$("refresh-results").addEventListener("click", loadResults);
$("refresh-capabilities").addEventListener("click", loadCapabilities);
$("copy-install").addEventListener("click", async () => {
  const command = capabilities?.pip_command || "pip install -r requirements-ai.txt";
  try {
    await navigator.clipboard.writeText(command);
    toast("安装命令已复制");
  } catch (_) {
    toast(command);
  }
});
$("qr-btn").addEventListener("click", startQr);
$("save-cookie").addEventListener("click", async () => {
  await api("/api/settings", { method: "PUT", body: { cookie: $("cookie-input").value } });
  $("cookie-input").value = "";
  toast("Cookie 已保存");
  refreshNav();
});
$("save-settings").addEventListener("click", async () => {
  await api("/api/settings", {
    method: "PUT",
    body: {
      min_interval: Number($("min-interval").value),
      max_interval: Number($("max-interval").value),
      max_retries: Number($("max-retries").value),
      comment_max_pages: Number($("comment-pages").value),
      whisper_model: $("whisper-model").value,
      whisper_language: $("whisper-language").value,
      whisper_device: $("whisper-device").value,
      whisper_compute_type: $("whisper-compute").value,
      ocr_min_confidence: Number($("ocr-confidence").value),
      video_quality: Number($("video-quality").value),
      rclone_remote: $("rclone-remote").value,
      rclone_root: $("rclone-root").value,
      gpu_worker_url: $("gpu-worker-url").value,
      autodl_ssh_command: $("autodl-ssh")?.value || "",
      compute_backend: radioValue("compute-backend") || "local",
      bark_enabled: $("bark-enabled").checked,
      bark_title: $("bark-title").value,
      bark_sound: $("bark-sound").value || "bell",
    },
  });
  const gpuToken = $("gpu-worker-token").value.trim();
  if (gpuToken) {
    await api("/api/settings", { method: "PUT", body: { gpu_worker_token: gpuToken } });
    $("gpu-worker-token").value = "";
  }
  const barkKey = $("bark-key").value.trim();
  if (barkKey) {
    await api("/api/settings", { method: "PUT", body: { bark_key: barkKey } });
    $("bark-key").value = "";
  }
  toast("设置已保存");
});

async function api(url, opts = {}) {
  const res = await fetch(url, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || "请求失败");
  return data;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.style.display = "block";
  setTimeout(() => (el.style.display = "none"), 2600);
}

function listenJob(id) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource("/api/jobs/" + id + "/events");
  ["snapshot", "log", "progress", "status", "done"].forEach((type) => {
    eventSource.addEventListener(type, (ev) => {
      const data = JSON.parse(ev.data);
      if (type === "log") appendLog(data);
      if (type === "progress" || type === "snapshot") applyProgress(data.progress || data);
      if (type === "snapshot" && data.logs) data.logs.forEach(appendLog);
      if (type === "done") {
        applyProgress(data.progress || {});
        $("m-status").textContent = translateStatus(data.status);
        $("status-text").textContent = data.status === "done" ? "采集完成" : translateStatus(data.status);
        $("status-dot").className = `status-dot ${data.status === "error" ? "error" : data.status === "cancelled" ? "warn" : ""}`;
        if (data.error) appendLog({ level: "error", message: data.error });
        eventSource.close();
      }
    });
  });
}

function appendLog(item) {
  const line = document.createElement("div");
  line.className = item.level || "info";
  line.textContent = `[${item.level || "info"}] ${item.message}`;
  $("log-view").appendChild(line);
  $("log-view").scrollTop = $("log-view").scrollHeight;
}

function applyProgress(p) {
  if (!p) return;
  const transcribe = p.kind === "transcribe";
  const keyword = p.kind === "keyword";
  const academic = p.kind === "academic" || (!transcribe && !keyword && Boolean(p.max_nodes));
  const listed = transcribe || academic || keyword;
  const total = listed ? (p.max_nodes || 1) : (p.uids_total || 1);
  const done = listed ? (p.nodes_done || p.videos_done || 0) : (p.uids_done || 0);
  if ($("m-uids-label")) {
    $("m-uids-label").textContent = transcribe ? "进度" : keyword ? "节点" : academic ? "节点" : "账号";
  }
  if ($("m-videos-label")) {
    $("m-videos-label").textContent = transcribe ? "完成" : keyword ? "列表" : academic ? "通过" : "视频";
  }
  if ($("m-dyn-label")) {
    $("m-dyn-label").textContent = transcribe ? "失败" : keyword ? "动态" : academic ? "剪枝" : "动态";
  }
  $("m-status").textContent = translateStatus(p.stage || "running");
  $("m-uids").textContent = `${done} / ${total}`;
  $("m-videos-n").textContent = listed ? (p.passed || p.videos_done || p.candidates || 0) : (p.videos_done || 0);
  $("m-dyn-n").textContent = keyword ? (p.dynamics_done || 0) : listed ? (p.pruned || 0) : (p.dynamics_done || 0);
  const extra = keyword && p.searched
    ? ` · 已搜 ${p.searched} · 候选 ${p.candidates || 0}` + (p.keyword_export ? ` · ${p.keyword_export}` : "")
    : "";
  $("m-current").textContent = (p.current || "") + extra;
  const percent = Math.min(100, (done / total) * 100);
  $("progress-bar").style.width = percent + "%";
  $("progress-wrap").setAttribute("aria-valuenow", String(Math.round(percent)));
  $("status-text").textContent = translateStatus(p.stage || "running");
}

function translateStatus(status) {
  return {
    queued: "排队中",
    running: "运行中",
    bootstrap: "准备会话",
    account: "读取账号",
    done: "完成",
    cancelled: "已取消",
    cancelling: "正在取消",
    error: "失败",
  }[status] || status;
}

async function loadResults() {
  const data = await api("/api/results");
  const box = $("account-list");
  box.innerHTML = "";
  if (!data.accounts.length) {
    box.innerHTML = '<p class="muted">还没有采集结果。</p>';
    return;
  }
  data.accounts.forEach((acc) => {
    const btn = document.createElement("button");
    btn.className = "account-item";
    btn.innerHTML = `<b>${escapeHtml(acc.name || acc.mid)}</b><span class="muted">UID ${escapeHtml(acc.mid)} · 粉丝 ${acc.follower ?? "-"}</span>`;
    btn.onclick = () => {
      document.querySelectorAll(".account-item").forEach((x) => x.classList.remove("on"));
      btn.classList.add("on");
      showAccount(acc.mid);
    };
    box.appendChild(btn);
  });
}

async function showAccount(mid) {
  const data = await api("/api/results/" + mid);
  const acc = data.account || {};
  const snaps = data.snapshots || [];
  const latest = snaps[0] || {};
  const videos = (data.videos || [])
    .slice(0, 40)
    .map((v) => {
      const assets = [
        v.local_video ? '<span class="asset-badge">视频</span>' : "",
        v.local_audio ? '<span class="asset-badge">音频</span>' : "",
        v.transcript_path ? '<span class="asset-badge text">文字</span>' : "",
      ].join("");
      return `<li class="result-row"><a href="#" data-file="${v.markdown_path || ""}">${escapeHtml(v.title || v.bvid)}</a><small>播放 ${v.view ?? "-"} ${assets}</small></li>`;
    })
    .join("");
  const dyns = (data.dynamics || [])
    .slice(0, 40)
    .map((d) => `<li><a href="#" data-file="${d.markdown_path || ""}">${escapeHtml((d.text || d.dyn_id || "").slice(0, 48))}</a></li>`)
    .join("");
  $("result-detail").innerHTML = `
    <h3>${escapeHtml(acc.name || mid)}</h3>
    <p class="muted"><a href="${acc.space_url}" target="_blank">${acc.space_url}</a></p>
    <div class="metrics">
      <div><span>粉丝</span><b>${latest.follower ?? "-"}</b></div>
      <div><span>关注</span><b>${latest.following ?? "-"}</b></div>
      <div><span>投稿</span><b>${latest.archive_count ?? "-"}</b></div>
      <div><span>获赞</span><b>${latest.likes ?? "-"}</b></div>
    </div>
    <p class="muted">快照 ${snaps.length} 次 · 详见 stats.csv / snapshots.jsonl</p>
    <h3>视频</h3>
    <ul>${videos || "<li class='muted'>无</li>"}</ul>
    <h3>动态</h3>
    <ul>${dyns || "<li class='muted'>无</li>"}</ul>
    <pre class="code" id="file-preview"></pre>
  `;
  $("result-detail").querySelectorAll("a[data-file]").forEach((a) => {
    a.addEventListener("click", async (ev) => {
      ev.preventDefault();
      if (!a.dataset.file) return;
      const file = await api("/api/file?path=" + encodeURIComponent(a.dataset.file));
      $("file-preview").textContent = file.content;
    });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadSettings() {
  const s = await api("/api/settings");
  $("min-interval").value = s.min_interval;
  $("max-interval").value = s.max_interval;
  $("max-retries").value = s.max_retries;
  $("comment-pages").value = s.comment_max_pages;
  $("whisper-model").value = s.whisper_model;
  $("whisper-language").value = s.whisper_language || "auto";
  $("whisper-device").value = s.whisper_device || "auto";
  $("whisper-compute").value = s.whisper_compute_type || "auto";
  $("ocr-confidence").value = s.ocr_min_confidence ?? 0.55;
  $("video-quality").value = String(s.video_quality);
  $("rclone-remote").value = s.rclone_remote || "gdrive";
  $("rclone-root").value = s.rclone_root || "BiliArchiver";
  $("gpu-worker-url").value = s.gpu_worker_url || "";
  $("gpu-worker-preview").textContent = s.has_gpu_worker_token
    ? `Token 已保存 ${s.gpu_worker_token_preview}`
    : "尚未填写 Token";
  if ($("autodl-ssh") && s.autodl_ssh_command) $("autodl-ssh").value = s.autodl_ssh_command;
  if ($("autodl-preview")) {
    const bits = [];
    if (s.autodl_ssh_command) bits.push("SSH 指令已保存");
    if (s.has_autodl_password) bits.push(`密码 ${s.autodl_password_preview}`);
    $("autodl-preview").textContent = bits.join(" · ") || "尚未保存";
  }
  if (s.compute_backend) setRadio("compute-backend", s.compute_backend);
  updateGpuCommand();
  $("bark-enabled").checked = s.bark_enabled !== false;
  $("bark-title").value = s.bark_title || "b站爬虫";
  $("bark-sound").value = s.bark_sound || "bell";
  $("bark-preview").textContent = s.has_bark_key ? `已保存 ${s.bark_key_preview}` : "尚未填写 Bark Key";
  $("cookie-preview").textContent = s.cookie_preview || "尚未登录";
  $("dep-status").textContent = `Whisper ${s.whisper_installed ? "已安装" : "未安装"} · OCR ${s.ocr_installed ? "已安装" : "未安装"} · ffmpeg ${s.ffmpeg_installed ? "已安装" : "未安装"}`;
  $("library-path").textContent = s.library_dir || "";
}

async function loadCapabilities() {
  try {
    capabilities = await api("/api/capabilities");
    const items = [
      ["ffmpeg", "视频合并", capabilities.ffmpeg],
      ["whisper", "语音转文字", capabilities.whisper],
      ["gpu", "NVIDIA GPU", capabilities.gpu || { ready: false, purpose: "未检测" }],
      ["ocr", "图片 OCR", capabilities.ocr],
      ["rclone", "Google Drive", capabilities.rclone || { ready: false, purpose: "上传后删除本地媒体" }],
      ["gpu_worker", "云端 GPU 工作机", capabilities.gpu_worker || { ready: false, purpose: "未激活" }],
      ["autodl", "AutoDL", capabilities.autodl?.connected
        ? { ready: true, purpose: `${capabilities.autodl.role_label || "已接通"} ${capabilities.autodl.target || ""}`.trim() }
        : { ready: false, purpose: "先选采集转写或主题分析，再一键接入" }],
    ];
    $("capability-grid").innerHTML = items
      .map(([key, title, item]) => `
        <div class="capability-card ${item.ready ? "ready" : "missing"}">
          <span class="cap-dot"></span>
          <div><b>${title}</b><small>${item.purpose}</small></div>
          <em>${item.ready ? "可用" : key === "gpu_worker" || key === "autodl" ? "未激活" : "未安装"}</em>
        </div>`)
      .join("");
    const free = capabilities.disk.free_bytes / 1024 / 1024 / 1024;
    $("install-command").textContent = `可用磁盘 ${free.toFixed(1)} GB\n${capabilities.pip_command}`;
    updatePlanSummary();
    updateAcademicHints();
  } catch (err) {
    $("capability-grid").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

async function startQr() {
  const data = await api("/api/login/qr/start", { method: "POST" });
  $("qr-image").src = data.image;
  $("qr-image").hidden = false;
  $("qr-msg").textContent = "请使用哔哩哔哩 App 扫码";
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const st = await api("/api/login/qr/poll?key=" + encodeURIComponent(data.qrcode_key));
    if (st.code === 0) {
      clearInterval(pollTimer);
      $("qr-msg").textContent = "登录成功";
      toast("登录成功");
      refreshNav();
    } else if (st.code === 86038) {
      clearInterval(pollTimer);
      $("qr-msg").textContent = "二维码已过期";
    } else {
      $("qr-msg").textContent = st.message || "等待扫码";
    }
  }, 1600);
}

async function refreshNav() {
  try {
    const nav = await api("/api/nav");
    const pill = $("login-pill");
    if (nav.is_login) {
      pill.textContent = "已登录 " + (nav.uname || "");
      pill.classList.add("on");
    } else {
      pill.textContent = nav.settings?.has_cookie ? "Cookie 已保存 · 未确认登录" : "匿名模式";
      pill.classList.remove("on");
    }
    if (nav.settings) $("library-path").textContent = nav.settings.library_dir || "";
  } catch (e) {
    $("login-pill").textContent = "离线 / 无法访问 B 站";
  }
}

refreshNav();
loadSettings();
loadCapabilities();
syncChoiceCards();
updatePlanSummary();
updateAcademicHints();
enforceKeywordChoices();
enforceTranscribeChoices();
$("m-ocr").addEventListener("change", updatePlanSummary);
$("bark-test")?.addEventListener("click", async () => {
  try {
    const key = $("bark-key").value.trim();
    await api("/api/notify/test", { method: "POST", body: key ? { key } : {} });
    $("bark-key").value = "";
    toast("已发送测试推送，请看手机");
    loadSettings();
  } catch (err) {
    toast(err.message);
  }
});
function updateGpuCommand() {
  const token = $("gpu-worker-token")?.value.trim() || "你的Token";
  const model = $("whisper-model")?.value || "large-v3";
  if ($("gpu-worker-command")) {
    $("gpu-worker-command").textContent =
      `python tools/gpu_worker.py --host 0.0.0.0 --port 6006 --token ${token} --model ${model}`;
  }
}
$("gpu-gen-token")?.addEventListener("click", () => {
  const token = crypto.randomUUID().replaceAll("-", "");
  $("gpu-worker-token").value = token;
  updateGpuCommand();
  toast("已生成 Token，请复制启动命令到 GPU 机器");
});
$("gpu-worker-token")?.addEventListener("input", updateGpuCommand);
$("whisper-model")?.addEventListener("change", updateGpuCommand);
$("copy-gpu-command")?.addEventListener("click", async () => {
  updateGpuCommand();
  const command = $("gpu-worker-command")?.textContent || "";
  try {
    await navigator.clipboard.writeText(command);
    toast("启动命令已复制");
  } catch (_) {
    toast(command);
  }
});
$("gpu-test")?.addEventListener("click", async () => {
  try {
    const url = $("gpu-worker-url").value.trim();
    const token = $("gpu-worker-token").value.trim();
    const res = await api("/api/gpu/test", { method: "POST", body: { url, token } });
    $("gpu-worker-token").value = "";
    toast(res.message || "GPU 工作机已连接");
    loadSettings();
    loadCapabilities();
  } catch (err) {
    toast(err.message);
  }
});

function autodlRole() {
  const picked = document.querySelector('input[name="autodl-role"]:checked');
  return picked && picked.value === "analyze" ? "analyze" : "collect";
}

let autodlChannels = null;

async function loadAutodlChannels() {
  try {
    const data = await api("/api/gpu/autodl/channels");
    autodlChannels = data.channels || [];
    renderAutodlChannelMatrix();
  } catch (_) {
    autodlChannels = null;
  }
}

function channelById(id) {
  return (autodlChannels || []).find((row) => row.id === id) || null;
}

function renderAutodlChannelMatrix() {
  const box = $("autodl-channel-matrix");
  if (!box) return;
  const ch = channelById(autodlRole());
  if (!ch) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const files = (ch.upload_files || []).map((f) => `<li><code>${escapeHtml(f)}</code></li>`).join("");
  const models = (ch.download_models || []).map((f) => `<li>${escapeHtml(f)}</li>`).join("");
  const skip = (ch.skip_models || []).map((f) => `<li>${escapeHtml(f)}</li>`).join("");
  const covers = (ch.covers || []).map((f) => `<li>${escapeHtml(f)}</li>`).join("");
  box.innerHTML = `
    <h4>${escapeHtml(ch.label)} · ${escapeHtml(ch.badge || "")}</h4>
    <div class="mtx-grid">
      <div class="mtx-block">
        <div class="mtx-label">覆盖功能</div>
        <ul>${covers}</ul>
      </div>
      <div class="mtx-block">
        <div class="mtx-label">会下载 / 预热</div>
        <ul>${models || "<li>无（接入阶段不预热）</li>"}</ul>
      </div>
      <div class="mtx-block">
        <div class="mtx-label">明确跳过</div>
        <ul>${skip}</ul>
      </div>
      <div class="mtx-block">
        <div class="mtx-label">本次上传（${ch.upload_count || 0}）</div>
        <ul>${files}</ul>
      </div>
    </div>
    <p class="mtx-note">${escapeHtml(ch.upload_note || "")} ${escapeHtml(ch.next_step || "")}</p>
  `;
  const collectSummary = $("autodl-collect-summary");
  const analyzeSummary = $("autodl-analyze-summary");
  const collect = channelById("collect");
  const analyze = channelById("analyze");
  if (collectSummary && collect) {
    collectSummary.textContent = `覆盖四种采集的云端 Whisper。上传 ${collect.upload_count} 个文件；预热 large-v3。`;
  }
  if (analyzeSummary && analyze) {
    analyzeSummary.textContent = `覆盖 BERTopic。上传 ${analyze.upload_count} 个文件；不下载 Whisper。`;
  }
}

function updateAutodlRoleHint() {
  const hint = $("autodl-role-hint");
  const btn = $("autodl-connect");
  if (!hint) return;
  const ch = channelById(autodlRole());
  if (autodlRole() === "analyze") {
    hint.innerHTML = ch?.next_step
      ? escapeHtml(ch.next_step)
      : "主题分析：只同步 BERTopic 脚本，<strong>不会</strong>下载 Whisper large-v3。接入后运行 <code>tools/launch_bertopic_autodl.py</code>。";
    if (btn) btn.textContent = "接入主题分析通道";
  } else {
    hint.innerHTML = ch?.next_step
      ? escapeHtml(ch.next_step)
      : "采集转写：爬虫仍在本机，云端只做 Whisper / OCR。四种采集共用这一通道。";
    if (btn) btn.textContent = "接入采集转写通道";
  }
  renderAutodlChannelMatrix();
}

document.querySelectorAll('input[name="autodl-role"]').forEach((input) => {
  input.addEventListener("change", () => {
    syncChoiceCards();
    updateAutodlRoleHint();
  });
});
updateAutodlRoleHint();
loadAutodlChannels();

function appendAutodlLog(message, level) {
  const view = $("autodl-log");
  if (!view || !message) return;
  const line = document.createElement("div");
  line.className = level || "info";
  line.textContent = message;
  view.appendChild(line);
  view.scrollTop = view.scrollHeight;
}

$("autodl-connect")?.addEventListener("click", async () => {
  const ssh = $("autodl-ssh")?.value.trim() || "";
  const password = $("autodl-password")?.value.trim() || "";
  const view = $("autodl-log");
  if (view) view.textContent = "";
  const btn = $("autodl-connect");
  if (btn) btn.disabled = true;
  appendAutodlLog(
    autodlRole() === "analyze"
      ? "开始接入「主题分析通道」：只传 BERTopic 脚本，不预热 Whisper…"
      : "开始接入「采集转写通道」：同步 Whisper 工作机并预热 large-v3（四种采集共用）…",
    "info",
  );
  try {
    const res = await fetch("/api/gpu/autodl/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssh_command: ssh, password, role: autodlRole() }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.message || "接入失败");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let last = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const payload = JSON.parse(line.slice(6));
        last = payload;
        if (payload.line) appendAutodlLog(payload.line, payload.level || "info");
        if (payload.error) appendAutodlLog(payload.error, "error");
      }
    }
    if (last && last.ok === false) throw new Error(last.error || "接入失败");
    if ($("autodl-password")) $("autodl-password").value = "";
    toast(last?.message || "AutoDL 已接入");
    loadSettings();
    loadCapabilities();
  } catch (err) {
    appendAutodlLog(err.message, "error");
    toast(err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("autodl-disconnect")?.addEventListener("click", async () => {
  try {
    await api("/api/gpu/autodl/disconnect", { method: "POST", body: {} });
    appendAutodlLog("本机隧道已断开。AutoDL 上的工作机还在，下次可再点接入。", "warn");
    toast("隧道已断开");
    loadCapabilities();
  } catch (err) {
    toast(err.message);
  }
});
$("reclaim-btn")?.addEventListener("click", async () => {
  if (!confirm("将把已完成转写/OCR 的媒体上传到 Google Drive，然后删除本地视频/音频/图片。正在处理的条目会跳过。继续？")) return;
  try {
    const summary = await api("/api/library/reclaim", {
      method: "POST",
      body: { policy: "upload_then_delete", dry_run: false },
    });
    const mb = ((summary.bytes_freed || 0) / 1024 / 1024).toFixed(1);
    toast(`已处理 ${summary.folders || 0} 个目录，释放约 ${mb} MB`);
  } catch (err) {
    toast(err.message);
  }
});

async function restoreLiveJob() {
  try {
    const data = await api("/api/jobs");
    const job = (data.live || []).find((item) => ["queued", "running", "cancelling"].includes(item.status));
    if (job) {
      currentJobId = job.id;
      $("job-id").textContent = "#" + job.id;
      $("log-view").textContent = "";
      (job.logs || []).forEach(appendLog);
      applyProgress(job.progress || { stage: job.status });
      listenJob(job.id);
      return;
    }
    const paused = (data.history || []).find((item) =>
      ["cancelled", "interrupted", "error"].includes(item.status)
      && ["academic", "keyword", "transcribe", "archive", "scout"].includes(jobKind(item))
    );
    if (paused) {
      currentJobId = paused.id;
      $("job-id").textContent = "#" + paused.id;
      applyProgress(paused.progress || { stage: paused.status });
      $("m-current").textContent = "已暂停。可点「续跑此任务」，或在上方任务记录里选别的一次。";
    }
    loadJobHistory(data);
  } catch (_) {}
}

async function loadJobHistory(preset) {
  const box = $("job-history");
  if (!box) return;
  try {
    const data = preset || await fetchJobs();
    const seen = new Set();
    const rows = [...(data.live || []), ...(data.history || [])].filter((job) => {
      if (seen.has(job.id)) return false;
      seen.add(job.id);
      return ["archive", "scout", "academic", "keyword", "transcribe"].includes(jobKind(job));
    }).slice(0, 12);
    if (!rows.length) {
      box.innerHTML = '<p class="muted">还没有任务。到左侧三种采集里选一种，点「从头开始」。</p>';
      return;
    }
    box.innerHTML = rows.map((job) => {
      const busy = ["queued", "running", "cancelling"].includes(job.status);
      const status = STATUS_LABEL[job.status] || job.status || "";
      return `<div class="job-row">
        <div>
          <b>${escapeHtml(KIND_LABEL[jobKind(job)] || "任务")} · ${escapeHtml(status)}</b>
          <span class="muted">#${escapeHtml(job.id)} · ${escapeHtml(jobProgressText(job))}</span>
        </div>
        ${busy
          ? `<button type="button" class="ghost" data-pause="${escapeHtml(job.id)}">暂停</button>`
          : `<button type="button" class="ghost" data-resume="${escapeHtml(job.id)}">续跑</button>`}
      </div>`;
    }).join("");
    box.querySelectorAll("[data-resume]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await resumeExistingJob(btn.dataset.resume);
        } catch (err) {
          toast(err.message);
        }
      });
    });
    box.querySelectorAll("[data-pause]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api("/api/jobs/" + btn.dataset.pause + "/cancel", { method: "POST" });
          currentJobId = btn.dataset.pause;
          toast("已暂停。进度已保留，回来后点「续跑」即可。");
          setTimeout(() => loadJobHistory(), 1500);
        } catch (err) {
          toast(err.message);
        }
      });
    });
  } catch (err) {
    box.innerHTML = `<p class="muted">读取任务失败：${escapeHtml(err.message)}</p>`;
  }
}

restoreLiveJob();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function renderTable(rows, el) {
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = '<p class="muted">没有行</p>';
    return;
  }
  const keys = Object.keys(rows[0]);
  const head = keys.map((k) => `<th>${escapeHtml(k)}</th>`).join("");
  const body = rows.map((row) => `<tr>${keys.map((k) => `<td>${escapeHtml(row[k])}</td>`).join("")}</tr>`).join("");
  el.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

const CORPUS_LABELS = {
  authors: "作者",
  videos: "视频",
  comments: "评论",
  danmaku: "弹幕",
  dynamics: "动态",
  snowball_edges: "推荐边",
  gate_decisions: "门禁",
  transcripts: "转写",
};

async function loadCorpus() {
  try {
    const data = await api("/api/corpus");
    const stats = $("corpus-stats");
    stats.innerHTML = Object.entries(CORPUS_LABELS).map(([key, label]) => {
      const n = (data.counts || {})[key] || 0;
      return `<div class="stat-chip"><b>${n}</b><span>${label}</span></div>`;
    }).join("");
    const funnel = data.funnel || [];
    const funnelEl = $("corpus-funnel");
    if (!funnel.length) {
      funnelEl.innerHTML = '<p class="muted">还没有门禁记录。跑一次「学术滚雪球」后会出现通过 / 剪枝原因。</p>';
    } else {
      funnelEl.innerHTML = `<div class="funnel-row">${funnel.map((row) => {
        const passed = Number(row.passed) === 1;
        return `<span class="funnel-chip ${passed ? "ok" : "warn"}">${escapeHtml(row.reason || (passed ? "pass" : "pruned"))} · ${row.n}</span>`;
      }).join("")}</div>`;
    }
    const runs = data.runs || [];
    if (!runs.length) {
      $("corpus-runs").innerHTML = '<p class="muted">还没有采样任务。可先回填现有档案，或开一次滚雪球。</p>';
    } else {
      renderTable(runs.map((run) => ({
        任务: run.run_id,
        类型: run.kind === "academic" ? "滚雪球" : "归档",
        标签: run.label || "",
        状态: run.status || "",
        开始: run.started_at || "",
      })), $("corpus-runs"));
    }
  } catch (err) {
    toast(err.message);
  }
}

$("corpus-refresh")?.addEventListener("click", loadCorpus);
$("corpus-export")?.addEventListener("click", async () => {
  try {
    const data = await api("/api/corpus/export", { method: "POST", body: {} });
    toast("已导出到 " + (data.dir || "data/exports"));
  } catch (err) {
    toast(err.message);
  }
});
$("corpus-ingest")?.addEventListener("click", async () => {
  if (!confirm("把现有 data/library 档案回填进 corpus.db？已有行会按主键更新，不会重复。")) return;
  try {
    const data = await api("/api/corpus/ingest", { method: "POST", body: {} });
    const c = data.counts || {};
    toast(`回填完成：账号 ${c.accounts || 0} · 视频 ${c.videos || 0} · 评论 ${c.comments || 0}`);
    loadCorpus();
  } catch (err) {
    toast(err.message);
  }
});
$("corpus-query")?.addEventListener("click", async () => {
  const sql = ($("corpus-sql").value || "").trim() || "SELECT * FROM v_video_corpus LIMIT 20";
  try {
    const data = await api("/api/corpus/query", { method: "POST", body: { sql } });
    renderTable(data.rows || [], $("corpus-query-out"));
    toast(`查出 ${data.count || 0} 行`);
  } catch (err) {
    toast(err.message);
  }
});
