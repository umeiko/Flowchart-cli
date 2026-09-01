const ui = {
  messages: document.querySelector("#messages"),
  composer: document.querySelector("#composer"),
  prompt: document.querySelector("#prompt"),
  send: document.querySelector("#send"),
  stop: document.querySelector("#stop"),
  file: document.querySelector("#file"),
  attachments: document.querySelector("#attachments"),
  contextStatus: document.querySelector("#context-status"),
  compactContext: document.querySelector("#compact-context"),
  status: document.querySelector("#status"),
  statusDot: document.querySelector("#status-dot"),
  canvas: document.querySelector("#canvas"),
  source: document.querySelector("#source"),
  sourceLink: document.querySelector("#source-link"),
  tree: document.querySelector("#file-tree"),
  refreshTree: document.querySelector("#refresh-tree"),
  previewKicker: document.querySelector("#preview-kicker"),
  previewTitle: document.querySelector("#preview-title"),
  backDiagram: document.querySelector("#back-diagram"),
  openFile: document.querySelector("#open-file"),
  markdownView: document.querySelector("#markdown-view"),
  csvView: document.querySelector("#csv-view"),
  codeView: document.querySelector("#code-view"),
  sourcePanel: document.querySelector("#source-panel"),
  filebar: document.querySelector(".filebar"),
  sidebarTitle: document.querySelector("#sidebar-title"),
  sidebarTabs: [...document.querySelectorAll(".sidebar-tab")],
  panels: {
    workspace: document.querySelector("#workspace-panel"),
    skills: document.querySelector("#skills-panel"),
    styles: document.querySelector("#styles-panel"),
  },
  saveResource: document.querySelector("#save-resource"),
  deleteResource: document.querySelector("#delete-resource"),
  resourceEditor: document.querySelector("#resource-editor"),
  attachWorkspaceFile: document.querySelector("#attach-workspace-file"),
  resourceDialog: document.querySelector("#resource-dialog"),
  resourceDialogForm: document.querySelector("#resource-dialog-form"),
  resourceDialogTitle: document.querySelector("#resource-dialog-title"),
  resourceName: document.querySelector("#resource-name"),
  resourceDescription: document.querySelector("#resource-description"),
  resourceDialogCancel: document.querySelector("#resource-dialog-cancel"),
  authDialog: document.querySelector("#auth-dialog"), authForm: document.querySelector("#auth-form"),
  authUsername: document.querySelector("#auth-username"), authPassword: document.querySelector("#auth-password"),
  authError: document.querySelector("#auth-error"), register: document.querySelector("#register"),
  sessionTabs: document.querySelector("#session-tabs"), newSession: document.querySelector("#new-session"),
  logout: document.querySelector("#logout"),
  userName: document.querySelector("#user-name"),
  userMenu: document.querySelector("#user-menu"), userSettings: document.querySelector("#user-settings"),
  userAvatarImage: document.querySelector("#user-avatar-image"), userAvatarFallback: document.querySelector("#user-avatar-fallback"),
  userSettingsDialog: document.querySelector("#user-settings-dialog"), userSettingsForm: document.querySelector("#user-settings-form"),
  settingsUsername: document.querySelector("#settings-username"), settingsAvatarImage: document.querySelector("#settings-avatar-image"),
  settingsAvatarFallback: document.querySelector("#settings-avatar-fallback"), avatarFile: document.querySelector("#avatar-file"),
  avatarRemove: document.querySelector("#avatar-remove"), userSettingsCancel: document.querySelector("#user-settings-cancel"),
  userSettingsError: document.querySelector("#user-settings-error"),
  renameSessionDialog: document.querySelector("#rename-session-dialog"), renameSessionForm: document.querySelector("#rename-session-form"),
  sessionTitleInput: document.querySelector("#session-title-input"), renameSessionCancel: document.querySelector("#rename-session-cancel"),
  deleteSessionDialog: document.querySelector("#delete-session-dialog"), deleteSessionForm: document.querySelector("#delete-session-form"),
  deleteSessionMessage: document.querySelector("#delete-session-message"), deleteSessionCancel: document.querySelector("#delete-session-cancel"),
  workspace: document.querySelector(".workspace"), conversation: document.querySelector(".conversation"), preview: document.querySelector(".preview"),
  leftResizer: document.querySelector("#left-resizer"), rightResizer: document.querySelector("#right-resizer"),
  composerResizer: document.querySelector("#composer-resizer"),
  fileActions: document.querySelector("#file-actions"), fileContextMenu: document.querySelector("#file-context-menu"),
  fileEntryDialog: document.querySelector("#file-entry-dialog"), fileEntryForm: document.querySelector("#file-entry-form"),
  fileEntryTitle: document.querySelector("#file-entry-title"), fileEntryName: document.querySelector("#file-entry-name"),
  fileEntryHelp: document.querySelector("#file-entry-help"), fileEntryCancel: document.querySelector("#file-entry-cancel"),
  toolDetailDialog: document.querySelector("#tool-detail-dialog"), toolDetailTitle: document.querySelector("#tool-detail-title"),
  toolDetailMeta: document.querySelector("#tool-detail-meta"), toolDetailRequest: document.querySelector("#tool-detail-request"),
  toolDetailResult: document.querySelector("#tool-detail-result"), toolDetailClose: document.querySelector("#tool-detail-close"),
};

let sessionId = null;
let pendingFiles = [];
let selectedFile = null;
let selectedResource = null;
let activeSection = "workspace";
let creatingResourceKind = null;
let managingSession = null;
let sessionLoadToken = 0;
let fileMenuTarget = null;
let fileClipboard = null;
let pendingFileDialog = null;
const resourceLoadTokens = {skills: 0, styles: 0};
const pendingResourceMutations = new Set();
let activeStream = null;
let activeRunId = null;
let activeRunSessionId = null;
let activeRunCleanup = null;
let currentUser = null;
let pendingAvatarFile = null;
let removeAvatarPending = false;
let avatarPreviewUrl = null;
let workspaceRefreshTimer = null;
let workspaceRefreshDiagram = false;
let renderedTreeSessionId = null;
let renderedTreeSignature = null;
let activeToolDetail = null;

const layoutDefaults = { filebar: 260, preview: 610, composer: 160 };
const layoutState = {...layoutDefaults};

function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

function saveLayout() {
  try { localStorage.setItem("flowchart-layout-v1", JSON.stringify(layoutState)); } catch (_) {}
}

function applyLayout() {
  ui.workspace.style.setProperty("--filebar-width", `${layoutState.filebar}px`);
  ui.workspace.style.setProperty("--preview-width", `${layoutState.preview}px`);
  ui.conversation.style.setProperty("--composer-height", `${layoutState.composer}px`);
  ui.leftResizer.setAttribute("aria-valuenow", String(Math.round(layoutState.filebar)));
  ui.rightResizer.setAttribute("aria-valuenow", String(Math.round(layoutState.preview)));
  ui.composerResizer.setAttribute("aria-valuenow", String(Math.round(layoutState.composer)));
}

function updateLayoutPart(part, value) {
  const totalWidth = ui.workspace.getBoundingClientRect().width;
  const conversationHeight = ui.conversation.getBoundingClientRect().height;
  if (part === "filebar") layoutState.filebar = clamp(value, 190, Math.min(440, totalWidth - layoutState.preview - 340));
  if (part === "preview") layoutState.preview = clamp(value, 360, Math.min(760, totalWidth - layoutState.filebar - 340));
  if (part === "composer") layoutState.composer = clamp(value, 150, Math.min(430, conversationHeight * .58));
  applyLayout();
}

function makeResizable(handle, part, axis, direction = 1) {
  handle.addEventListener("pointerdown", event => {
    if (window.matchMedia("(max-width: 900px)").matches) return;
    event.preventDefault();
    const startPointer = axis === "x" ? event.clientX : event.clientY;
    const startValue = layoutState[part];
    handle.classList.add("dragging");
    handle.setPointerCapture(event.pointerId);
    const move = moveEvent => {
      const pointer = axis === "x" ? moveEvent.clientX : moveEvent.clientY;
      updateLayoutPart(part, startValue + (pointer - startPointer) * direction);
    };
    const finish = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      saveLayout();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish, {once: true});
    handle.addEventListener("pointercancel", finish, {once: true});
  });
  handle.addEventListener("dblclick", () => {
    layoutState[part] = layoutDefaults[part];
    applyLayout(); saveLayout();
  });
  handle.addEventListener("keydown", event => {
    const previous = axis === "x" ? "ArrowLeft" : "ArrowUp";
    const next = axis === "x" ? "ArrowRight" : "ArrowDown";
    if (event.key !== previous && event.key !== next) return;
    event.preventDefault();
    const delta = (event.key === next ? 12 : -12) * direction;
    updateLayoutPart(part, layoutState[part] + delta); saveLayout();
  });
}

function initializeResizableLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem("flowchart-layout-v1") || "null");
    if (saved) Object.assign(layoutState, saved);
  } catch (_) {}
  updateLayoutPart("filebar", layoutState.filebar);
  updateLayoutPart("preview", layoutState.preview);
  updateLayoutPart("composer", layoutState.composer);
  makeResizable(ui.leftResizer, "filebar", "x", 1);
  makeResizable(ui.rightResizer, "preview", "x", -1);
  makeResizable(ui.composerResizer, "composer", "y", -1);
}

initializeResizableLayout();

function setStatus(text, ready = false) {
  ui.status.textContent = text;
  ui.statusDot.classList.toggle("ready", ready);
}

function addMessage(text, role = "assistant", files = []) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  const body = document.createElement("div");
  body.className = "message-body";
  if (role === "assistant") body.innerHTML = renderMarkdown(text);
  else body.textContent = text;
  node.append(body);
  if (files.length) {
    const refs = document.createElement("div");
    refs.className = "message-files";
    for (const file of files) {
      const ref = document.createElement("span");
      ref.className = "message-file";
      ref.textContent = `附件 · ${file.filename}`;
      ref.title = file.filename;
      refs.append(ref);
    }
    node.append(refs);
  }
  ui.messages.append(node);
  ui.messages.scrollTop = ui.messages.scrollHeight;
  return node;
}

