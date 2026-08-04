const state = {
  config: null,
  tasks: [],
  total: 0,
  page: 1,
  pageSize: 25,
  status: "",
  query: "",
  pollingTimer: null,
  tasksSignature: null,
  tasksRequestVersion: 0,
  currentDetailId: null,
  currentDetailSignature: null,
  uploads: new Map(),
};

const elements = {
  statusPill: document.querySelector("#statusPill"),
  statusPillText: document.querySelector("#statusPillText"),
  workerState: document.querySelector("#workerState"),
  deviceState: document.querySelector("#deviceState"),
  modelState: document.querySelector("#modelState"),
  queueCount: document.querySelector("#queueCount"),
  diskBar: document.querySelector("#diskBar"),
  diskLabel: document.querySelector("#diskLabel"),
  clock: document.querySelector("#clock"),
  dropZone: document.querySelector("#dropZone"),
  chooseButton: document.querySelector("#chooseButton"),
  fileInput: document.querySelector("#fileInput"),
  tabUpload: document.querySelector("#tabUpload"),
  tabLink: document.querySelector("#tabLink"),
  linkBox: document.querySelector("#linkBox"),
  linkInput: document.querySelector("#linkInput"),
  createLinkTask: document.querySelector("#createLinkTask"),
  modelSelect: document.querySelector("#modelSelect"),
  languageSelect: document.querySelector("#languageSelect"),
  modelHint: document.querySelector("#modelHint"),
  uploadLimit: document.querySelector("#uploadLimit"),
  uploadQueue: document.querySelector("#uploadQueue"),
  taskList: document.querySelector("#taskList"),
  statusFilters: document.querySelector("#statusFilters"),
  searchInput: document.querySelector("#searchInput"),
  refreshButton: document.querySelector("#refreshButton"),
  resultCount: document.querySelector("#resultCount"),
  prevPage: document.querySelector("#prevPage"),
  nextPage: document.querySelector("#nextPage"),
  pageLabel: document.querySelector("#pageLabel"),
  dialog: document.querySelector("#taskDialog"),
  dialogShell: document.querySelector(".dialog-shell"),
  dialogClose: document.querySelector("#dialogClose"),
  dialogTitle: document.querySelector("#dialogTitle"),
  dialogStatus: document.querySelector("#dialogStatus"),
  dialogMetadata: document.querySelector("#dialogMetadata"),
  dialogActions: document.querySelector("#dialogActions"),
  transcriptText: document.querySelector("#transcriptText"),
  copyTranscript: document.querySelector("#copyTranscript"),
  segmentsPanel: document.querySelector("#segmentsPanel"),
  segmentCount: document.querySelector("#segmentCount"),
  segmentList: document.querySelector("#segmentList"),
  toastRegion: document.querySelector("#toastRegion"),
};

const STATUS_LABELS = {
  uploading: "上传中",
  queued: "排队中",
  running: "处理中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  deleting: "删除中",
  delete_failed: "删除失败",
};

const STAGE_LABELS = {
  upload: "文件上传",
  receiving_upload: "接收文件",
  queue: "等待 Worker",
  starting: "启动任务",
  downloading: "下载视频",
  preprocessing: "提取音轨",
  transcribing: "语音转写",
  writing_results: "生成结果",
  complete: "处理完成",
  failed: "处理失败",
  cancelled: "任务取消",
  deleting: "删除文件",
  delete_failed: "删除失败",
};

document.addEventListener("DOMContentLoaded", bootstrap);

async function bootstrap() {
  bindEvents();
  tickClock();
  window.setInterval(tickClock, 1000);
  try {
    await loadConfig();
    await Promise.all([loadTasks(), loadSystemStatus()]);
  } catch (error) {
    toast(readError(error), true);
  }
  schedulePoll();
}