function toolLabel(name) {
  return ({
    read_document: "读取文档", read_image: "读取图片", list_skill_packs: "检索 Skills",
    use_skill: "加载 Skill", list_styles: "检索 Styles", set_style: "应用 Style",
    create_diagram: "生成流程图", modify_diagram: "修改流程图", restyle_diagram: "调整图表风格",
    create_style: "生成 Style", create_skill: "生成 Skill", set_verification: "调整验证模式",
    get_current_diagram: "读取当前图", list_dir: "查看目录树", find_files: "查找文件", grep_files: "搜索文件内容",
    write_file: "写入文件", replace_in_file: "修改文件", ocr_image: "识别图片文字",
    run_command: "执行命令", delegate_task: "委派子 Agent", subagent_task: "处理委派任务",
    image_reasoning: "图像推理",
    write_working_doc: "整理工作文档",
  })[name] || name;
}

function prettyToolData(value, emptyText) {
  if (value === null || value === undefined || value === "") return emptyText;
  if (typeof value !== "string") return JSON.stringify(value, null, 2);
  try { return JSON.stringify(JSON.parse(value), null, 2); }
  catch (_error) { return value; }
}

function renderToolDetail(action) {
  if (!action) return;
  const owner = action.agent === "subagent" ? "子 Agent" : "主 Agent";
  const status = action.result === null ? "执行中" : "已完成";
  ui.toolDetailTitle.textContent = toolLabel(action.name);
  ui.toolDetailMeta.textContent = `${owner} · ${action.name} · ${status}`;
  ui.toolDetailRequest.textContent = prettyToolData(action.request, "（模型没有提供参数）");
  let liveResult = "";
  const isSubagentTask = action.name === "subagent_task";
  if (action.liveReasoning) {
    liveResult += `【${isSubagentTask ? "子 Agent 思考" : "视觉模型推理"}】\n${action.liveReasoning}\n\n`;
  }
  if (action.liveOutput) {
    liveResult += `【${isSubagentTask ? "子 Agent 当前输出" : "视觉模型输出"}】\n${action.liveOutput}`;
  }
  if (action.result === null) {
    ui.toolDetailResult.textContent = liveResult || "工具仍在执行，返回后会自动更新…";
  } else {
    const finalResult = prettyToolData(action.result, "（工具没有返回内容）");
    ui.toolDetailResult.textContent = liveResult
      ? `${liveResult}\n\n【最终返回】\n${finalResult}`
      : finalResult;
  }
}

function openToolDetail(action) {
  activeToolDetail = action;
  renderToolDetail(action);
  if (!ui.toolDetailDialog.open) ui.toolDetailDialog.showModal();
}

function makeToolActionInspectable(action) {
  action.node.classList.add("inspectable");
  action.node.setAttribute("role", "button");
  action.node.setAttribute("tabindex", "0");
  action.node.title = "点击查看工具请求和返回结果";
}

function addAgentAction(name, agent = "main", request = null, beforeNode = null) {
  const node = document.createElement("div");
  node.className = `agent-action running${agent === "subagent" ? " subagent" : ""}`;
  const icon = document.createElement("span"); icon.className = "agent-action-icon"; icon.textContent = "·";
  const prefix = agent === "subagent" ? "子 Agent 正在" : "正在";
  const text = document.createElement("span"); text.textContent = `${prefix}${toolLabel(name)}…`;
  node.append(icon, text);
  if (beforeNode?.parentNode === ui.messages) ui.messages.insertBefore(node, beforeNode);
  else ui.messages.append(node);
  ui.messages.scrollTop = ui.messages.scrollHeight;
  const action = {
    node, icon, text, name, agent, request, result: null,
    liveReasoning: "", liveOutput: "",
  };
  if (request !== null) makeToolActionInspectable(action);
  node.addEventListener("click", () => {
    if (node.classList.contains("inspectable")) openToolDetail(action);
  });
  node.addEventListener("keydown", event => {
    if (!node.classList.contains("inspectable") || !["Enter", " "].includes(event.key)) return;
    event.preventDefault(); openToolDetail(action);
  });
  return action;
}

ui.toolDetailClose.addEventListener("click", () => ui.toolDetailDialog.close());
ui.toolDetailDialog.addEventListener("close", () => { activeToolDetail = null; });
ui.toolDetailDialog.addEventListener("click", event => {
  if (event.target === ui.toolDetailDialog) ui.toolDetailDialog.close();
});

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    let detail = body.detail;
    if (Array.isArray(detail)) {
      const labels = {username: "用户名", password: "密码", name: "名称", description: "需求描述"};
      detail = detail.map(item => {
        const field = item.loc?.[item.loc.length - 1];
        const label = labels[field] || field || "输入";
        const message = item.type === "string_too_short"
          ? `至少需要 ${item.ctx?.min_length || "规定"} 个字符`
          : (item.msg || "格式不正确");
        return `${label}：${message}`;
      }).join("；");
    } else if (detail && typeof detail === "object") {
      detail = detail.msg || JSON.stringify(detail);
    }
    throw new Error(detail || `请求失败：${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

function compactTokenLabel(tokens) {
  if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}m`;
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)}k`;
  return String(tokens);
}

function renderContextStats(stats) {
  ui.contextStatus.textContent = `上下文 ≈${compactTokenLabel(stats.used_tokens)} / ${compactTokenLabel(stats.limit_tokens)} · ${stats.percent}%`;
  const controls = ui.contextStatus.closest(".context-controls");
  controls.classList.toggle("warning", stats.percent >= 70 && stats.percent < 90);
  controls.classList.toggle("danger", stats.percent >= 90);
}

async function refreshContext(targetSessionId = sessionId) {
  if (!targetSessionId) return;
  const stats = await api(`/v1/sessions/${targetSessionId}/context`);
  if (targetSessionId === sessionId) renderContextStats(stats);
  return stats;
}

function userInitial(username) {
  return [...(username || "账").trim()][0]?.toUpperCase() || "账";
}

function setAvatarView(image, fallback, username, source) {
  fallback.textContent = userInitial(username);
  if (!source) {
    image.removeAttribute("src"); image.classList.add("hidden"); fallback.classList.remove("hidden");
    return;
  }
  image.onload = () => { image.classList.remove("hidden"); fallback.classList.add("hidden"); };
  image.onerror = () => { image.classList.add("hidden"); fallback.classList.remove("hidden"); };
  image.src = source;
}

function renderCurrentUser(user, cacheBust = false) {
  currentUser = user;
  ui.userName.textContent = user.username;
  const source = user.avatar_url
    ? `${user.avatar_url}${user.avatar_url.includes("?") ? "&" : "?"}t=${cacheBust ? Date.now() : "current"}`
    : null;
  setAvatarView(ui.userAvatarImage, ui.userAvatarFallback, user.username, source);
}

function resetAvatarPreview() {
  if (avatarPreviewUrl) URL.revokeObjectURL(avatarPreviewUrl);
  avatarPreviewUrl = null;
  pendingAvatarFile = null;
  removeAvatarPending = false;
  ui.avatarFile.value = "";
  ui.userSettingsError.textContent = "";
}

function openUserSettings() {
  if (!currentUser) return;
  resetAvatarPreview();
  ui.userMenu.open = false;
  ui.settingsUsername.textContent = currentUser.username;
  setAvatarView(
    ui.settingsAvatarImage, ui.settingsAvatarFallback, currentUser.username,
    currentUser.avatar_url ? `${currentUser.avatar_url}?t=${Date.now()}` : null,
  );
  ui.avatarRemove.disabled = !currentUser.avatar_url;
  ui.userSettingsDialog.showModal();
}

async function createSession() {
  const session = await api("/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  await loadSession(session.id);
  return session;
}

async function refreshSessionTabs() {
  const sessions = await api("/v1/sessions");
  document.querySelectorAll(".session-floating-menu").forEach(node => node.remove());
  ui.sessionTabs.replaceChildren();
  for (const item of sessions) {
    const wrapper = document.createElement("div");
    wrapper.className = "session-item";
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-tab${item.id === sessionId ? " active" : ""}`;
    button.textContent = item.title;
    button.addEventListener("click", () => loadSession(item.id).catch(error => setStatus(error.message)));
    const more = document.createElement("button");
    more.type = "button"; more.className = "session-more"; more.textContent = "⋯"; more.title = "管理会话";
    const menu = document.createElement("div"); menu.className = "session-menu session-floating-menu hidden";
    const rename = document.createElement("button"); rename.type = "button"; rename.textContent = "重命名";
    rename.addEventListener("click", () => { menu.classList.add("hidden"); openRenameSession(item); });
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "删除会话";
    remove.addEventListener("click", () => { menu.classList.add("hidden"); openDeleteSession(item); });
    more.addEventListener("click", event => {
      event.stopPropagation();
      document.querySelectorAll(".session-floating-menu").forEach(node => { if (node !== menu) node.classList.add("hidden"); });
      const opening = menu.classList.contains("hidden");
      menu.classList.toggle("hidden", !opening);
      if (opening) {
        const rect = more.getBoundingClientRect();
        menu.style.top = `${rect.bottom + 6}px`;
        menu.style.left = `${Math.max(8, Math.min(rect.right - 132, window.innerWidth - 140))}px`;
      }
    });
    menu.addEventListener("click", event => event.stopPropagation());
    menu.append(rename, remove); wrapper.append(button, more); ui.sessionTabs.append(wrapper); document.body.append(menu);
  }
  return sessions;
}
document.addEventListener("click", () => document.querySelectorAll(".session-floating-menu").forEach(node => node.classList.add("hidden")));