function bindEvents() {
  elements.chooseButton.addEventListener("click", (event) => {
    event.stopPropagation();
    elements.fileInput.click();
  });
  elements.dropZone.addEventListener("click", () => elements.fileInput.click());
  elements.dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      elements.fileInput.click();
    }
  });
  elements.fileInput.addEventListener("change", () => {
    handleFiles([...elements.fileInput.files]);
    elements.fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((name) => {
    elements.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      elements.dropZone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    elements.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      elements.dropZone.classList.remove("dragging");
    });
  });
  elements.dropZone.addEventListener("drop", (event) => {
    handleFiles([...event.dataTransfer.files]);
  });

  elements.modelSelect.addEventListener("change", updateModelHint);
  elements.tabUpload.addEventListener("click", () => switchSourceMode("upload"));
  elements.tabLink.addEventListener("click", () => switchSourceMode("link"));
  elements.createLinkTask.addEventListener("click", createLinkTaskFlow);
  elements.statusFilters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-status]");
    if (!button) return;
    state.status = button.dataset.status;
    state.page = 1;
    elements.statusFilters
      .querySelectorAll("button")
      .forEach((item) => item.classList.toggle("active", item === button));
    loadTasks().catch(showError);
  });

  let searchTimer;
  elements.searchInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.query = elements.searchInput.value.trim();
      state.page = 1;
      loadTasks().catch(showError);
    }, 300);
  });

  elements.refreshButton.addEventListener("click", () => refreshAll(true));
  elements.statusPill.addEventListener("click", () => refreshAll(true));
  elements.prevPage.addEventListener("click", () => changePage(-1));
  elements.nextPage.addEventListener("click", () => changePage(1));
  elements.taskList.addEventListener("click", handleTaskAction);
  elements.dialogActions.addEventListener("click", handleDialogAction);
  elements.dialogClose.addEventListener("click", () => elements.dialog.close());
  elements.dialog.addEventListener("close", () => {
    state.currentDetailId = null;
    state.currentDetailSignature = null;
    syncDialogLayer();
  });
  elements.dialog.addEventListener("click", (event) => {
    if (event.target === elements.dialog) elements.dialog.close();
  });
  elements.copyTranscript.addEventListener("click", copyTranscript);
}

async function loadConfig() {
  state.config = await api("/api/config");
  elements.modelSelect.innerHTML = state.config.models
    .map(
      (model) =>
        `<option value="${escapeHtml(model.value)}">${escapeHtml(model.label)}</option>`,
    )
    .join("");
  elements.modelSelect.value = state.config.default_model;
  elements.languageSelect.innerHTML = state.config.languages
    .map(
      (language) =>
        `<option value="${escapeHtml(language.value)}">${escapeHtml(language.label)}</option>`,
    )
    .join("");
  elements.languageSelect.value = state.config.default_language || "auto";
  elements.uploadLimit.textContent = `单文件不超过 ${formatBytes(
    state.config.limits.max_upload_bytes,
  )}，媒体时长不超过 ${formatDuration(
    state.config.limits.max_media_seconds,
  )}。`;
  updateModelHint();
}

async function loadTasks() {
  const requestVersion = ++state.tasksRequestVersion;
  const view = {
    page: state.page,
    pageSize: state.pageSize,
    status: state.status,
    query: state.query,
  };
  const params = new URLSearchParams({
    page: String(view.page),
    page_size: String(view.pageSize),
  });
  if (view.status) params.set("status", view.status);
  if (view.query) params.set("q", view.query);
  const result = await api(`/api/tasks?${params}`);
  if (requestVersion !== state.tasksRequestVersion) return;
  const nextSignature = JSON.stringify({
    ...view,
    total: result.total,
    items: result.items,
  });
  state.tasks = result.items;
  state.total = result.total;
  if (nextSignature !== state.tasksSignature) {
    state.tasksSignature = nextSignature;
    renderTasks();
  }
  if (state.currentDetailId) {
    try {
      await openDetail(state.currentDetailId, false);
    } catch {
      // The task list remains usable if a detail refresh fails.
    }
  }
}

async function loadSystemStatus() {
  try {
    const status = await api("/api/system/status");
    renderSystemStatus(status);
  } catch (error) {
    elements.statusPill.className = "status-pill offline";
    elements.statusPillText.textContent = "服务离线";
    elements.workerState.textContent = "无法连接";
    throw error;
  }
}

function renderSystemStatus(status) {
  const worker = status.worker;
  const online = worker.running && worker.available;
  elements.statusPill.className = `status-pill ${online ? "online" : "offline"}`;
  elements.statusPillText.textContent = online ? "服务可用" : "Worker 不可用";
  elements.statusPill.title = worker.detail || "";
  elements.workerState.textContent = worker.current_task_id
    ? "正在处理"
    : worker.available
      ? "空闲"
      : worker.detail || "不可用";
  elements.deviceState.textContent = `${String(worker.device || "—").toUpperCase()}${
    worker.compute_type ? ` / ${worker.compute_type}` : ""
  }`;
  elements.modelState.textContent = worker.loaded_model || "按任务加载";
  elements.queueCount.textContent = String(status.tasks.queued || 0);
  const usedPercent = status.disk.total_bytes
    ? (status.disk.used_bytes / status.disk.total_bytes) * 100
    : 0;
  elements.diskBar.style.width = `${Math.min(100, usedPercent)}%`;
  elements.diskLabel.textContent = `存储已用 ${usedPercent.toFixed(
    1,
  )}% · 剩余 ${formatBytes(status.disk.free_bytes)}`;
}

function renderTasks() {
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  if (state.page > pageCount) {
    state.page = pageCount;
    loadTasks().catch(showError);
    return;
  }
  elements.pageLabel.textContent = `${state.page} / ${pageCount}`;
  elements.prevPage.disabled = state.page <= 1;
  elements.nextPage.disabled = state.page >= pageCount;
  elements.resultCount.textContent = `共 ${state.total} 项记录`;

  if (!state.tasks.length) {
    elements.taskList.innerHTML = `
      <div class="empty-state">
        <div>
          <strong>${state.query || state.status ? "没有匹配的任务" : "还没有转写记录"}</strong>
          <p>${state.query || state.status ? "更换筛选条件后再试。" : "从上方上传文件或粘贴抖音分享链接开始。"}</p>
        </div>
      </div>`;
    return;
  }

  elements.taskList.innerHTML = state.tasks.map(renderTaskRow).join("");
}

function renderTaskRow(task) {
  const progress = Number(task.progress || 0);
  const elapsed = taskElapsed(task);
  const actionButtons = [];
  actionButtons.push(
    `<button class="primary" data-action="detail" data-id="${task.id}" title="查看详情">详情</button>`,
  );
  if (task.actions.cancel) {
    actionButtons.push(
      `<button data-action="cancel" data-id="${task.id}" title="取消任务">取消</button>`,
    );
  }
  if (task.actions.retry) {
    actionButtons.push(
      `<button data-action="retry" data-id="${task.id}" title="重新排队">重试</button>`,
    );
  }
  if (task.status === "succeeded") {
    actionButtons.push(
      `<a href="${task.artifacts.txt}" title="下载纯文本">TXT</a>`,
    );
  }
  if (task.actions.delete) {
    actionButtons.push(
      `<button data-action="delete" data-id="${task.id}" title="删除任务">×</button>`,
    );
  }

  return `
    <article class="task-row" data-id="${task.id}">
      <div class="task-media">
        <button data-action="detail" data-id="${task.id}">${escapeHtml(task.original_name)}</button>
        <small>${formatDate(task.created_at)} · ${formatBytes(task.size_bytes || task.expected_size_bytes || 0)}</small>
      </div>
      <div class="task-model">
        <strong>${escapeHtml(task.model_name)}</strong>
        <small>${escapeHtml(task.device || "待分配")}</small>
      </div>
      <div class="task-state">
        <div class="status-line">
          <span class="status-badge ${task.status}">${STATUS_LABELS[task.status] || task.status}</span>
          <small>${progress.toFixed(0)}%</small>
        </div>
        <div class="task-progress"><i style="width:${progress}%"></i></div>
        <span class="progress-message">${escapeHtml(task.message || STAGE_LABELS[task.stage] || "")}</span>
      </div>
      <div class="task-time">
        <strong>${formatDuration(task.duration_seconds)}</strong>
        <small>${elapsed === null ? "尚未开始" : `耗时 ${formatDuration(elapsed)}`}</small>
      </div>
      <div class="task-actions">${actionButtons.join("")}</div>
    </article>`;
}