async function loadSession(id) {
  const loadToken = ++sessionLoadToken;
  if (activeRunCleanup) activeRunCleanup();
  else if (activeStream) activeStream.close();
  if (workspaceRefreshTimer) clearTimeout(workspaceRefreshTimer);
  workspaceRefreshTimer = null;
  workspaceRefreshDiagram = false;
  activeStream = null;
  activeRunCleanup = null;
  activeRunId = null;
  activeRunSessionId = null;
  setRunControls(false);
  sessionId = id;
  localStorage.setItem("flowchart:last-session", id);
  clearDiagramPreview();
  const messages = await api(`/v1/sessions/${id}/messages`);
  if (loadToken !== sessionLoadToken || sessionId !== id) return;
  pendingFiles = [];
  ui.attachments.replaceChildren();
  selectedFile = null;
  selectedResource = null;
  ui.messages.replaceChildren();
  if (!messages.length) addMessage("描述你要创建的流程图，也可以附带需求文档或参考图片。");
  for (const message of messages) addMessage(message.content, message.role, message.attachments.map(filename => ({filename})));
  await Promise.all([
    refreshTree(), refreshResources("skills"), refreshResources("styles"),
    refreshDiagram(id), refreshContext(id),
  ]);
  if (loadToken !== sessionLoadToken || sessionId !== id) return;
  const activeRun = await api(`/v1/sessions/${id}/active-run`);
  if (loadToken !== sessionLoadToken || sessionId !== id) return;
  if (activeRun) followRun(activeRun.id, id);
  await refreshSessionTabs();
  if (!activeRun) setStatus("已连接", true);
}

async function boot() {
  let user;
  try { user = await api("/v1/auth/me"); }
  catch (_) { ui.authDialog.showModal(); return; }
  renderCurrentUser(user);
  const sessions = await refreshSessionTabs();
  const last = localStorage.getItem("flowchart:last-session");
  const chosen = sessions.find(item => item.id === last) || sessions[0];
  if (chosen) await loadSession(chosen.id);
  else await createSession();
}

async function authenticate(mode) {
  ui.authError.textContent = "";
  try {
    await api(`/v1/auth/${mode}`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:ui.authUsername.value,password:ui.authPassword.value})});
    ui.authDialog.close(); await boot();
  } catch (error) { ui.authError.textContent = error.message; }
}
ui.authForm.addEventListener("submit", event => { event.preventDefault(); authenticate("login"); });
ui.register.addEventListener("click", () => authenticate("register"));
ui.newSession.addEventListener("click", async () => {
  ui.newSession.disabled = true;
  try { await createSession(); } catch (error) { setStatus(error.message); }
  finally { ui.newSession.disabled = false; }
});
ui.logout.addEventListener("click", async () => { await api("/v1/auth/logout", {method:"POST"}); location.reload(); });
ui.userSettings.addEventListener("click", openUserSettings);
ui.userSettingsCancel.addEventListener("click", () => { resetAvatarPreview(); ui.userSettingsDialog.close(); });
ui.avatarFile.addEventListener("change", () => {
  const file = ui.avatarFile.files?.[0];
  if (!file) return;
  const allowed = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
  if (!allowed.has(file.type) || file.size > 2 * 1024 * 1024) {
    ui.userSettingsError.textContent = !allowed.has(file.type) ? "请选择 PNG、JPEG、WebP 或 GIF 图片" : "头像不能超过 2 MB";
    ui.avatarFile.value = ""; return;
  }
  if (avatarPreviewUrl) URL.revokeObjectURL(avatarPreviewUrl);
  avatarPreviewUrl = URL.createObjectURL(file);
  pendingAvatarFile = file; removeAvatarPending = false;
  ui.avatarRemove.disabled = false;
  ui.userSettingsError.textContent = "";
  setAvatarView(ui.settingsAvatarImage, ui.settingsAvatarFallback, currentUser.username, avatarPreviewUrl);
});
ui.avatarRemove.addEventListener("click", () => {
  if (avatarPreviewUrl) URL.revokeObjectURL(avatarPreviewUrl);
  avatarPreviewUrl = null; pendingAvatarFile = null; removeAvatarPending = true;
  ui.avatarFile.value = ""; ui.avatarRemove.disabled = true;
  setAvatarView(ui.settingsAvatarImage, ui.settingsAvatarFallback, currentUser.username, null);
});
ui.userSettingsForm.addEventListener("submit", async event => {
  event.preventDefault();
  const submit = ui.userSettingsForm.querySelector('button[type="submit"]');
  submit.disabled = true; ui.userSettingsError.textContent = "";
  try {
    let user = currentUser;
    if (pendingAvatarFile) {
      user = await api("/v1/auth/avatar", {method: "PUT", headers: {"Content-Type": pendingAvatarFile.type}, body: pendingAvatarFile});
    } else if (removeAvatarPending) {
      user = await api("/v1/auth/avatar", {method: "DELETE"});
    }
    renderCurrentUser(user, true); resetAvatarPreview(); ui.userSettingsDialog.close();
    setStatus("用户设置已保存", true);
  } catch (error) { ui.userSettingsError.textContent = error.message; }
  finally { submit.disabled = false; }
});

function openRenameSession(item) {
  managingSession = item;
  ui.sessionTitleInput.value = item.title;
  ui.renameSessionDialog.showModal();
  ui.sessionTitleInput.select();
}
function openDeleteSession(item) {
  managingSession = item;
  ui.deleteSessionMessage.textContent = `即将删除“${item.title}”。`;
  ui.deleteSessionDialog.showModal();
}
ui.renameSessionCancel.addEventListener("click", () => ui.renameSessionDialog.close());
ui.deleteSessionCancel.addEventListener("click", () => ui.deleteSessionDialog.close());
ui.renameSessionForm.addEventListener("submit", async event => {
  event.preventDefault(); if (!managingSession) return;
  try {
    await api(`/v1/sessions/${managingSession.id}/title`, {method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:ui.sessionTitleInput.value})});
    ui.renameSessionDialog.close(); await refreshSessionTabs(); setStatus("会话已重命名", true);
  } catch (error) { setStatus(error.message); }
});
ui.deleteSessionForm.addEventListener("submit", async event => {
  event.preventDefault(); if (!managingSession) return;
  const deletingId = managingSession.id;
  try {
    await api(`/v1/sessions/${deletingId}`, {method:"DELETE"});
    ui.deleteSessionDialog.close();
    const sessions = await refreshSessionTabs();
    if (deletingId === sessionId) {
      if (sessions.length) await loadSession(sessions[0].id); else await createSession();
    }
    setStatus("会话已永久删除", true);
  } catch (error) { setStatus(error.message); }
});

function switchSection(section) {
  activeSection = section;
  for (const tab of ui.sidebarTabs) tab.classList.toggle("active", tab.dataset.section === section);
  for (const [name, panel] of Object.entries(ui.panels)) panel.classList.toggle("active", name === section);
  ui.sidebarTitle.textContent = section === "workspace" ? "output" : section;
}

for (const tab of ui.sidebarTabs) {
  tab.addEventListener("click", () => switchSection(tab.dataset.section));
}

async function refreshResources(kind, targetSessionId = sessionId) {
  if (!targetSessionId) return;
  const requestToken = ++resourceLoadTokens[kind];
  const resources = await api(`/v1/sessions/${targetSessionId}/client/${kind}`);
  if (targetSessionId !== sessionId || requestToken !== resourceLoadTokens[kind]) return;
  const panel = ui.panels[kind];
  panel.replaceChildren();
  const toolbar = document.createElement("div");
  toolbar.className = "resource-toolbar";
  const create = document.createElement("button");
  create.type = "button";
  create.className = "resource-tool";
  create.textContent = "＋ 新建";
  create.addEventListener("click", () => openCreateResourceDialog(kind));
  const importLabel = document.createElement("label");
  importLabel.className = "resource-tool";
  importLabel.textContent = "↑ 导入";
  const importInput = document.createElement("input");
  importInput.type = "file";
  importInput.accept = ".md,text/markdown";
  importInput.multiple = true;
  importInput.addEventListener("change", () => {
    importResources(kind, [...importInput.files]);
    importInput.value = "";
  });
  importLabel.append(importInput);
  toolbar.append(create, importLabel);
  panel.append(toolbar);
  if (!resources.length) {
    const empty = document.createElement("p");
    empty.className = "resource-empty";
    empty.textContent = `没有可用的 ${kind}`;
    panel.append(empty);
  }
  for (const resource of resources) {
    const row = document.createElement("div");
    row.className = "resource-item";
    const mounted = document.createElement("button");
    mounted.type = "button";
    mounted.className = `tree-attach resource-mount${resource.mounted ? " attached" : ""}`;
    const renderMounted = () => {
      mounted.textContent = resource.mounted ? "✓" : "+";
      mounted.title = resource.mounted ? "取消挂载" : "挂载到 Agent 上下文";
      mounted.setAttribute("aria-label", `${resource.mounted ? "取消挂载" : "挂载"} ${resource.name}`);
      mounted.classList.toggle("attached", resource.mounted);
    };
    renderMounted();
    mounted.addEventListener("click", async () => {
      mounted.disabled = true;
      const nextMounted = !resource.mounted;
      const mutation = (async () => {
        try {
          await api(
            `/v1/sessions/${targetSessionId}/client/${kind}/${encodeURIComponent(resource.name)}`,
            {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ mounted: nextMounted }),
            }
          );
          await refreshResources(kind, targetSessionId);
          setStatus(nextMounted ? `已加载到 Agent：${resource.name}` : `已从 Agent 卸载：${resource.name}`, true);
        } catch (error) {
          setStatus(error.message);
        } finally {
          mounted.disabled = false;
        }
      })();
      pendingResourceMutations.add(mutation);
      try { await mutation; } finally { pendingResourceMutations.delete(mutation); }
    });
    const open = document.createElement("button");
    open.type = "button";
    open.className = "resource-open";
    open.textContent = resource.name;
    open.title = resource.name;
    open.addEventListener("click", () => openResource(kind, resource.name, row));
    row.append(open, mounted);
    panel.append(row);
  }
  const hint = document.createElement("div");
  hint.className = "resource-drop-hint";
  hint.textContent = `拖入 .md，导入到当前 ${kind}`;
  panel.append(hint);
}

function resourceTemplate(kind, name) {
  if (kind === "styles") {
    return `---\nname: ${name}\ndescription: 请填写风格说明和适用场景\nbackground: "#ffffff"\ninit: ""\n---\n\n请在这里填写风格规则。\n`;
  }
  return `---\nname: ${name}\ndescription: 请填写技能说明和触发场景\n---\n\n请在这里填写 Agent 操作指引。\n`;
}

async function uploadResource(kind, file) {
  if (!file.name.toLowerCase().endsWith(".md")) throw new Error(`${file.name} 不是 .md 文件`);
  const content = await file.text();
  return api(`/v1/sessions/${sessionId}/client/${kind}?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "Content-Type": "text/markdown; charset=utf-8" },
    body: content,
  });
}

async function importResources(kind, files) {
  if (!files.length) return;
  try {
    for (const file of files) await uploadResource(kind, file);
    await refreshResources(kind);
    setStatus(`已导入 ${files.length} 个 ${kind}`, true);
  } catch (error) {
    setStatus(error.message);
  }
}

function openCreateResourceDialog(kind) {
  creatingResourceKind = kind;
  const label = kind === "skills" ? "Skill" : "Style";
  ui.resourceDialogTitle.textContent = `新建 ${label}`;
  ui.resourceName.value = `my-${kind === "skills" ? "skill" : "style"}`;
  ui.resourceDescription.value = "";
  ui.resourceDialog.showModal();
  ui.resourceName.select();
}

ui.resourceDialogCancel.addEventListener("click", () => ui.resourceDialog.close());
ui.resourceDialogForm.addEventListener("submit", async event => {
  event.preventDefault();
  const kind = creatingResourceKind;
  const entered = ui.resourceName.value;
  const action = event.submitter?.value || "generate";
  if (!kind || !entered) return;
  const key = entered.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!key) return setStatus("名称至少需要包含字母、数字、下划线或连字符");
  try {
    let resource;
    if (action === "generate") {
      const description = ui.resourceDescription.value.trim();
      if (!description) return setStatus("请先填写生成需求，或选择“空白模板”");
      setStatus(`正在生成 ${key}…`);
      resource = await api(`/v1/sessions/${sessionId}/client/${kind}/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: key, description }),
      });
    } else {
      resource = await api(
        `/v1/sessions/${sessionId}/client/${kind}?filename=${encodeURIComponent(`${key}.md`)}`,
        { method: "POST", headers: { "Content-Type": "text/markdown; charset=utf-8" }, body: resourceTemplate(kind, key) }
      );
    }
    await refreshResources(kind);
    const row = [...ui.panels[kind].querySelectorAll(".resource-item")]
      .find(node => node.querySelector(".resource-open")?.textContent === resource.name);
    if (row) await openResource(kind, resource.name, row);
    ui.resourceDialog.close();
    setStatus(`${action === "generate" ? "已生成" : "已新建"} ${resource.name}`, true);
  } catch (error) {
    setStatus(error.message);
  }
});

for (const kind of ["skills", "styles"]) {
  const panel = ui.panels[kind];
  for (const eventName of ["dragenter", "dragover"]) {
    panel.addEventListener(eventName, event => {
      event.preventDefault();
      panel.classList.add("dragging");
    });
  }
  panel.addEventListener("dragleave", event => {
    if (!panel.contains(event.relatedTarget)) panel.classList.remove("dragging");
  });
  panel.addEventListener("drop", event => {
    event.preventDefault();
    event.stopPropagation();
    panel.classList.remove("dragging");
    importResources(kind, [...event.dataTransfer.files]);
  });
}

async function openResource(kind, name, row) {
  const resource = await api(`/v1/sessions/${sessionId}/client/${kind}/${encodeURIComponent(name)}`);
  selectedResource = resource;
  document.querySelectorAll(".resource-item.active").forEach(node => node.classList.remove("active"));
  row.classList.add("active");
  ui.previewKicker.textContent = kind === "skills" ? "CLIENT SKILL" : "CLIENT STYLE";
  ui.previewTitle.textContent = name;
  ui.backDiagram.classList.remove("hidden");
  ui.saveResource.classList.remove("hidden");
  ui.deleteResource.classList.toggle("hidden", resource.builtin);
  ui.attachWorkspaceFile.classList.add("hidden");
  ui.openFile.classList.add("hidden");
  ui.sourceLink.classList.add("hidden");
  resetPreviewViews();
  ui.resourceEditor.value = resource.content || "";
  ui.resourceEditor.classList.remove("hidden");
}

function fileIcon(name) {
  const ext = name.split(".").pop().toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(ext)) return "◫";
  if (["md", "markdown"].includes(ext)) return "M";
  if (["xml", "drawio", "mmd"].includes(ext)) return "<>";
  return "·";
}

function entryName(path) { return path.split("/").filter(Boolean).pop() || path; }
function entryParent(path) { const parts = path.split("/").filter(Boolean); parts.pop(); return parts.join("/"); }
function entryJoin(directory, name) { return `${directory.replace(/\/$/, "")}/${name}`; }
function isWorkspaceRoot(path) { return !path.includes("/"); }

function validateEntryName(name) {
  const clean = name.trim();
  if (!clean || clean === "." || clean === ".." || /[\\/\0]/.test(clean)) {
    throw new Error("名称不能为空，也不能包含斜杠");
  }
  return clean;
}

function closeFileMenu() { ui.fileContextMenu.classList.add("hidden"); }

function openFileMenu(target, x, y) {
  fileMenuTarget = target;
  const root = isWorkspaceRoot(target.path);
  for (const button of ui.fileContextMenu.querySelectorAll("button[data-file-action]")) {
    const action = button.dataset.fileAction;
    button.disabled = (
      (["new-file", "new-directory", "paste"].includes(action) && target.type !== "directory") ||
      (action === "paste" && !fileClipboard) ||
      (["copy", "cut", "rename", "delete"].includes(action) && root) ||
      (action === "download" && !["file", "directory"].includes(target.type))
    );
    if (action === "download") {
      button.textContent = target.type === "directory" ? "下载目录（ZIP）" : "下载文件";
    }
  }
  ui.fileContextMenu.classList.remove("hidden");
  const width = 168;
  const height = ui.fileContextMenu.offsetHeight;
  ui.fileContextMenu.style.left = `${Math.max(6, Math.min(x, innerWidth - width - 6))}px`;
  ui.fileContextMenu.style.top = `${Math.max(6, Math.min(y, innerHeight - height - 6))}px`;
}

function openFileEntryDialog(action, target) {
  pendingFileDialog = {action, target};
  const labels = {"new-file": "新建文本文件", "new-directory": "新建目录", rename: "重命名"};
  ui.fileEntryTitle.textContent = labels[action];
  ui.fileEntryName.value = action === "new-file" ? "untitled.txt" : action === "new-directory" ? "new-folder" : target.name;
  ui.fileEntryHelp.textContent = action === "rename" ? `当前位置：${entryParent(target.path)}` : `创建在：${target.path}`;
  ui.fileEntryDialog.showModal();
  ui.fileEntryName.select();
}

async function transferWorkspace(source, target, operation) {
  if (source === target) return;
  await api(`/v1/sessions/${sessionId}/workspace/transfer`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({source, target, operation}),
  });
  const selectedAffected = operation === "move" && selectedFile && (selectedFile === source || selectedFile.startsWith(`${source}/`));
  if (operation === "move") {
    for (const file of pendingFiles) {
      if (file.workspacePath === source || file.workspacePath?.startsWith(`${source}/`)) {
        file.workspacePath = `${target}${file.workspacePath.slice(source.length)}`;
      }
    }
  }
  if (selectedAffected) { selectedFile = null; await refreshDiagram(); }
  await refreshTree();
  setStatus(`${operation === "move" ? "已移动" : "已复制"}：${entryName(target)}`, true);
}

async function executeFileAction(action, target) {
  closeFileMenu();
  if (["new-file", "new-directory", "rename"].includes(action)) return openFileEntryDialog(action, target);
  if (action === "copy" || action === "cut") {
    fileClipboard = {path: target.path, type: target.type, operation: action === "copy" ? "copy" : "move"};
    await refreshTree();
    return setStatus(`${action === "copy" ? "已复制" : "已剪切"}：${target.name}`, true);
  }
  if (action === "paste") {
    if (!fileClipboard) return;
    const targetPath = entryJoin(target.path, entryName(fileClipboard.path));
    await transferWorkspace(fileClipboard.path, targetPath, fileClipboard.operation);
    if (fileClipboard.operation === "move") fileClipboard = null;
    return;
  }
  if (action === "download") {
    const link = document.createElement("a");
    link.href = `/v1/sessions/${sessionId}/workspace/files/download?path=${encodeURIComponent(target.path)}`;
    link.download = target.type === "directory" ? `${target.name}.zip` : target.name;
    document.body.appendChild(link); link.click(); link.remove();
    setStatus(`正在下载：${link.download}`, true);
    return;
  }
  if (action === "delete") {
    const description = target.type === "directory" ? "目录及其全部内容" : "文件";
    if (!confirm(`永久删除${description}“${target.name}”？此操作不可恢复。`)) return;
    await api(`/v1/sessions/${sessionId}/workspace/entries?path=${encodeURIComponent(target.path)}`, {method: "DELETE"});
    if (selectedFile === target.path) { selectedFile = null; await refreshDiagram(); }
    if (fileClipboard?.path === target.path || fileClipboard?.path.startsWith(`${target.path}/`)) fileClipboard = null;
    await refreshTree(); setStatus(`已删除：${target.name}`, true);
  }
}