function switchSourceMode(mode) {
  const uploadActive = mode === "upload";
  elements.dropZone.hidden = !uploadActive;
  elements.linkBox.hidden = uploadActive;
  elements.tabUpload.classList.toggle("active", uploadActive);
  elements.tabLink.classList.toggle("active", !uploadActive);
  elements.tabUpload.setAttribute("aria-selected", String(uploadActive));
  elements.tabLink.setAttribute("aria-selected", String(!uploadActive));
  if (!uploadActive) elements.linkInput.focus();
}

async function createLinkTaskFlow() {
  if (!state.config) {
    toast("服务配置尚未载入", true);
    return;
  }
  const text = elements.linkInput.value.trim();
  if (!text) {
    toast("请先粘贴抖音分享链接或口令", true);
    return;
  }
  const url = extractLinkUrl(text);
  if (!url) {
    toast("未在文本中找到链接，请粘贴完整的抖音分享链接", true);
    return;
  }
  const localId = window.WhisperUploadId.create();
  state.uploads.set(localId, {
    id: localId,
    fileName: "抖音链接任务",
    size: 0,
    progress: 0,
    status: "preparing",
    message: "正在创建任务",
  });
  renderUploads();
  try {
    const task = await api("/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": localId,
      },
      body: JSON.stringify({
        source_url: url,
        model: elements.modelSelect.value,
        language: elements.languageSelect.value,
      }),
    });
    updateUpload(localId, {
      taskId: task.id,
      status: "done",
      progress: 100,
      message: "已入队，等待下载",
    });
    elements.linkInput.value = "";
    window.setTimeout(() => {
      state.uploads.delete(localId);
      renderUploads();
    }, 5000);
    toast("链接任务已创建");
    await refreshAll();
  } catch (error) {
    updateUpload(localId, {
      status: "failed",
      message: readError(error),
    });
    toast(`链接任务创建失败：${readError(error)}`, true);
  }
}

function extractLinkUrl(text) {
  const match = text.match(/https?:\/\/[^\s　，,；;：:！!？?、()（）]+/);
  return match ? match[0].replace(/\/+$/, "") : null;
}

async function handleFiles(files) {
  if (!files.length) return;
  if (!state.config) {
    toast("服务配置尚未载入", true);
    return;
  }
  const accepted = [];
  for (const file of files) {
    if (file.size > state.config.limits.max_upload_bytes) {
      toast(`${file.name} 超过上传大小限制`, true);
      continue;
    }
    accepted.push(file);
  }
  await runWithConcurrency(accepted, 2, uploadFile);
  await refreshAll();
}