ui.fileEntryCancel.addEventListener("click", () => ui.fileEntryDialog.close());
ui.fileEntryForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (!pendingFileDialog) return;
  try {
    const name = validateEntryName(ui.fileEntryName.value);
    const {action, target} = pendingFileDialog;
    if (action === "rename") {
      await transferWorkspace(target.path, entryJoin(entryParent(target.path), name), "move");
    } else {
      const path = entryJoin(target.path, name);
      await api(`/v1/sessions/${sessionId}/workspace/entries`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path, type: action === "new-file" ? "file" : "directory"}),
      });
      await refreshTree(); setStatus(`已新建：${name}`, true);
    }
    ui.fileEntryDialog.close();
  } catch (error) { ui.fileEntryHelp.textContent = error.message; }
});

ui.fileContextMenu.addEventListener("click", event => {
  const button = event.target.closest("button[data-file-action]");
  if (!button || button.disabled || !fileMenuTarget) return;
  executeFileAction(button.dataset.fileAction, fileMenuTarget).catch(error => setStatus(error.message));
});
ui.fileActions.addEventListener("click", event => {
  const rect = event.currentTarget.getBoundingClientRect();
  openFileMenu({path: "workspace", name: "workspace", type: "directory"}, rect.left, rect.bottom + 4);
  event.stopPropagation();
});
document.addEventListener("click", event => { if (!ui.fileContextMenu.contains(event.target)) closeFileMenu(); });
document.addEventListener("keydown", event => { if (event.key === "Escape") closeFileMenu(); });
ui.tree.addEventListener("contextmenu", event => {
  if (event.target.closest(".tree-file, .tree-dir > summary")) return;
  event.preventDefault(); openFileMenu({path: "workspace", name: "workspace", type: "directory"}, event.clientX, event.clientY);
});

function bindTreeEntry(element, node, {dropDirectory = false} = {}) {
  const root = isWorkspaceRoot(node.path);
  element.dataset.path = node.path;
  element.addEventListener("contextmenu", event => {
    event.preventDefault(); event.stopPropagation();
    openFileMenu(node, event.clientX, event.clientY);
  });
  if (!root) {
    element.draggable = true;
    element.addEventListener("dragstart", event => {
      event.stopPropagation();
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("application/x-flowchart-path", node.path);
      event.dataTransfer.setData("text/plain", node.path);
    });
  }
  if (dropDirectory) {
    element.addEventListener("dragover", event => {
      if (!event.dataTransfer.types.includes("application/x-flowchart-path")) return;
      event.preventDefault(); event.stopPropagation(); element.classList.add("drag-target");
      event.dataTransfer.dropEffect = "move";
    });
    element.addEventListener("dragleave", () => element.classList.remove("drag-target"));
    element.addEventListener("drop", event => {
      const source = event.dataTransfer.getData("application/x-flowchart-path");
      if (!source) return;
      event.preventDefault(); event.stopPropagation(); element.classList.remove("drag-target");
      transferWorkspace(source, entryJoin(node.path, entryName(source)), "move").catch(error => setStatus(error.message));
    });
  }
}

function renderTreeNodes(nodes, parent) {
  for (const node of nodes) {
    if (node.type === "directory") {
      const details = document.createElement("details");
      details.className = "tree-dir";
      details.open = node.path.split("/").length <= 1;
      const summary = document.createElement("summary");
      summary.textContent = node.name;
      summary.title = node.path;
      bindTreeEntry(summary, node, {dropDirectory: true});
      if (fileClipboard?.operation === "move" && fileClipboard.path === node.path) summary.classList.add("clipboard-cut");
      const children = document.createElement("div");
      children.className = "tree-children";
      renderTreeNodes(node.children, children);
      details.append(summary, children);
      parent.append(details);
    } else {
      const row = document.createElement("div");
      row.className = "tree-file-row";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tree-file";
      button.dataset.path = node.path;
      button.title = `${node.path} · ${node.size} bytes`;
      const icon = document.createElement("span");
      icon.className = "file-icon";
      icon.textContent = fileIcon(node.name);
      const name = document.createElement("span");
      name.className = "file-name";
      name.textContent = node.name;
      button.append(icon, name);
      bindTreeEntry(button, node);
      if (fileClipboard?.operation === "move" && fileClipboard.path === node.path) button.classList.add("clipboard-cut");
      button.addEventListener("click", () => previewFile(node.path, button));
      button.addEventListener("dblclick", event => {
        event.preventDefault();
        toggleWorkspaceAttachment(node.path, attach);
      });
      const attach = document.createElement("button");
      attach.type = "button";
      attach.className = "tree-attach";
      attach.dataset.path = node.path;
      attach.textContent = "+";
      attach.title = "加入聊天附件";
      attach.setAttribute("aria-label", `把 ${node.name} 加入聊天附件`);
      attach.addEventListener("click", () => toggleWorkspaceAttachment(node.path, attach));
      row.append(button, attach);
      parent.append(row);
    }
  }
}

function treeStructureSignature(nodes) {
  const entries = [];
  const walk = items => items.forEach(node => {
    entries.push(`${node.type}:${node.path}`);
    if (node.children?.length) walk(node.children);
  });
  walk(nodes);
  return entries.join("\n");
}

function scheduleWorkspaceRefresh(targetSessionId, refreshDiagramToo = false, delay = 180) {
  if (!targetSessionId || targetSessionId !== sessionId) return;
  workspaceRefreshDiagram ||= refreshDiagramToo;
  if (workspaceRefreshTimer) clearTimeout(workspaceRefreshTimer);
  workspaceRefreshTimer = setTimeout(async () => {
    workspaceRefreshTimer = null;
    const shouldRefreshDiagram = workspaceRefreshDiagram;
    workspaceRefreshDiagram = false;
    if (targetSessionId !== sessionId) return;
    try {
      await refreshTree(targetSessionId);
      if (shouldRefreshDiagram) await refreshDiagram(targetSessionId);
    } catch (error) {
      console.warn("自动刷新 Workspace 失败", error);
    }
  }, delay);
}

async function refreshTree(targetSessionId = sessionId) {
  if (!targetSessionId) return;
  const expanded = new Set([...ui.tree.querySelectorAll(".tree-dir[open] > summary")].map(node => node.dataset.path));
  const scrollTop = ui.tree.scrollTop;
  const nodes = await api(`/v1/sessions/${targetSessionId}/workspace/tree`);
  if (targetSessionId !== sessionId) return;
  const signature = treeStructureSignature(nodes);
  if (renderedTreeSessionId === targetSessionId && renderedTreeSignature === signature) {
    updateWorkspaceAttachButtons();
    return;
  }
  renderedTreeSessionId = targetSessionId;
  renderedTreeSignature = signature;
  ui.tree.replaceChildren();
  if (!nodes.length) {
    const empty = document.createElement("p");
    empty.className = "tree-empty";
    empty.textContent = "output 目录目前为空";
    ui.tree.append(empty);
    return;
  }
  renderTreeNodes(nodes, ui.tree);
  for (const summary of ui.tree.querySelectorAll(".tree-dir > summary")) {
    if (expanded.has(summary.dataset.path)) summary.parentElement.open = true;
  }
  ui.tree.scrollTop = scrollTop;
  updateWorkspaceAttachButtons();
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
}

function renderMarkdown(source) {
  const escaped = escapeHtml(source);
  const lines = escaped.split(/\r?\n/);
  let html = "";
  let inCode = false;
  let inList = false;
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inList) { html += "</ul>"; inList = false; }
      html += inCode ? "</code></pre>" : "<pre><code>";
      inCode = !inCode;
      continue;
    }
    if (inCode) { html += `${line}\n`; continue; }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      if (inList) { html += "</ul>"; inList = false; }
      const level = heading[1].length;
      html += `<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`;
      continue;
    }
    const item = line.match(/^[-*]\s+(.+)$/);
    if (item) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${renderInlineMarkdown(item[1])}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.startsWith("&gt; ")) html += `<blockquote>${renderInlineMarkdown(line.slice(5))}</blockquote>`;
    else if (line.trim()) html += `<p>${renderInlineMarkdown(line)}</p>`;
  }
  if (inList) html += "</ul>";
  if (inCode) html += "</code></pre>";
  return html;
}

function renderInlineMarkdown(source) {
  return source
    .replace(
      /\[([^\]]+)\]\(workspace-file:([A-Za-z0-9%._~-]+)\)/g,
      '<a href="#workspace-file" class="message-file-link" data-workspace-path="$2">$1</a>',
    )
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

const CSV_PREVIEW_MAX_ROWS = 500;
const CSV_PREVIEW_MAX_COLUMNS = 80;

function parseCsv(source, maxRows = CSV_PREVIEW_MAX_ROWS, maxColumns = CSV_PREVIEW_MAX_COLUMNS) {
  const text = source.replace(/^\uFEFF/, "");
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  let rowsTruncated = false;
  let columnsTruncated = false;

  const finishRow = () => {
    row.push(field);
    if (row.length > maxColumns) columnsTruncated = true;
    rows.push(row.slice(0, maxColumns));
    row = [];
    field = "";
    return rows.length >= maxRows;
  };

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"'; index += 1;
      } else if (char === '"') quoted = false;
      else field += char;
      continue;
    }
    if (char === '"' && field === "") quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[index + 1] === "\n") index += 1;
      if (finishRow()) {
        rowsTruncated = index < text.length - 1;
        break;
      }
    } else field += char;
  }
  if (!rowsTruncated && (field !== "" || row.length || text.endsWith(","))) finishRow();
  return {rows, rowsTruncated, columnsTruncated};
}

function renderCsvPreview(source) {
  const {rows, rowsTruncated, columnsTruncated} = parseCsv(source);
  ui.csvView.replaceChildren();
  const summary = document.createElement("div");
  summary.className = "csv-preview-summary";
  if (!rows.length) {
    summary.textContent = "CSV 文件为空";
    ui.csvView.append(summary);
    return;
  }
  const columnCount = Math.max(...rows.map(row => row.length));
  const limited = rowsTruncated || columnsTruncated;
  summary.textContent = limited
    ? `已显示前 ${rows.length.toLocaleString()} 行 × ${columnCount.toLocaleString()} 列；完整内容请打开原文件`
    : `${rows.length.toLocaleString()} 行 × ${columnCount.toLocaleString()} 列`;

  const wrap = document.createElement("div");
  wrap.className = "csv-table-wrap";
  const table = document.createElement("table");
  table.className = "csv-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "csv-row-number";
  corner.textContent = "#";
  headRow.append(corner);
  for (const value of rows[0]) {
    const cell = document.createElement("th");
    cell.textContent = value;
    headRow.append(cell);
  }
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.slice(1).forEach((values, rowIndex) => {
    const tableRow = document.createElement("tr");
    const number = document.createElement("th");
    number.className = "csv-row-number";
    number.scope = "row";
    number.textContent = String(rowIndex + 1);
    tableRow.append(number);
    for (let column = 0; column < columnCount; column += 1) {
      const cell = document.createElement("td");
      cell.textContent = values[column] ?? "";
      tableRow.append(cell);
    }
    body.append(tableRow);
  });
  table.append(head, body);
  wrap.append(table);
  ui.csvView.append(summary, wrap);
}

function resetPreviewViews() {
  ui.canvas.classList.add("hidden");
  ui.markdownView.classList.add("hidden");
  ui.csvView.classList.add("hidden");
  ui.codeView.classList.add("hidden");
  ui.resourceEditor.classList.add("hidden");
  ui.sourcePanel.classList.add("hidden");
}

async function previewFile(path, button) {
  selectedFile = path;
  selectedResource = null;
  document.querySelectorAll(".tree-file.active").forEach(node => node.classList.remove("active"));
  button.classList.add("active");
  const url = `/v1/sessions/${sessionId}/workspace/files/content?path=${encodeURIComponent(path)}`;
  const name = path.split("/").pop();
  const ext = name.split(".").pop().toLowerCase();
  ui.previewKicker.textContent = "OUTPUT FILE";
  ui.previewTitle.textContent = name;
  ui.backDiagram.classList.remove("hidden");
  ui.saveResource.classList.add("hidden");
  ui.deleteResource.classList.add("hidden");
  ui.attachWorkspaceFile.classList.remove("hidden");
  updateWorkspaceAttachButtons();
  ui.openFile.href = url;
  ui.openFile.classList.remove("hidden");
  ui.sourceLink.classList.add("hidden");
  resetPreviewViews();
  setStatus(`读取 ${name}`);
  try {
    if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(ext)) {
      ui.canvas.replaceChildren();
      ui.canvas.classList.remove("hidden", "empty");
      const image = new Image();
      image.alt = name;
      image.src = `${url}&t=${Date.now()}`;
      ui.canvas.append(image);
    } else {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`读取失败：${response.status}`);
      const text = await response.text();
      if (["md", "markdown"].includes(ext)) {
        ui.markdownView.innerHTML = renderMarkdown(text);
        ui.markdownView.classList.remove("hidden");
      } else if (ext === "csv") {
        renderCsvPreview(text);
        ui.csvView.classList.remove("hidden");
      } else {
        ui.codeView.textContent = text;
        ui.codeView.classList.remove("hidden");
      }
    }
    setStatus("已连接", true);
  } catch (error) {
    ui.codeView.textContent = error.message;
    ui.codeView.classList.remove("hidden");
    setStatus("读取失败");
  }
}

async function openMessageFile(path) {
  switchSection("workspace");
  await refreshTree();
  const button = [...ui.tree.querySelectorAll(".tree-file")]
    .find(node => node.dataset.path === path);
  if (!button) throw new Error(`文件不存在或已被删除：${path}`);
  let parent = button.parentElement;
  while (parent && parent !== ui.tree) {
    if (parent.matches?.("details.tree-dir")) parent.open = true;
    parent = parent.parentElement;
  }
  await previewFile(path, button);
  button.scrollIntoView({block: "nearest"});
}

ui.messages.addEventListener("click", event => {
  const link = event.target.closest(".message-file-link");
  if (!link) return;
  event.preventDefault();
  let path;
  try {
    path = decodeURIComponent(link.dataset.workspacePath || "");
  } catch (_) {
    setStatus("文件链接无效");
    return;
  }
  openMessageFile(path).catch(error => setStatus(error.message));
});

ui.refreshTree.addEventListener("click", () => {
  const refresh = activeSection === "workspace" ? refreshTree() : refreshResources(activeSection);
  refresh.catch(error => setStatus(error.message));
});
ui.backDiagram.addEventListener("click", () => {
  selectedFile = null;
  selectedResource = null;
  document.querySelectorAll(".tree-file.active").forEach(node => node.classList.remove("active"));
  document.querySelectorAll(".resource-item.active").forEach(node => node.classList.remove("active"));
  refreshDiagram();
});