async function uploadFile(file) {
  const localId = window.WhisperUploadId.create();
  state.uploads.set(localId, {
    id: localId,
    fileName: file.name,
    size: file.size,
    progress: 0,
    status: "preparing",
    message: "正在创建任务",
  });
  renderUploads();

  try {
    const task = await api("/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": localId,
      },
      body: JSON.stringify({
        file_name: file.name,
        size_bytes: file.size,
        content_type: file.type || null,
        model: elements.modelSelect.value,
        language: elements.languageSelect.value,
      }),
    });
    updateUpload(localId, {
      taskId: task.id,
      status: "uploading",
      message: "正在上传",
    });
    await uploadBinary(task.id, file, (progress) => {
      updateUpload(localId, {
        progress,
        message: `正在上传 ${progress.toFixed(0)}%`,
      });
    });
    updateUpload(localId, {
      status: "done",
      progress: 100,
      message: "上传完成，已进入队列",
    });
    window.setTimeout(() => {
      state.uploads.delete(localId);
      renderUploads();
    }, 5000);
  } catch (error) {
    updateUpload(localId, {
      status: "failed",
      message: readError(error),
    });
    toast(`${file.name}：${readError(error)}`, true);
  }
}

function uploadBinary(taskId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/tasks/${taskId}/source`);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        onProgress((event.loaded / event.total) * 100);
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
      } else {
        reject(new Error(parseErrorResponse(xhr.responseText, xhr.status)));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("网络连接中断")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(file);
  });
}

function renderUploads() {
  const uploads = [...state.uploads.values()];
  elements.uploadQueue.innerHTML = uploads
    .map(
      (upload) => `
        <div class="upload-card ${upload.status === "failed" ? "failed" : upload.status === "done" ? "done" : ""}">
          <div>
            <strong>${escapeHtml(upload.fileName)}</strong>
            <small>${escapeHtml(upload.message)} · ${formatBytes(upload.size)}</small>
          </div>
          <div class="mini-progress"><i style="width:${upload.progress}%"></i></div>
          <small>${upload.status === "failed" ? "ERROR" : `${upload.progress.toFixed(0)}%`}</small>
        </div>`,
    )
    .join("");
}

function updateUpload(id, patch) {
  const upload = state.uploads.get(id);
  if (!upload) return;
  Object.assign(upload, patch);
  renderUploads();
}

async function handleTaskAction(event) {
  const target = event.target.closest("[data-action][data-id]");
  if (!target) return;
  await performAction(target.dataset.action, target.dataset.id);
}

async function handleDialogAction(event) {
  const target = event.target.closest("[data-action][data-id]");
  if (!target || target.tagName === "A") return;
  await performAction(target.dataset.action, target.dataset.id);
}

async function performAction(action, taskId) {
  try {
    if (action === "detail") {
      await openDetail(taskId);
      return;
    }
    if (action === "cancel") {
      if (!window.confirm("确定取消这个任务吗？")) return;
      await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
      toast("取消请求已提交");
    } else if (action === "retry") {
      await api(`/api/tasks/${taskId}/retry`, { method: "POST" });
      toast("任务已重新进入队列");
    } else if (action === "delete") {
      if (!window.confirm("确定永久删除这个任务及其文件吗？此操作不能恢复。")) return;
      await api(`/api/tasks/${taskId}`, { method: "DELETE" });
      if (state.currentDetailId === taskId) elements.dialog.close();
      toast("任务已删除");
    }
    await refreshAll();
  } catch (error) {
    showError(error);
  }
}

async function openDetail(taskId, show = true) {
  const task = await api(`/api/tasks/${taskId}`);
  const taskSignature = JSON.stringify(task);
  const sameTask = state.currentDetailId === task.id;
  state.currentDetailId = task.id;
  if (sameTask && taskSignature === state.currentDetailSignature) {
    if (show && !elements.dialog.open) {
      elements.dialog.showModal();
      syncDialogLayer();
    }
    return;
  }
  const scrollState =
    sameTask && elements.dialog.open ? captureDetailScroll() : null;
  elements.dialogTitle.textContent = task.original_name;
  elements.dialogStatus.innerHTML = `
    <div class="status-line">
      <span class="status-badge ${task.status}">${STATUS_LABELS[task.status] || task.status}</span>
      <small>${STAGE_LABELS[task.stage] || task.stage} · ${Number(task.progress || 0).toFixed(0)}%</small>
    </div>
    <div class="task-progress"><i style="width:${Number(task.progress || 0)}%"></i></div>
    <span class="progress-message">${escapeHtml(task.error_message || task.message || "")}</span>`;
  const metadataRows = [
    ["模型", task.model_name],
    ["语言", task.language_detected || task.language_requested || "自动"],
    ["设备", task.device || "待分配"],
    ["计算类型", task.compute_type || "待分配"],
    ["媒体时长", formatDuration(task.duration_seconds)],
    ["文件大小", formatBytes(task.size_bytes || task.expected_size_bytes || 0)],
    ["创建时间", formatDate(task.created_at)],
    ["处理耗时", formatDuration(taskElapsed(task))],
  ];
  if (task.source_url) {
    metadataRows.push(["来源链接", task.source_url]);
  }
  elements.dialogMetadata.innerHTML = metadataRows
    .map(
      ([label, value]) => `
        <div class="meta-item">
          <span>${escapeHtml(label)}</span>
          <strong title="${escapeHtml(String(value ?? "—"))}">${escapeHtml(String(value ?? "—"))}</strong>
        </div>`,
    )
    .join("");

  const actions = [];
  if (task.actions.cancel) {
    actions.push(
      `<button data-action="cancel" data-id="${task.id}">取消任务</button>`,
    );
  }
  if (task.actions.retry) {
    actions.push(
      `<button class="primary" data-action="retry" data-id="${task.id}">重新转写</button>`,
    );
  }
  for (const [kind, url] of Object.entries(task.artifacts || {})) {
    actions.push(
      `<a href="${url}" download>下载 ${kind.toUpperCase()}</a>`,
    );
  }
  if (task.actions.delete) {
    actions.push(
      `<button data-action="delete" data-id="${task.id}">删除任务</button>`,
    );
  }
  elements.dialogActions.innerHTML = actions.join("");
  elements.transcriptText.textContent =
    task.result_text ||
    task.error_message ||
    (task.status === "succeeded" ? "该任务没有识别到语音文本。" : "任务完成后将在这里显示正文。");
  elements.copyTranscript.disabled = !task.result_text;
  elements.segmentsPanel.hidden = true;
  elements.segmentCount.textContent = "";
  elements.segmentList.innerHTML = "";

  if (task.status === "succeeded" && task.artifacts?.json) {
    try {
      const result = await api(task.artifacts.json);
      renderSegments(result.segments || []);
    } catch {
      // The main transcript remains usable even if timeline loading fails.
    }
  }
  state.currentDetailSignature = taskSignature;
  if (show && !elements.dialog.open) {
    elements.dialog.showModal();
    syncDialogLayer();
    elements.dialogShell.scrollTop = 0;
    elements.transcriptText.scrollTop = 0;
    elements.segmentList.scrollTop = 0;
  } else if (scrollState) {
    restoreDetailScroll(scrollState);
  }
}

function renderSegments(segments) {
  if (!segments.length) return;
  elements.segmentsPanel.hidden = false;
  elements.segmentCount.textContent = `${segments.length} 段 · 区域内滚动`;
  elements.segmentList.innerHTML = segments
    .map(
      (segment) => `
        <div class="segment-row">
          <time>${formatTimeline(segment.start)} → ${formatTimeline(segment.end)}</time>
          <p>${escapeHtml(segment.text || "")}</p>
        </div>`,
    )
    .join("");
}

function captureDetailScroll() {
  return {
    shell: elements.dialogShell.scrollTop,
    transcript: elements.transcriptText.scrollTop,
    segments: elements.segmentList.scrollTop,
  };
}

function restoreDetailScroll(scrollState) {
  elements.dialogShell.scrollTop = scrollState.shell;
  elements.transcriptText.scrollTop = scrollState.transcript;
  elements.segmentList.scrollTop = scrollState.segments;
}

async function copyTranscript() {
  const text = elements.transcriptText.textContent;
  if (!text) return;
  const copied = await writeClipboardText(text);
  toast(
    copied ? "全文已复制" : "复制失败，请选中正文后手动复制",
    !copied,
  );
}

async function writeClipboardText(text) {
  if (typeof navigator.clipboard?.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Intranet HTTP pages usually require the selection-based fallback below.
    }
  }
  return copyTextWithSelection(text);
}

function copyTextWithSelection(text) {
  if (typeof document.execCommand !== "function") return false;

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.cssText =
    "position:fixed;top:0;left:-9999px;width:1px;height:1px;opacity:0;pointer-events:none;";
  const activeElement = document.activeElement;
  const host = elements.dialog.open ? elements.dialog : document.body;
  host.append(textarea);

  try {
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
    if (activeElement && typeof activeElement.focus === "function") {
      try {
        activeElement.focus({ preventScroll: true });
      } catch {
        activeElement.focus();
      }
    }
  }
}

async function refreshAll(showToast = false) {
  try {
    await Promise.all([loadTasks(), loadSystemStatus()]);
    if (showToast) toast("状态已刷新");
  } catch (error) {
    showError(error);
  } finally {
    schedulePoll();
  }
}

function schedulePoll() {
  window.clearTimeout(state.pollingTimer);
  const hasActive = state.tasks.some((task) =>
    ["uploading", "queued", "running", "deleting"].includes(task.status),
  );
  state.pollingTimer = window.setTimeout(
    () => refreshAll(),
    hasActive ? 2000 : 10000,
  );
}

function changePage(delta) {
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  state.page = Math.min(pageCount, Math.max(1, state.page + delta));
  loadTasks().catch(showError);
}

function updateModelHint() {
  const model = state.config?.models.find(
    (item) => item.value === elements.modelSelect.value,
  );
  elements.modelHint.textContent =
    model?.hint || "模型越大，通常质量越高、处理越慢。";
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      message = payload.detail || payload.error?.message || message;
      if (Array.isArray(message)) {
        message = message.map((item) => item.msg).join("；");
      }
    } catch {
      // Keep HTTP status as the error message.
    }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function parseErrorResponse(text, statusCode) {
  try {
    const payload = JSON.parse(text);
    return payload.detail || `${statusCode} 上传失败`;
  } catch {
    return `${statusCode} 上传失败`;
  }
}

function taskElapsed(task) {
  if (!task.started_at) return null;
  const started = new Date(task.started_at).getTime();
  const ended = task.finished_at
    ? new Date(task.finished_at).getTime()
    : Date.now();
  return Math.max(0, (ended - started) / 1000);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(value) / Math.log(1024)),
  );
  const amount = value / 1024 ** index;
  return `${amount.toFixed(index === 0 || amount >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) {
    return "—";
  }
  const total = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}时 ${minutes}分 ${secs}秒`;
  if (minutes) return `${minutes}分 ${secs}秒`;
  return `${secs}秒`;
}

function formatTimeline(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(minutes).padStart(2, "0")}:${secs.toFixed(1).padStart(4, "0")}`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function tickClock() {
  elements.clock.textContent = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

async function runWithConcurrency(items, limit, worker) {
  const queue = [...items];
  const runners = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) {
      const item = queue.shift();
      await worker(item);
    }
  });
  await Promise.all(runners);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(message, isError = false) {
  syncDialogLayer();
  const item = document.createElement("div");
  item.className = `toast${isError ? " error" : ""}`;
  item.textContent = message;
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 4500);
}

function syncDialogLayer() {
  const dialogOpen = elements.dialog.open;
  document.body.classList.toggle("dialog-open", dialogOpen);
  const toastHost = dialogOpen ? elements.dialog : document.body;
  if (elements.toastRegion.parentElement !== toastHost) {
    toastHost.append(elements.toastRegion);
  }
}

function showError(error) {
  toast(readError(error), true);
}

function readError(error) {
  return error instanceof Error ? error.message : String(error);
}