ui.saveResource.addEventListener("click", async () => {
  if (!selectedResource) return;
  ui.saveResource.disabled = true;
  try {
    selectedResource = await api(
      `/v1/sessions/${sessionId}/client/${selectedResource.kind}/${encodeURIComponent(selectedResource.name)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: ui.resourceEditor.value }),
      }
    );
    setStatus(`已保存 ${selectedResource.name}`, true);
  } catch (error) {
    setStatus(error.message);
  } finally {
    ui.saveResource.disabled = false;
  }
});

ui.deleteResource.addEventListener("click", async () => {
  if (!selectedResource || selectedResource.builtin) return;
  ui.deleteResource.disabled = true;
  try {
    const kind = selectedResource.kind;
    await api(`/v1/sessions/${sessionId}/client/${kind}/${encodeURIComponent(selectedResource.name)}`, { method: "DELETE" });
    selectedResource = null;
    await refreshResources(kind);
    await refreshDiagram();
    setStatus("资源已删除", true);
  } catch (error) {
    setStatus(error.message);
  } finally {
    ui.deleteResource.disabled = false;
  }
});

function addAttachmentChip(uploaded) {
  pendingFiles.push(uploaded);
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.title = uploaded.filename;
  const name = document.createElement("span");
  name.className = "chip-name";
  name.textContent = uploaded.filename;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "chip-remove";
  remove.textContent = "×";
  remove.title = `取消添加 ${uploaded.filename}`;
  remove.setAttribute("aria-label", `取消添加附件 ${uploaded.filename}`);
  remove.addEventListener("click", () => removePendingAttachment(uploaded));
  chip.append(name, remove);
  ui.attachments.append(chip);
}

function removePendingAttachment(uploaded) {
  pendingFiles = pendingFiles.filter(file => file !== uploaded && file.id !== uploaded.id);
  renderPendingAttachments();
  updateWorkspaceAttachButtons();
  setStatus(`已取消附件：${uploaded.filename}`, true);
}

function renderPendingAttachments() {
  const files = [...pendingFiles];
  ui.attachments.replaceChildren();
  pendingFiles = [];
  files.forEach(addAttachmentChip);
}

function workspaceAttachment(path) {
  return pendingFiles.find(file => file.workspacePath === path);
}

function updateWorkspaceAttachButtons() {
  ui.tree.querySelectorAll(".tree-attach").forEach(button => {
    const attached = Boolean(workspaceAttachment(button.dataset.path));
    button.classList.toggle("attached", attached);
    button.textContent = attached ? "✓" : "+";
    button.title = attached ? "取消聊天附件" : "加入聊天附件";
    button.setAttribute("aria-label", attached ? `取消附件 ${button.dataset.path}` : `加入附件 ${button.dataset.path}`);
    button.disabled = false;
  });
  const selectedAttached = selectedFile && workspaceAttachment(selectedFile);
  ui.attachWorkspaceFile.disabled = false;
  ui.attachWorkspaceFile.textContent = selectedAttached ? "× 取消附件" : "＋ 加入聊天附件";
}

async function toggleWorkspaceAttachment(path, button = null) {
  const attached = workspaceAttachment(path);
  if (attached) {
    removePendingAttachment(attached);
    return;
  }
  await attachWorkspacePath(path, button);
}

async function attachWorkspacePath(path, button = null) {
  if (!path || !sessionId || workspaceAttachment(path)) return;
  if (button) button.disabled = true;
  try {
    const uploaded = await api(`/v1/sessions/${sessionId}/files/from-workspace`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    uploaded.workspacePath = path;
    addAttachmentChip(uploaded);
    updateWorkspaceAttachButtons();
    setStatus(`已加入附件：${uploaded.filename}`, true);
    ui.prompt.focus();
  } catch (error) {
    setStatus(error.message);
  } finally {
    if (button && !workspaceAttachment(path)) button.disabled = false;
  }
}

ui.attachWorkspaceFile.addEventListener("click", async () => {
  ui.attachWorkspaceFile.disabled = true;
  try {
    await toggleWorkspaceAttachment(selectedFile);
  } finally {
    ui.attachWorkspaceFile.disabled = false;
  }
});

async function uploadAttachmentFiles(files) {
  if (!sessionId) return;
  for (const file of files) {
    setStatus(`上传 ${file.name}`);
    try {
      const uploaded = await api(`/v1/sessions/${sessionId}/files?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      addAttachmentChip(uploaded);
    } catch (error) {
      addMessage(error.message, "assistant");
    }
  }
  setStatus("已连接", true);
  await refreshTree();
}

async function uploadWorkspaceFiles(files) {
  for (const file of files) {
    setStatus(`保存 ${file.name}`);
    await api(`/v1/sessions/${sessionId}/workspace/files?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
  }
  await refreshTree();
  setStatus("已保存到 output", true);
}

ui.file.addEventListener("change", async () => {
  await uploadAttachmentFiles([...ui.file.files]);
  ui.file.value = "";
});

function bindDropZone(element, onFiles) {
  let dragDepth = 0;
  element.addEventListener("dragenter", event => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    dragDepth += 1;
    element.classList.add("drag-over");
  });
  element.addEventListener("dragover", event => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
  element.addEventListener("dragleave", () => {
    dragDepth -= 1;
    if (dragDepth <= 0) {
      dragDepth = 0;
      element.classList.remove("drag-over");
    }
  });
  element.addEventListener("drop", async event => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth = 0;
    element.classList.remove("drag-over");
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    try {
      await onFiles(files);
    } catch (error) {
      addMessage(`文件上传失败：${error.message}`, "assistant");
      setStatus("上传失败");
    }
  });
}

bindDropZone(ui.filebar, uploadWorkspaceFiles);
bindDropZone(ui.composer, uploadAttachmentFiles);

function setRunControls(running, stopping = false) {
  ui.send.classList.toggle("hidden", running);
  ui.stop.classList.toggle("hidden", !running);
  ui.stop.disabled = stopping;
  ui.stop.textContent = stopping ? "停止中…" : "停止";
  ui.prompt.disabled = running;
  ui.compactContext.disabled = running;
}

function followRun(runId, runSessionId = sessionId) {
  if (activeRunCleanup) activeRunCleanup();
  else if (activeStream) activeStream.close();
  const stream = new EventSource(`/v1/runs/${runId}/events`);
  activeStream = stream;
  activeRunId = runId;
  activeRunSessionId = runSessionId;
  setRunControls(true);
  setStatus("任务运行中");
  let assistant = null;
  let streamedText = "";
  let generation = null;
  const pendingTools = new Map();
  const pendingSubagentTools = new Map();
  let reasoningAction = null;
  let reasoningBuffer = "";
  let usageChars = 0;
  let verificationChars = 0;
  let subagentTaskAction = null;
  let subagentReasoning = null;
  let subagentReasoningBuffer = "";
  let subagentOutput = null;
  let subagentOutputBuffer = "";
  const startedAt = Date.now();
  const metrics = document.createElement("div");
  metrics.className = "run-metrics";
  ui.messages.append(metrics);
  const updateMetrics = () => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    metrics.textContent = `本轮约 ${Math.ceil(usageChars / 4).toLocaleString()} tokens · ${seconds}s`;
  };
  const timer = setInterval(updateMetrics, 1000);
  // SSE is authoritative; this low-frequency refresh is only a safety net for
  // files written by extensions that do not emit a workspace milestone yet.
  const workspaceFallback = setInterval(
    () => scheduleWorkspaceRefresh(runSessionId, false, 0), 2500
  );
  const cleanup = () => {
    stream.close();
    clearInterval(timer);
    clearInterval(workspaceFallback);
    if (activeStream === stream) activeStream = null;
    if (activeRunCleanup === cleanup) activeRunCleanup = null;
  };
  activeRunCleanup = cleanup;
  updateMetrics();
  const finishReasoning = () => {
    if (!reasoningAction || reasoningAction.classList.contains("completed")) return;
    reasoningAction.classList.add("completed");
    reasoningAction.textContent = reasoningAction.textContent.replace(/^思考中/, "思考完成");
  };
  const ensureReasoningLine = () => {
    if (!reasoningAction || reasoningAction.classList.contains("completed")) {
      reasoningBuffer = "";
      reasoningAction = document.createElement("div");
      reasoningAction.className = "reasoning-line";
      reasoningAction.textContent = "思考中…";
      ui.messages.append(reasoningAction);
    }
    return reasoningAction;
  };

  stream.addEventListener("assistant.delta", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    if (!assistant) assistant = addMessage("", "assistant");
    streamedText += payload.data.text;
    usageChars += payload.data.text.length;
    finishReasoning(); updateMetrics();
    assistant.querySelector(".message-body").textContent = streamedText;
  });
  stream.addEventListener("generation.round_started", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    generation = addMessage(`正在生成第 ${payload.data.round} 轮…`, "progress");
    scheduleWorkspaceRefresh(runSessionId);
  });
  stream.addEventListener("generation.stage", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    finishReasoning();
    if (!generation) generation = addMessage("", "progress");
    generation.textContent = payload.data.message;
    scheduleWorkspaceRefresh(runSessionId, payload.data.stage === "verifying");
  });
  stream.addEventListener("workspace.changed", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    scheduleWorkspaceRefresh(runSessionId, payload.data.refresh_diagram === true);
  });
  stream.addEventListener("verification.delta", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    verificationChars += (payload.data.text || "").length + (payload.data.chars || 0);
    if (!generation) generation = addMessage("", "progress");
    generation.textContent = `正在视觉验证 · 已接收 ${verificationChars.toLocaleString()} 字符…`;
  });
  stream.addEventListener("reasoning.status", () => {
    if (sessionId !== runSessionId) return;
    ensureReasoningLine();
  });
  stream.addEventListener("reasoning.delta", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    reasoningBuffer = (reasoningBuffer + (payload.data.text || "")).slice(-1200);
    const lines = reasoningBuffer.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const tail = lines.at(-1) || "正在分析与规划…";
    const visible = tail.length > 110 ? `…${tail.slice(-110)}` : tail;
    ensureReasoningLine().textContent = `思考中 · ${visible}`;
    ui.messages.scrollTop = ui.messages.scrollHeight;
  });
  stream.addEventListener("usage.delta", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    usageChars += payload.data.chars || 0; updateMetrics();
  });
  stream.addEventListener("progress.updated", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    if (!generation) generation = addMessage("", "progress");
    generation.textContent = payload.data.message;
  });
  stream.addEventListener("tool.started", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const name = payload.data.name;
    finishReasoning();
    const action = addAgentAction(name, "main", payload.data.arguments ?? "", assistant);
    const queue = pendingTools.get(name) || [];
    queue.push(action); pendingTools.set(name, queue);
  });
  stream.addEventListener("resource.activated", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const isSkill = payload.data.kind === "skills";
    const action = addAgentAction(isSkill ? "use_skill" : "set_style", "main", null, assistant);
    action.node.classList.remove("running"); action.node.classList.add("completed");
    action.icon.textContent = "✓";
    action.text.textContent = `服务端已加载 ${isSkill ? "Skill" : "Style"}：${payload.data.name}`;
  });
  stream.addEventListener("tool.completed", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const name = payload.data.name;
    const queue = pendingTools.get(name) || [];
    const action = queue.shift();
    if (!action) return;
    action.node.classList.remove("running"); action.node.classList.add("completed");
    action.icon.textContent = "✓"; action.text.textContent = `已完成：${toolLabel(name)}`;
    action.result = payload.data.result ?? "";
    makeToolActionInspectable(action);
    if (activeToolDetail === action) renderToolDetail(action);
    scheduleWorkspaceRefresh(runSessionId);
  });
  stream.addEventListener("subagent.started", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const task = payload.data.task || "文件任务";
    subagentTaskAction = addAgentAction("subagent_task", "subagent", task, assistant);
    subagentTaskAction.text.textContent = `子 Agent 已接手：${task.length > 90 ? `${task.slice(0, 90)}…` : task}`;
    subagentTaskAction.node.title = task;
  });
  stream.addEventListener("subagent.reasoning.delta", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    subagentReasoningBuffer = (subagentReasoningBuffer + (payload.data.text || "")).slice(-1200);
    if (!subagentReasoning) {
      subagentReasoning = document.createElement("div");
      subagentReasoning.className = "subagent-line";
      ui.messages.append(subagentReasoning);
    }
    const lines = subagentReasoningBuffer.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const latest = lines.at(-1) || "正在分析文件…";
    subagentReasoning.textContent = `子 Agent 思考中 · ${latest}`;
    if (subagentTaskAction) {
      subagentTaskAction.liveReasoning = (
        subagentTaskAction.liveReasoning + (payload.data.text || "")
      ).slice(-12000);
      if (activeToolDetail === subagentTaskAction) renderToolDetail(subagentTaskAction);
    }
    // 兼容仍在旧服务进程中运行的长工具：旧后端把工具内部进度发成 reasoning.delta。
    const checkAction = (pendingSubagentTools.get("image_reasoning") || [])[0];
    if (checkAction && latest) {
      const visibleLatest = latest.length > 140 ? `…${latest.slice(-140)}` : latest;
      checkAction.text.textContent = `子 Agent · ${toolLabel("image_reasoning")}：${visibleLatest}`;
      checkAction.node.title = latest;
    }
    ui.messages.scrollTop = ui.messages.scrollHeight;
  });
  stream.addEventListener("subagent.delta", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    subagentOutputBuffer = (subagentOutputBuffer + (payload.data.text || "")).slice(-1600);
    if (!subagentOutput) {
      subagentOutput = document.createElement("div");
      subagentOutput.className = "subagent-line";
      ui.messages.append(subagentOutput);
    }
    const lines = subagentOutputBuffer.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const tail = lines.at(-1) || "正在整理结果…";
    subagentOutput.textContent = `子 Agent 输出 · ${tail.length > 120 ? `…${tail.slice(-120)}` : tail}`;
    if (subagentTaskAction) {
      subagentTaskAction.liveOutput = (
        subagentTaskAction.liveOutput + (payload.data.text || "")
      ).slice(-12000);
      if (activeToolDetail === subagentTaskAction) renderToolDetail(subagentTaskAction);
    }
    ui.messages.scrollTop = ui.messages.scrollHeight;
  });
  stream.addEventListener("subagent.tool.started", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const name = payload.data.name;
    const action = addAgentAction(name, "subagent", payload.data.arguments ?? "", assistant);
    const queue = pendingSubagentTools.get(name) || [];
    queue.push(action); pendingSubagentTools.set(name, queue);
  });
  stream.addEventListener("subagent.tool.progress", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const name = payload.data.name;
    const queue = pendingSubagentTools.get(name) || [];
    const action = queue[0];
    if (!action) return;
    const message = payload.data.message || "工具执行中…";
    if (payload.data.reasoning_delta) {
      action.liveReasoning = (action.liveReasoning + payload.data.reasoning_delta).slice(-12000);
    }
    if (payload.data.output_delta) {
      action.liveOutput = (action.liveOutput + payload.data.output_delta).slice(-12000);
    }
    action.text.textContent = `子 Agent · ${toolLabel(name)}：${message}`;
    action.node.title = message;
    if (activeToolDetail === action) renderToolDetail(action);
  });
  stream.addEventListener("subagent.tool.completed", event => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    const name = payload.data.name;
    const queue = pendingSubagentTools.get(name) || [];
    const action = queue.shift();
    if (!action) return;
    action.node.classList.remove("running"); action.node.classList.add("completed");
    action.icon.textContent = "✓"; action.text.textContent = `子 Agent 已完成：${toolLabel(name)}`;
    action.result = payload.data.result ?? "";
    makeToolActionInspectable(action);
    if (activeToolDetail === action) renderToolDetail(action);
    scheduleWorkspaceRefresh(runSessionId);
  });
  const finishSubagent = (status, data = {}) => {
    if (subagentReasoning) subagentReasoning.classList.add("completed");
    if (subagentOutput) subagentOutput.classList.add("completed");
    if (!subagentTaskAction) return;
    subagentTaskAction.node.classList.remove("running");
    subagentTaskAction.node.classList.add("completed");
    subagentTaskAction.icon.textContent = status === "completed" ? "✓" : "!";
    subagentTaskAction.text.textContent = status === "completed" ? "子 Agent 工作完成" : `子 Agent ${status}`;
    subagentTaskAction.result = data.result ?? data.error ?? subagentTaskAction.liveOutput ?? "";
    makeToolActionInspectable(subagentTaskAction);
    if (activeToolDetail === subagentTaskAction) renderToolDetail(subagentTaskAction);
  };
  const finishSubagentEvent = (event, status) => {
    if (sessionId !== runSessionId) return;
    const payload = JSON.parse(event.data);
    finishSubagent(status, payload.data || {});
  };
  stream.addEventListener("subagent.completed", event => finishSubagentEvent(event, "completed"));
  stream.addEventListener("subagent.failed", event => finishSubagentEvent(event, "执行失败"));
  stream.addEventListener("subagent.cancelled", event => finishSubagentEvent(event, "已停止"));
  stream.addEventListener("run.completed", async event => {
    if (sessionId !== runSessionId) { stream.close(); return; }
    const payload = JSON.parse(event.data);
    const reply = payload.data.reply || "已完成。";
    if (!assistant || !streamedText.trim()) {
      assistant = addMessage(reply, "assistant");
    }
    assistant.querySelector(".message-body").innerHTML = renderMarkdown(reply);
    cleanup(); finishReasoning(); updateMetrics();
    activeStream = null; activeRunId = null; activeRunSessionId = null;
    setRunControls(false);
    setStatus("已连接", true);
    await refreshDiagram();
    await refreshTree();
    await refreshContext(runSessionId);
    await refreshSessionTabs();
  });
  stream.addEventListener("run.failed", async event => {
    if (sessionId !== runSessionId) { stream.close(); return; }
    const payload = JSON.parse(event.data);
    addMessage(`运行失败：${payload.data.error}`, "assistant");
    cleanup(); finishReasoning(); updateMetrics();
    activeStream = null; activeRunId = null; activeRunSessionId = null;
    setRunControls(false);
    setStatus("运行失败");
    await refreshTree(runSessionId);
    await refreshDiagram(runSessionId);
    await refreshContext(runSessionId);
  });
  stream.addEventListener("run.cancelling", () => {
    if (sessionId !== runSessionId) return;
    setRunControls(true, true);
    setStatus("正在停止任务");
  });
  stream.addEventListener("run.cancelled", async event => {
    if (sessionId !== runSessionId) { stream.close(); return; }
    const payload = JSON.parse(event.data);
    const reply = payload.data.reply || "生成已停止。";
    addMessage(reply, "assistant");
    cleanup(); finishReasoning(); updateMetrics();
    activeStream = null; activeRunId = null; activeRunSessionId = null;
    setRunControls(false);
    setStatus("已停止", true);
    await refreshDiagram(runSessionId);
    await refreshTree();
    await refreshContext(runSessionId);
  });
  stream.onerror = () => {
    if (stream.readyState === EventSource.CLOSED) return;
    setStatus("事件流重连中");
  };
}

function clearDiagramPreview() {
  ui.source.textContent = "尚无源码";
  ui.previewKicker.textContent = "CURRENT DIAGRAM";
  ui.previewTitle.textContent = "当前图";
  ui.backDiagram.classList.add("hidden");
  ui.saveResource.classList.add("hidden");
  ui.deleteResource.classList.add("hidden");
  ui.attachWorkspaceFile.classList.add("hidden");
  ui.openFile.classList.add("hidden");
  ui.sourceLink.classList.add("hidden");
  resetPreviewViews();
  ui.canvas.replaceChildren();
  ui.canvas.textContent = "生成完成后，流程图会出现在这里。";
  ui.canvas.classList.add("empty");
  ui.canvas.classList.remove("hidden");
  ui.sourcePanel.classList.remove("hidden");
}

async function refreshDiagram(targetSessionId = sessionId) {
  clearDiagramPreview();
  const diagram = await api(`/v1/sessions/${targetSessionId}/diagram`);
  if (targetSessionId !== sessionId) return;
  ui.source.textContent = diagram.source || "尚无源码";
  ui.previewKicker.textContent = "CURRENT DIAGRAM";
  ui.previewTitle.textContent = "当前图";
  ui.backDiagram.classList.add("hidden");
  ui.saveResource.classList.add("hidden");
  ui.deleteResource.classList.add("hidden");
  ui.attachWorkspaceFile.classList.add("hidden");
  ui.openFile.classList.add("hidden");
  resetPreviewViews();
  ui.canvas.classList.remove("hidden");
  ui.sourcePanel.classList.remove("hidden");
  if (diagram.source_artifact_id) {
    ui.sourceLink.href = `/v1/sessions/${targetSessionId}/artifacts/${diagram.source_artifact_id}/content`;
    ui.sourceLink.classList.remove("hidden");
  }
  const visualId = diagram.svg_artifact_id || diagram.image_artifact_id;
  if (visualId) {
    ui.canvas.classList.remove("empty");
    ui.canvas.replaceChildren();
    const image = new Image();
    image.alt = "当前流程图";
    image.src = `/v1/sessions/${targetSessionId}/artifacts/${visualId}/content?t=${Date.now()}`;
    ui.canvas.append(image);
  }
}

ui.composer.addEventListener("submit", async event => {
  event.preventDefault();
  const input = ui.prompt.value.trim();
  if (!input || !sessionId) return;
  if (pendingResourceMutations.size) {
    setStatus("正在确认 Skill / Style 挂载…");
    await Promise.allSettled([...pendingResourceMutations]);
  }
  const attachmentsForRun = [...pendingFiles];
  addMessage(input, "user", attachmentsForRun);
  ui.prompt.value = "";
  setRunControls(true);
  setStatus("任务已提交");
  try {
    const run = await api(`/v1/sessions/${sessionId}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input, attachments: attachmentsForRun.map(file => file.id) }),
    });
    pendingFiles = [];
    ui.attachments.replaceChildren();
    updateWorkspaceAttachButtons();
    followRun(run.id, sessionId);
  } catch (error) {
    addMessage(error.message, "assistant");
    setRunControls(false);
    setStatus("提交失败");
  }
});

ui.prompt.addEventListener("keydown", event => {
  if (
    event.key !== "Enter" ||
    event.shiftKey ||
    event.isComposing ||
    event.keyCode === 229
  ) return;
  event.preventDefault();
  if (!activeRunId && ui.prompt.value.trim()) {
    ui.composer.requestSubmit();
  }
});

ui.stop.addEventListener("click", async () => {
  if (!activeRunId || activeRunSessionId !== sessionId) return;
  setRunControls(true, true);
  setStatus("正在停止任务");
  try {
    await api(`/v1/runs/${activeRunId}/cancel`, {method: "POST"});
  } catch (error) {
    setRunControls(true, false);
    setStatus(error.message);
  }
});

ui.compactContext.addEventListener("click", async () => {
  if (!sessionId || activeRunId) return;
  const targetSessionId = sessionId;
  ui.compactContext.disabled = true;
  ui.compactContext.textContent = "压缩中…";
  setStatus("正在压缩上下文");
  try {
    const stats = await api(`/v1/sessions/${targetSessionId}/context/compact`, {method: "POST"});
    if (targetSessionId !== sessionId) return;
    renderContextStats(stats);
    if (stats.compressed) {
      addMessage(`上下文已压缩：≈${compactTokenLabel(stats.before_tokens)} → ${compactTokenLabel(stats.used_tokens)} tokens`, "progress");
      setStatus("上下文已压缩", true);
    } else {
      addMessage(stats.reason || "当前上下文无需压缩。", "progress");
      setStatus("无需压缩", true);
    }
  } catch (error) {
    setStatus(error.message);
  } finally {
    ui.compactContext.textContent = "压缩";
    ui.compactContext.disabled = Boolean(activeRunId);
  }
});

boot().catch(error => {
  setStatus("连接失败");
  addMessage(`无法创建会话：${error.message}`, "assistant");
});
