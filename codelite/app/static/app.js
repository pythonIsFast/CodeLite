/*
 * Copyright 2026 Code Lite contributors
 * Licensed under the Apache License, Version 2.0.
 *
 * Vanilla JS, no framework, no build step. The SSE stream for the active
 * conversation stays open for as long as it is selected, so a run's events
 * can never arrive before the UI is listening.
 */

"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  conversations: $("conversations"),
  messages: $("messages"),
  emptyState: $("empty-state"),
  title: $("chat-title"),
  workspace: $("chat-workspace"),
  modelSelect: $("model-select"),
  effortSelect: $("effort-select"),
  fastToggle: $("fast-toggle"),
  modeSelect: $("mode-select"),
  compactContext: $("compact-context"),
  composer: $("composer"),
  prompt: $("prompt"),
  attachments: $("composer-attachments"),
  send: $("send"),
  statusBar: $("status-bar"),
  statusText: $("status-text"),
  stopRun: $("stop-run"),
  usage: $("usage"),
  usageRingFill: $("usage-ring-fill"),
  usageLabel: $("usage-label"),
  planUsage: $("plan-usage"),
  planRingFill: $("plan-ring-fill"),
  planLabel: $("plan-label"),
  newChat: $("new-chat"),
  toolCount: $("tool-count"),
  toast: $("toast"),
  permOverlay: $("permission-overlay"),
  permKind: $("perm-kind"),
  permDetail: $("perm-detail"),
  permScope: $("perm-scope"),
  permDiff: $("perm-diff"),
  permJudge: $("perm-judge"),
  permJudgeReason: $("perm-judge-reason"),
  permFeedback: $("perm-feedback"),
  permOnce: $("perm-once"),
  permSession: $("perm-session"),
  permDeny: $("perm-deny"),
  questionOverlay: $("question-overlay"),
  questionTitle: $("question-title"),
  questionText: $("question-text"),
  questionOptions: $("question-options"),
  questionAnswer: $("question-answer"),
  questionCancel: $("question-cancel"),
  questionSubmit: $("question-submit"),
  newOverlay: $("new-overlay"),
  newWorkspace: $("new-workspace"),
  browseWorkspace: $("browse-workspace"),
  newMode: $("new-mode"),
  newEffort: $("new-effort"),
  renameOverlay: $("rename-overlay"),
  renameInput: $("rename-input"),
  renameSave: $("rename-save"),
  renameCancel: $("rename-cancel"),
  confirmOverlay: $("confirm-overlay"),
  confirmText: $("confirm-text"),
  confirmOk: $("confirm-ok"),
  confirmCancel: $("confirm-cancel"),
  newCreate: $("new-create"),
  newCancel: $("new-cancel"),
  settingsButton: $("settings-button"),
  settingsOverlay: $("settings-overlay"),
  settingsClose: $("settings-close"),
  settingsTabs: [...document.querySelectorAll("[data-settings-tab]")],
  settingsPanels: [...document.querySelectorAll("[data-settings-panel]")],
  settingsLogin: $("settings-login"),
  accountName: $("account-name"),
  accountDetail: $("account-detail"),
  settingsAuthStatus: $("settings-auth-status"),
  globalMemory: $("global-memory"),
  globalMemoryPath: $("global-memory-path"),
  globalMemorySave: $("global-memory-save"),
  behaviourGroups: $("behaviour-groups"),
  behaviourSave: $("behaviour-save"),
  behaviourReset: $("behaviour-reset"),
  behaviourStatus: $("behaviour-status"),
  importSummary: $("import-summary"),
  importStatus: $("import-status"),
  codexImport: $("codex-import"),
  projectSettingsTab: $("project-settings-tab"),
  projectSettings: $("project-settings"),
  projectSettingsPath: $("project-settings-path"),
  projectMemory: $("project-memory"),
  projectMemorySave: $("project-memory-save"),
  projectSkills: $("project-skills"),
  projectMcpConfig: $("project-mcp-config"),
  projectMcpSave: $("project-mcp-save"),
  projectLspConfig: $("project-lsp-config"),
  projectLspSave: $("project-lsp-save"),
  authOverlay: $("auth-overlay"),
  authLogin: $("auth-login"),
  authStatus: $("auth-status"),
};

const state = {
  meta: null,
  models: [],
  efforts: [],
  capabilities: {},
  behaviour: null,
  pendingRename: null,
  pendingDelete: null,
  conversations: [],
  active: null,
  stream: null,
  busy: false,
  liveText: null,
  liveReasoning: null,
  liveGroup: null,
  liveTodos: null,
  lastTurnOutputTokens: null,
  toolCards: new Map(),
  permissionQueue: [],
  questionQueue: [],
  currentPermission: null,
  currentQuestion: null,
  auth: null,
  appLoaded: false,
  authPoll: null,
  attachments: [],
  uploadsInProgress: 0,
};

/* -- API ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

const get = (path) => api(path);
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
const patch = (path, body) => api(path, { method: "PATCH", body: JSON.stringify(body) });
const del = (path) => api(path, { method: "DELETE" });

/* -- ChatGPT account ------------------------------------------------------ */

function renderAuth(auth) {
  state.auth = auth;
  const label = auth.account_label || "ChatGPT account";
  el.accountName.textContent = auth.authenticated ? label : "Not signed in";
  el.accountDetail.textContent = auth.authenticated
    ? (auth.email && auth.email !== label ? auth.email : "Connected with ChatGPT")
    : "Connect your ChatGPT subscription to use Code Lite.";
  el.authOverlay.hidden = Boolean(auth.authenticated);

  const waiting = auth.login_status === "waiting";
  el.authLogin.disabled = waiting;
  el.settingsLogin.disabled = waiting;
  el.authLogin.querySelector("span:last-child").textContent = waiting
    ? "Waiting for ChatGPT…"
    : "Sign in with ChatGPT";
  el.settingsLogin.textContent = waiting ? "Waiting for ChatGPT…" : "Sign in again";

  const message = auth.error || (waiting ? "Finish signing in in your browser." : "");
  el.authStatus.textContent = message;
  el.authStatus.classList.toggle("error", Boolean(auth.error));
  el.settingsAuthStatus.textContent = message;
  el.settingsAuthStatus.classList.toggle("error", Boolean(auth.error));
}

async function refreshAuth() {
  const auth = await get("/api/auth");
  renderAuth(auth);
  return auth;
}

async function loadProjectSettings() {
  el.projectSettingsTab.hidden = !state.active;
  if (!state.active) return;
  const settings = await get(`/api/conversations/${state.active.id}/project-settings`);
  el.projectSettingsPath.textContent = settings.project_dir;
  el.projectMemory.value = settings.memory || "";
  el.projectMcpConfig.value = settings.mcp || "";
  el.projectLspConfig.value = settings.lsp || "";
  const skills = settings.skills || [];
  el.projectSkills.textContent = skills.length
    ? `Skills: ${skills.map((skill) => skill.name).join(", ")}`
    : "Skills: none. Add Markdown skills under .codelite/skills/.";
}

async function loadGlobalSettings() {
  const settings = await get("/api/settings");
  el.globalMemory.value = settings.memory || "";
  el.globalMemoryPath.textContent = settings.memory_path || "";
}

async function saveGlobalMemory() {
  const data = await api("/api/settings/memory", {
    method: "PUT",
    body: JSON.stringify({ content: el.globalMemory.value }),
  });
  el.globalMemory.value = data.content ?? el.globalMemory.value;
  toast("Global memory saved.");
}

function selectSettingsTab(name) {
  if (name === "project" && !state.active) name = "account";
  for (const tab of el.settingsTabs) {
    const active = tab.dataset.settingsTab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const panel of el.settingsPanels) {
    panel.hidden = panel.dataset.settingsPanel !== name;
  }
}

function plural(count, singular, suffix = "s") {
  return `${count} ${singular}${count === 1 ? "" : suffix}`;
}

/* -- Behaviour settings --------------------------------------------------- */

function behaviourRow(setting, value, models) {
  const row = document.createElement("div");
  row.className = "behaviour-row";

  const label = document.createElement("label");
  label.className = "behaviour-label";
  const name = document.createElement("span");
  name.textContent = setting.label;
  const help = document.createElement("span");
  help.className = "behaviour-help";
  help.textContent = setting.help;
  label.append(name, help);

  const control = document.createElement("div");
  control.className = "behaviour-control";

  if (setting.kind === "models") {
    const list = document.createElement("div");
    list.className = "behaviour-models";
    const selected = new Set(value || []);
    // Fall back to the stored value when the catalog is unreachable, so an
    // offline form still shows what is actually configured.
    const choices = models.length ? models : [...selected];
    for (const model of choices) {
      const pill = document.createElement("label");
      pill.className = "behaviour-model" + (selected.has(model) ? " on" : "");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = selected.has(model);
      box.dataset.model = model;
      box.addEventListener("change", () => pill.classList.toggle("on", box.checked));
      const text = document.createElement("span");
      text.textContent = model;
      pill.append(box, text);
      list.appendChild(pill);
    }
    list.dataset.settingKey = setting.key;
    control.appendChild(list);
  } else if (setting.kind === "model") {
    const picker = document.createElement("div");
    picker.className = "custom-select field-picker";
    picker.setAttribute("data-select-picker", "");
    const select = document.createElement("select");
    select.className = "custom-select-source";
    select.tabIndex = -1;
    select.dataset.settingKey = setting.key;
    const choices = models.includes(value) || !value ? models : [value, ...models];
    for (const model of choices.length ? choices : [value]) {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      select.appendChild(option);
    }
    select.value = value;
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "custom-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const menu = document.createElement("div");
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    picker.append(select, trigger, menu);
    control.appendChild(picker);
    // The custom picker has to be initialised after the option list exists.
    requestAnimationFrame(() => renderCustomSelect(select));
  } else {
    const input = document.createElement("input");
    input.type = "number";
    input.dataset.settingKey = setting.key;
    input.dataset.kind = setting.kind;
    if (setting.minimum !== null) input.min = setting.minimum;
    if (setting.maximum !== null) input.max = setting.maximum;
    input.step = setting.step ?? (setting.kind === "float" ? 0.01 : 1);
    input.value = value;
    // The server clamps too, but flagging it here means the user sees which
    // field is out of range rather than a corrected number appearing later.
    input.addEventListener("input", () => {
      const number = Number(input.value);
      const low = setting.minimum !== null && number < setting.minimum;
      const high = setting.maximum !== null && number > setting.maximum;
      input.classList.toggle("out-of-range", input.value !== "" && (low || high));
    });
    control.appendChild(input);
    if (setting.unit) {
      const unit = document.createElement("span");
      unit.className = "behaviour-unit";
      unit.textContent = setting.unit;
      control.appendChild(unit);
    }
  }

  row.append(label, control);
  return row;
}

function renderBehaviour(schema, values) {
  el.behaviourGroups.replaceChildren();
  const models = schema.models || [];
  for (const group of schema.groups) {
    const settings = schema.settings.filter((setting) => setting.group === group.key);
    if (!settings.length) continue;
    const section = document.createElement("section");
    section.className = "behaviour-group" + (group.key === "danger" ? " danger" : "");
    const heading = document.createElement("h3");
    heading.textContent = group.label;
    const help = document.createElement("p");
    help.className = "behaviour-group-help";
    help.textContent = group.help;
    section.append(heading, help);
    for (const setting of settings) {
      section.appendChild(behaviourRow(setting, values[setting.key], models));
    }
    el.behaviourGroups.appendChild(section);
  }
}

async function loadBehaviour() {
  const data = await get("/api/settings/behaviour");
  state.behaviour = data;
  renderBehaviour(data.schema, data.values);
  el.behaviourStatus.textContent = "";
}

function collectBehaviour() {
  const values = {};
  for (const input of el.behaviourGroups.querySelectorAll("input[type=number]")) {
    const raw = Number(input.value);
    if (input.value === "" || Number.isNaN(raw)) continue;
    values[input.dataset.settingKey] = input.dataset.kind === "int" ? Math.round(raw) : raw;
  }
  for (const select of el.behaviourGroups.querySelectorAll("select[data-setting-key]")) {
    values[select.dataset.settingKey] = select.value;
  }
  for (const list of el.behaviourGroups.querySelectorAll("[data-setting-key].behaviour-models")) {
    values[list.dataset.settingKey] = [...list.querySelectorAll("input:checked")]
      .map((box) => box.dataset.model);
  }
  return values;
}

async function saveBehaviour(values) {
  const data = await api("/api/settings/behaviour", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });
  // Re-render from what the server stored: it clamps, so the form must show
  // the value that is actually in effect, not the one that was typed.
  state.behaviour = { ...state.behaviour, values: data.values };
  renderBehaviour(state.behaviour.schema, data.values);
  const clamped = Object.keys(data.values).filter(
    (key) => String(data.values[key]) !== String(values[key]) && key in values,
  );
  el.behaviourStatus.textContent = clamped.length
    ? `Saved. ${clamped.length} value(s) were adjusted to their allowed range.`
    : "Saved.";
  toast("Settings saved.");
  await loadModels();
}

async function loadCodexImport() {
  const data = await api("/api/import/codex");
  if (!data.found) {
    el.importSummary.textContent =
      `No Codex installation found at ${data.codex_home}.`;
    el.codexImport.disabled = true;
    return;
  }
  const parts = [`${plural(data.available, "session")} in ${data.codex_home}`];
  if (data.already_imported) parts.push(`${data.already_imported} already imported`);
  el.importSummary.textContent = parts.join(" · ");
  el.codexImport.disabled = data.new === 0;
  el.codexImport.textContent = data.new
    ? `Import ${plural(data.new, "session")}`
    : "Nothing new to import";
}

async function runCodexImport() {
  el.codexImport.disabled = true;
  // No progress channel exists for this, so say plainly that a long wait is
  // the import working rather than the window having frozen.
  el.importStatus.textContent = "Importing… this can take a while on a large history.";
  try {
    const report = await api("/api/import/codex", { method: "POST" });
    const parts = [`Imported ${plural(report.imported, "conversation")}`];
    if (report.skipped) parts.push(`${report.skipped} already present`);
    if (report.empty) parts.push(`${report.empty} empty`);
    if (report.failed) parts.push(`${report.failed} failed`);
    el.importStatus.textContent = `${parts.join(", ")}.`;
    // The first error is the useful one; the rest are usually the same cause.
    if (report.errors && report.errors.length) {
      el.importStatus.textContent += ` First error: ${report.errors[0]}`;
    }
    if (report.imported) {
      await loadConversations();
      toast(`Imported ${plural(report.imported, "conversation")} from Codex.`);
    }
  } finally {
    await loadCodexImport().catch(() => {});
  }
}

async function saveProjectSetting(kind, textarea) {
  if (!state.active) return;
  const data = await api(`/api/conversations/${state.active.id}/project-settings/${kind}`, {
    method: "PUT",
    body: JSON.stringify({ content: textarea.value }),
  });
  textarea.value = data.content ?? textarea.value;
  toast(`${kind.toUpperCase()} settings saved.`);
}

function stopAuthPolling() {
  if (state.authPoll) clearInterval(state.authPoll);
  state.authPoll = null;
}

async function pollAuth() {
  try {
    const auth = await refreshAuth();
    if (auth.login_status === "success" && auth.authenticated) {
      stopAuthPolling();
      el.settingsOverlay.hidden = true;
      toast("Signed in with ChatGPT.");
      await loadAuthenticatedApp();
    } else if (auth.login_status === "error") {
      stopAuthPolling();
    }
  } catch (error) {
    stopAuthPolling();
    toast(error.message, true);
  }
}

async function startAuthLogin() {
  const hasNativeOpener = Boolean(window.pywebview?.api?.open_external);
  const popup = hasNativeOpener ? null : window.open("about:blank", "codelite-chatgpt-login");
  try {
    const data = await post("/api/auth/login");
    if (hasNativeOpener) {
      await window.pywebview.api.open_external(data.authorization_url);
    } else if (popup) {
      popup.location.href = data.authorization_url;
    } else {
      window.open(data.authorization_url, "_blank", "noopener");
    }
    await refreshAuth();
    stopAuthPolling();
    state.authPoll = setInterval(pollAuth, 1000);
  } catch (error) {
    if (popup) popup.close();
    toast(error.message, true);
    await refreshAuth().catch(() => {});
  }
}

/* -- Small helpers -------------------------------------------------------- */

function toast(message, isError = false) {
  el.toast.textContent = message;
  el.toast.classList.toggle("error", isError);
  el.toast.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.toast.hidden = true; }, isError ? 7000 : 3500);
}

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/**
 * A deliberately tiny Markdown subset: fenced code, inline code, bold, and
 * bullet lists. Enough for an agent's output without shipping a parser.
 */
function inlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

/**
 * Render one blank-line-separated block.
 *
 * Classified per line rather than per block: a block is often a heading with
 * its text underneath, or a sentence followed by bullets. Deciding once for the
 * whole block meant anything mixed fell through to a paragraph -- which is why
 * a `## heading` used to render as literal hashes.
 */
function renderTextBlock(block) {
  const out = [];
  let list = [];
  let paragraph = [];
  const flushList = () => {
    if (list.length) out.push(`<ul>${list.join("")}</ul>`);
    list = [];
  };
  const flushParagraph = () => {
    if (paragraph.length) out.push(`<p>${paragraph.join("<br />")}</p>`);
    paragraph = [];
  };

  for (const line of block.split("\n")) {
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushList();
      flushParagraph();
      // Offset by three: the surrounding page owns h1-h3, so a message's own
      // "#" must not compete with the app's headings.
      const level = Math.min(6, heading[1].length + 3);
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      list.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
      continue;
    }
    flushList();
    paragraph.push(inlineMarkdown(line));
  }
  flushList();
  flushParagraph();
  return out.join("");
}

function formatMarkdown(text) {
  const parts = String(text).split(/```/);
  return parts
    .map((part, index) => {
      if (index % 2 === 1) {
        const newline = part.indexOf("\n");
        const body = newline === -1 ? part : part.slice(newline + 1);
        return `<pre><code>${escapeHtml(body.replace(/\n$/, ""))}</code></pre>`;
      }
      return part
        .split(/\n{2,}/)
        .filter((block) => block.trim())
        .map(renderTextBlock)
        .join("");
    })
    .join("");
}

function atBottom() {
  const box = el.messages;
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
}

function scrollDown(force = false) {
  if (force || atBottom()) el.messages.scrollTop = el.messages.scrollHeight;
}

function append(node) {
  const stick = atBottom();
  el.messages.appendChild(node);
  scrollDown(stick);
  return node;
}

/* -- Rendering ------------------------------------------------------------ */

function userBubble(text) {
  const div = document.createElement("div");
  div.className = "msg user";
  div.textContent = text;
  return div;
}

function userTextForDisplay(text) {
  return String(text)
    .replace(/^Attached workspace files:\n(?:- .+\n?)+\n*/, "")
    .trim() || "Attached file";
}

function userMessage(text, attachments = []) {
  const message = document.createElement("div");
  message.className = "user-message";
  for (const attachment of attachments) {
    if (!String(attachment.type || "").startsWith("image/")) continue;
    const image = document.createElement("img");
    image.className = "user-image-attachment";
    image.src = fileUrl(attachment.path);
    image.alt = attachment.name || attachment.path;
    image.loading = "lazy";
    message.appendChild(image);
  }
  message.appendChild(userBubble(userTextForDisplay(text)));
  return message;
}

function assistantBubble(text = "") {
  const div = document.createElement("div");
  div.className = "msg assistant";
  div.dataset.raw = text;
  div.innerHTML = formatMarkdown(text);
  return div;
}

function errorBubble(message) {
  const div = document.createElement("div");
  div.className = "msg error";
  div.textContent = message;
  return div;
}

function reasoningBlock(text = "") {
  const div = document.createElement("div");
  div.className = "reasoning";
  div.textContent = text;
  return div;
}

function modelDecisionCard(data) {
  const card = document.createElement("section");
  card.className = "model-decision";
  const title = document.createElement("strong");
  // The effort is part of the decision, not a detail: it changes how long
  // every turn of the run takes, so it belongs in the headline.
  title.textContent = data.effort
    ? `Auto selected ${modelDisplayName(data)} at ${data.effort} effort`
    : `Auto selected ${modelDisplayName(data)}`;
  const reason = document.createElement("p");
  reason.textContent = data.reason || "Choosing the lowest capable model.";
  card.append(title, reason);

  const profiles = data.profiles || {};
  const list = document.createElement("div");
  list.className = "model-decision-profiles";
  for (const model of ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]) {
    const profile = profiles[model];
    if (!profile) continue;
    const row = document.createElement("p");
    row.className = model === data.model ? "selected" : "";
    row.textContent = `${profile.name}: ${profile.fit} ${profile.limit}`;
    list.appendChild(row);
  }
  card.appendChild(list);
  return card;
}

function modelDisplayName(data) {
  return data.model_name || data.model;
}

/* -- Message footer -------------------------------------------------------- */

const ICONS = {
  copy:
    `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">` +
    `<rect x="5.5" y="5.5" width="8" height="8" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.3"/>` +
    `<path d="M10.5 3.5H4A1.5 1.5 0 0 0 2.5 5v6.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  retry:
    `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">` +
    `<path d="M13.5 8a5.5 5.5 0 1 1-1.9-4.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>` +
    `<path d="M13.5 2v3h-3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
};

/** "vor 3 Min." style relative time, recomputed on render rather than stored. */
function relativeTime(iso) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  const steps = [
    [60, "min", 60],
    [3600, "h", 3600],
    [86400, "d", 86400],
  ];
  for (const [limit, unit, divisor] of steps) {
    if (seconds < limit * 60 || unit === "d") {
      const value = Math.floor(seconds / divisor);
      if (value >= 1) return `${value} ${unit} ago`;
    }
  }
  return new Date(then).toLocaleDateString();
}

function footerButton(icon, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "msg-action";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = icon;
  button.addEventListener("click", onClick);
  return button;
}

/**
 * Add the action row under an assistant message: copy, retry, when it was
 * written and how many tokens the visible model output used.
 */
function attachFooter(bubble, { at, outputTokens } = {}) {
  const footer = document.createElement("div");
  footer.className = "msg-foot";

  footer.appendChild(
    footerButton(ICONS.copy, "Copy message", async (event) => {
      const raw = bubble.dataset.raw || bubble.textContent;
      try {
        await navigator.clipboard.writeText(raw);
        toast("Copied.");
      } catch {
        toast("Could not copy to the clipboard.", true);
      }
      event.currentTarget.blur();
    })
  );

  footer.appendChild(
    footerButton(ICONS.retry, "Send this prompt again", () => retryLastPrompt())
  );

  if (at) {
    const time = document.createElement("span");
    time.className = "msg-time";
    time.textContent = relativeTime(at);
    time.title = new Date(at).toLocaleString();
    time.dataset.at = at;
    footer.appendChild(time);
  }

  if (Number.isFinite(outputTokens) && outputTokens > 0) {
    const cost = document.createElement("span");
    cost.className = "msg-tokens";
    cost.textContent = `${formatTokens(outputTokens)} output tokens`;
    cost.title = `${outputTokens.toLocaleString()} tokens in this model output`;
    footer.appendChild(cost);
  }

  bubble.appendChild(footer);
  return footer;
}

/**
 * Close out the streaming bubble and give it its footer.
 *
 * Has to happen after the last delta: `text_delta` rebuilds the bubble's
 * innerHTML each time, which would throw away an already-appended footer.
 */
function finalizeLiveText() {
  const bubble = state.liveText;
  state.liveText = null;
  if (!bubble || !bubble.isConnected) return;
  if (bubble.querySelector(".msg-foot")) return;
  attachFooter(bubble, {
    at: new Date().toISOString(),
    outputTokens: state.lastTurnOutputTokens,
  });
}

/** Re-send the most recent user message. */
function retryLastPrompt() {
  const bubbles = el.messages.querySelectorAll(".msg.user");
  const last = bubbles[bubbles.length - 1];
  if (!last) {
    toast("Nothing to retry yet.");
    return;
  }
  el.prompt.value = last.textContent;
  el.prompt.focus();
  el.prompt.dispatchEvent(new Event("input"));
  toast("Prompt restored — press Enter to send it again.");
}

const TODO_GLYPH = { completed: "✓", in_progress: "→", pending: "" };

/** The agent's plan. Re-rendered in place as it advances, not appended anew. */
function todoCard(todos) {
  const card = document.createElement("div");
  card.className = "todos";
  renderTodos(card, todos);
  return card;
}

function renderTodos(card, todos) {
  const done = todos.filter((t) => t.status === "completed").length;
  card.replaceChildren();

  const head = document.createElement("div");
  head.className = "todos-head";
  head.textContent = `Plan · ${done}/${todos.length}`;
  card.appendChild(head);

  const list = document.createElement("ul");
  list.className = "todos-list";
  for (const todo of todos) {
    const item = document.createElement("li");
    item.className = `todo ${todo.status}`;
    const mark = document.createElement("span");
    mark.className = "todo-mark";
    mark.textContent = TODO_GLYPH[todo.status] ?? "";
    const text = document.createElement("span");
    text.textContent = todo.content;
    item.append(mark, text);
    list.appendChild(item);
  }
  card.appendChild(list);
}

function summarizeArgs(rawArguments) {
  try {
    const parsed = JSON.parse(rawArguments || "{}");
    const interesting = parsed.command || parsed.path || parsed.pattern || parsed.glob;
    if (interesting) return String(interesting);
    const keys = Object.keys(parsed);
    return keys.length ? `${keys.length} argument(s)` : "";
  } catch {
    return "";
  }
}

/**
 * One glyph per category. A folder icon above a web search claims a file was
 * touched, so every category carries its own shape rather than sharing one.
 */
const CATEGORY_ICON_PATHS = {
  read: ["M4 2h5l3 3v9H4z", "M9 2v3h3"],
  list: ["M2 3.5A1.5 1.5 0 0 1 3.5 2h4l1.5 1.5H12.5A1.5 1.5 0 0 1 14 5v7a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12z"],
  search: ["M7 11.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9z", "M10.5 10.5 14 14"],
  write: ["M11.5 2.5 13.5 4.5 5.5 12.5 2.5 13.5 3.5 10.5z"],
  shell: ["M2 3.5h12v9H2z", "M4.5 7 6 8.5 4.5 10", "M8 10.5h3.5"],
  extension: ["M2.5 2.5h5v5h-5z", "M8.5 2.5h5v5h-5z", "M8.5 8.5h5v5h-5z"],
  websearch: ["M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12z", "M2.5 6.5h11", "M2.5 9.5h11", "M8 2c-2 2.5-2 9.5 0 12", "M8 2c2 2.5 2 9.5 0 12"],
  webfetch: ["M8 2.5v7", "M5 7l3 3 3-3", "M3 13h10"],
  intel: ["M6.5 2.5C5 2.5 5 5 5 6.5S3.5 8 3.5 8 5 9.5 5 11s0 2.5 1.5 2.5", "M9.5 2.5C11 2.5 11 5 11 6.5s1.5 1.5 1.5 1.5-1.5 1.5-1.5 3 0 2.5-1.5 2.5"],
  memory: ["M4 2.5h8v11l-4-3-4 3z"],
  image: ["M2.5 3.5h11v9h-11z", "M5.5 7a1 1 0 1 0 0-2 1 1 0 0 0 0 2z", "M2.5 11 6 8l3 2.5 2.5-2 2 1.5"],
  showcase: ["M1.5 8S4 4 8 4s6.5 4 6.5 4-2.5 4-6.5 4S1.5 8 1.5 8z", "M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"],
  ask: ["M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12z", "M6.2 6.2A1.9 1.9 0 0 1 9.9 6.8c0 1.3-1.9 1.5-1.9 2.7", "M8 11.4h.01"],
  tool: ["M4 8h.01", "M8 8h.01", "M12 8h.01"],
};

function categoryIcon(category) {
  const paths = CATEGORY_ICON_PATHS[category] || CATEGORY_ICON_PATHS.tool;
  return (
    `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none"` +
    ` stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">` +
    paths.map((d) => `<path d="${d}"/>`).join("") +
    `</svg>`
  );
}

const CHEVRON_ICON =
  `<svg class="tool-group-chevron" viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">` +
  `<path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6"` +
  ` stroke-linecap="round" stroke-linejoin="round"/></svg>`;

const HIDDEN_TOOL_NAMES = new Set(["view_image"]);

/**
 * Which category a tool call is summarized under.
 *
 * Every tool is listed explicitly. The fallback deliberately says "tool" and
 * not "file": an unlisted tool is one nobody has classified yet, and guessing
 * "read 1 file" for it describes work that never happened. Add new tools here
 * when they are added to `tools/registry.py`.
 */
const TOOL_CATEGORY = {
  read_file: "read",
  list_dir: "list",
  grep: "search",
  find_files: "search",
  write_file: "write",
  edit_file: "write",
  shell: "shell",
  extensions: "extension",
  web_search: "websearch",
  web_fetch: "webfetch",
  code_intelligence: "intel",
  project_memory: "memory",
  generate_image: "image",
  view_image: "image",
  showcase_file: "showcase",
  ask_user: "ask",
};

function toolCategory(name) {
  return TOOL_CATEGORY[name] || "tool";
}

/** `[verb, singular, plural]` -- rendered as "verb count noun". */
const CATEGORY_LABEL = {
  read: ["Read", "file", "files"],
  list: ["Listed", "directory", "directories"],
  search: ["Searched", "location", "locations"],
  write: ["Edited", "file", "files"],
  shell: ["Ran", "command", "commands"],
  extension: ["Ran", "extension", "extensions"],
  websearch: ["Ran", "web search", "web searches"],
  webfetch: ["Fetched", "page", "pages"],
  intel: ["Looked up", "symbol", "symbols"],
  memory: ["Updated", "memory note", "memory notes"],
  image: ["Generated", "image", "images"],
  showcase: ["Showed", "file", "files"],
  ask: ["Asked", "question", "questions"],
  tool: ["Ran", "tool", "tools"],
};

/** A collapsible group of one or more tool calls, rendered together like a single agent step. */
function toolGroup() {
  const group = document.createElement("div");
  group.className = "tool-group";

  const head = document.createElement("button");
  head.className = "tool-group-head";
  head.type = "button";
  head.innerHTML =
    `<span class="tool-group-icon">${categoryIcon("tool")}</span>` +
    `<span class="tool-group-summary">Working…</span>${CHEVRON_ICON}`;
  head.addEventListener("click", () => group.classList.toggle("open"));

  const body = document.createElement("div");
  body.className = "tool-group-body";

  group.append(head, body);
  group._summary = head.querySelector(".tool-group-summary");
  group._icon = head.querySelector(".tool-group-icon");
  group._body = body;
  group._counts = new Map();
  return group;
}

function refreshGroupSummary(group) {
  const parts = [];
  let leading = null;
  for (const [category, count] of group._counts) {
    const [verb, singular, plural] = CATEGORY_LABEL[category] || CATEGORY_LABEL.tool;
    parts.push(`${verb} ${count} ${count === 1 ? singular : plural}`);
    // The icon shows the category the group did most of; ties keep the one
    // that happened first, since Map preserves insertion order.
    if (!leading || count > leading[1]) leading = [category, count];
  }
  group._summary.textContent = parts.join(", ") || "Working…";
  group._icon.innerHTML = categoryIcon(leading ? leading[0] : "tool");
}

function toolRow(group, name, rawArguments) {
  const category = toolCategory(name);
  group._counts.set(category, (group._counts.get(category) || 0) + 1);
  refreshGroupSummary(group);

  const row = document.createElement("div");
  row.className = "tool-row";

  const head = document.createElement("button");
  head.className = "tool-row-head";
  head.type = "button";
  head.innerHTML =
    `<span class="tool-row-name"></span><span class="tool-row-arg"></span>` +
    `<span class="tool-row-state run">running</span>`;
  head.querySelector(".tool-row-name").textContent = name;
  head.querySelector(".tool-row-arg").textContent = summarizeArgs(rawArguments);
  head.addEventListener("click", () => row.classList.toggle("open"));

  const body = document.createElement("div");
  body.className = "tool-row-body";
  const pre = document.createElement("pre");
  pre.textContent = "…";
  body.appendChild(pre);

  row.append(head, body);
  row._state = head.querySelector(".tool-row-state");
  row._output = pre;
  group._body.appendChild(row);
  return row;
}

function finishToolRow(group, row, output, ok) {
  row._output.textContent = output || "(no output)";
  row._state.textContent = ok ? "done" : "blocked";
  row._state.className = `tool-row-state ${ok ? "ok" : "fail"}`;
  if (!ok) { row.classList.add("open"); group.classList.add("open"); }
}

function fileUrl(path) {
  const conversation = state.active && state.active.id;
  if (!conversation || !path) return "";
  const encodedPath = String(path).split("/").map(encodeURIComponent).join("/");
  return `/api/conversations/${encodeURIComponent(conversation)}/files/${encodedPath}`;
}

function showcaseFile(path) {
  const url = fileUrl(path);
  if (!url) return null;
  const name = String(path).split("/").pop() || path;
  const extension = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  const card = document.createElement("figure");
  card.className = "file-showcase";

  let preview;
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension)) {
    preview = document.createElement("img");
    preview.src = url;
    preview.alt = name;
    preview.loading = "lazy";
  } else if (["mp4", "webm", "mov"].includes(extension)) {
    preview = document.createElement("video");
    preview.src = url;
    preview.controls = true;
  } else if (["mp3", "wav", "ogg", "m4a"].includes(extension)) {
    preview = document.createElement("audio");
    preview.src = url;
    preview.controls = true;
  } else {
    preview = document.createElement("a");
    preview.href = url;
    preview.textContent = "Open file";
    preview.target = "_blank";
    preview.rel = "noopener";
  }
  card.appendChild(preview);
  const caption = document.createElement("figcaption");
  const link = document.createElement("a");
  link.href = url;
  link.textContent = path;
  link.target = "_blank";
  link.rel = "noopener";
  caption.appendChild(link);
  card.appendChild(caption);
  return card;
}

/**
 * Render a stored conversation from its Responses-API items.
 *
 * `entries` carry each item's stored timestamp and token count alongside the
 * payload, which is what lets a reloaded message keep its footer.
 */
function renderEntries(entries) {
  el.messages.replaceChildren();
  const rows = new Map();
  let group = null;
  let todos = null;

  for (const entry of entries) {
    const item = entry && entry.payload;
    if (!item || typeof item !== "object") continue;
    const meta = entry.meta || {};

    if (item.role === "user") {
      group = null;
      el.messages.appendChild(userMessage(textFromContent(item.content), meta.attachments || []));
      continue;
    }
    if (item.type === "message" || item.role === "assistant") {
      const text = textFromContent(item.content);
      if (text.trim()) {
        group = null;
        const bubble = el.messages.appendChild(assistantBubble(text));
        attachFooter(bubble, { at: entry.created_at, outputTokens: meta.output_tokens });
      }
      continue;
    }
    if (item.type === "function_call") {
      // The plan is state, not an event: show the latest version as one card
      // rather than a tool row per revision.
      if (item.name === "todo_write") {
        const parsed = parseTodoArgs(item.arguments);
        if (parsed.length) {
          if (todos && todos.isConnected) renderTodos(todos, parsed);
          else { todos = todoCard(parsed); el.messages.appendChild(todos); group = null; }
        }
        continue;
      }
      if (HIDDEN_TOOL_NAMES.has(item.name)) {
        rows.set(item.call_id || item.id, { hidden: true });
        continue;
      }
      if (!group) { group = toolGroup(); el.messages.appendChild(group); }
      const row = toolRow(group, item.name || "tool", item.arguments);
      rows.set(item.call_id || item.id, { group, row, name: item.name, arguments: item.arguments });
      continue;
    }
    if (item.type === "function_call_output") {
      const entry = rows.get(item.call_id);
      const output = typeof item.output === "string" ? item.output : JSON.stringify(item.output);
      const ok = !/^Error:|did not allow/.test(output);
      if (entry && !entry.hidden) {
        finishToolRow(entry.group, entry.row, output, ok);
        if (ok && entry.name === "showcase_file") {
          try {
            const args = JSON.parse(entry.arguments || "{}");
            const card = showcaseFile(args.path);
            if (card) el.messages.appendChild(card);
          } catch { /* the tool row still records malformed historical arguments */ }
        }
      }
    }
  }

  el.messages.appendChild(el.emptyState);
  el.emptyState.hidden = entries.length > 0;
  scrollDown(true);
}

function parseTodoArgs(raw) {
  try {
    const parsed = JSON.parse(raw || "{}");
    return Array.isArray(parsed.todos) ? parsed.todos : [];
  } catch {
    return [];
  }
}

function textFromContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((part) => part && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
}

/* -- Conversation list ---------------------------------------------------- */

const FOLDER_ICON =
  `<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">` +
  `<path d="M1.5 3.5A1 1 0 0 1 2.5 2.5h3l1 1.3h6.5a1 1 0 0 1 1 1v7.2a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z"` +
  ` fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>`;

/** The last path segment of a workspace, used as its sidebar group label. */
function workspaceLabel(path) {
  if (!path) return "Workspace";
  const parts = String(path).replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

const KEBAB_ICON =
  `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">` +
  `<circle cx="8" cy="3.5" r="1.3" fill="currentColor"/>` +
  `<circle cx="8" cy="8" r="1.3" fill="currentColor"/>` +
  `<circle cx="8" cy="12.5" r="1.3" fill="currentColor"/></svg>`;

/** Close any open conversation menu. */
function closeConversationMenu() {
  const open = el.conversations.querySelector(".conv.menu-open");
  if (open) open.classList.remove("menu-open");
  // The menu lives on <body>, not in the row, so it has to be found globally.
  const menu = document.querySelector(".conv-menu");
  if (menu) menu.remove();
}

/**
 * Place the menu next to its button using fixed coordinates.
 *
 * It is appended to <body> rather than to the row on purpose: the sidebar's
 * conversation list scrolls, and an absolutely positioned child is clipped by
 * that overflow, so the menu of the bottom-most chat would be cut off.
 */
function placeConversationMenu(menu, anchor) {
  const box = anchor.getBoundingClientRect();
  menu.style.visibility = "hidden";
  document.body.appendChild(menu);
  const height = menu.offsetHeight;
  const width = menu.offsetWidth;
  const margin = 6;
  // Flip above the button when there is no room below it.
  const below = box.bottom + margin;
  const top = below + height > window.innerHeight ? box.top - height - margin : below;
  menu.style.top = `${Math.max(margin, top)}px`;
  menu.style.left = `${Math.max(margin, Math.min(box.right - width, window.innerWidth - width - margin))}px`;
  menu.style.visibility = "";
}

function conversationMenu(conversation) {
  const menu = document.createElement("div");
  menu.className = "conv-menu";
  menu.setAttribute("role", "menu");

  const item = (label, onClick, destructive = false) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "menuitem");
    if (destructive) button.className = "destructive";
    button.textContent = label;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      closeConversationMenu();
      onClick();
    });
    return button;
  };

  menu.append(
    item("Rename", () => askRename(conversation)),
    item("Delete", () => askDelete(conversation), true),
  );
  return menu;
}

/**
 * One sidebar row: a button to open it plus a menu button.
 *
 * A div rather than a button, because it contains two buttons and nesting
 * interactive elements is invalid HTML -- which is why the old delete control
 * was a span pretending to be a button.
 */
function conversationRow(conversation) {
  const row = document.createElement("div");
  row.className = "conv" + (conversation.id === state.active?.id ? " active" : "");

  const open = document.createElement("button");
  open.type = "button";
  open.className = "conv-open";
  open.title = conversation.workspace;
  const title = document.createElement("span");
  title.className = "conv-title";
  title.textContent = conversation.title || "Untitled";
  open.appendChild(title);
  open.addEventListener("click", () => openConversation(conversation.id));

  const menuButton = document.createElement("button");
  menuButton.type = "button";
  menuButton.className = "conv-menu-btn";
  menuButton.innerHTML = KEBAB_ICON;
  menuButton.title = "Conversation options";
  menuButton.setAttribute("aria-label", "Conversation options");
  menuButton.setAttribute("aria-haspopup", "menu");
  menuButton.addEventListener("click", (event) => {
    event.stopPropagation();
    const wasOpen = row.classList.contains("menu-open");
    closeConversationMenu();
    if (wasOpen) return;
    row.classList.add("menu-open");
    placeConversationMenu(conversationMenu(conversation), menuButton);
  });

  row.append(open, menuButton);
  return row;
}

function askRename(conversation) {
  state.pendingRename = conversation;
  el.renameInput.value = conversation.title || "";
  el.renameOverlay.hidden = false;
  el.renameInput.focus();
  el.renameInput.select();
}

async function commitRename() {
  const conversation = state.pendingRename;
  if (!conversation) return;
  const title = el.renameInput.value.trim();
  el.renameOverlay.hidden = true;
  state.pendingRename = null;
  // An empty title is a no-op rather than a way to blank the name: the list
  // would then show "Untitled" and the original would be unrecoverable.
  if (!title || title === conversation.title) return;
  const updated = await patch(`/api/conversations/${conversation.id}`, { title })
    .catch((error) => { toast(error.message, true); return null; });
  if (!updated) return;
  if (state.active?.id === conversation.id) {
    state.active.title = updated.title ?? title;
    el.title.textContent = state.active.title || "Untitled";
  }
  await loadConversations();
}

function askDelete(conversation) {
  state.pendingDelete = conversation;
  el.confirmText.textContent =
    `"${conversation.title || "Untitled"}" and its whole history will be deleted. ` +
    "This cannot be undone.";
  el.confirmOverlay.hidden = false;
  el.confirmCancel.focus();
}

async function commitDelete() {
  const conversation = state.pendingDelete;
  el.confirmOverlay.hidden = true;
  state.pendingDelete = null;
  if (!conversation) return;
  await del(`/api/conversations/${conversation.id}`).catch((e) => toast(e.message, true));
  if (state.active?.id === conversation.id) closeConversation();
  await loadConversations();
}

function renderConversationList() {
  closeConversationMenu();
  el.conversations.replaceChildren();

  const groups = new Map(); // label -> { workspace, conversations: [] }
  for (const conversation of state.conversations) {
    const label = workspaceLabel(conversation.workspace);
    if (!groups.has(label)) groups.set(label, { workspace: conversation.workspace, conversations: [] });
    groups.get(label).conversations.push(conversation);
  }

  for (const [label, { workspace, conversations }] of groups) {
    const isActiveGroup = conversations.some((c) => c.id === state.active?.id);
    const wrap = document.createElement("div");
    wrap.className = "conv-group";

    const head = document.createElement("button");
    head.className = "conv-group-head";
    head.type = "button";
    head.innerHTML =
      `${FOLDER_ICON}<span class="conv-group-name"></span>` +
      `<span class="conv-group-add" title="New chat in ${escapeHtml(workspace)}">+</span>`;
    head.querySelector(".conv-group-name").textContent = label;
    head.title = workspace;
    head.addEventListener("click", (event) => {
      if (event.target.closest(".conv-group-add")) return;
      wrap.classList.toggle("collapsed");
    });
    head.querySelector(".conv-group-add").addEventListener("click", (event) => {
      event.stopPropagation();
      el.newWorkspace.value = workspace;
      fillModes(el.newMode, state.active?.permission_mode || state.meta.default_mode);
      el.newOverlay.hidden = false;
      el.newWorkspace.focus();
    });

    const body = document.createElement("div");
    body.className = "conv-group-body";

    for (const conversation of conversations) {
      body.appendChild(conversationRow(conversation));
    }

    if (!isActiveGroup) wrap.classList.toggle("collapsed", groups.size > 1 && conversations.length > 4);
    wrap.append(head, body);
    el.conversations.appendChild(wrap);
  }
}

async function loadConversations() {
  const data = await get("/api/conversations");
  state.conversations = data.conversations || [];
  renderConversationList();
}

/* -- Live stream ---------------------------------------------------------- */

function setBusy(busy, label = "Working…") {
  state.busy = busy;
  el.statusBar.hidden = !busy;
  el.statusText.textContent = label;
  el.send.disabled = busy || !state.active;
  el.compactContext.disabled = busy || !state.active;
}

function resetLive() {
  state.liveText = null;
  state.liveReasoning = null;
  state.liveGroup = null;
  state.liveTodos = null;
  state.toolCards.clear();
}

/* -- Token usage / context window ----------------------------------------- */

/** Circumference of the ring in index.html (2*pi*9), kept in sync with its CSS. */
const RING_CIRCUMFERENCE = 56.55;

const formatTokens = (n) => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
};

function resetUsage() {
  el.usage.hidden = true;
  el.usage.classList.remove("warn", "high");
  el.usageRingFill.style.strokeDashoffset = RING_CIRCUMFERENCE;
}

/**
 * Paint the context ring. Called both from live `usage` events and when a
 * conversation is opened, so the ring reflects the stored chat size at all
 * times rather than only while a run happens to be streaming.
 */
function updateUsage({ contextTokens, contextWindow, totalTokens }) {
  const hasContext =
    Number.isFinite(contextTokens) && Number.isFinite(contextWindow) && contextWindow > 0;
  const hasTotal = Number.isFinite(totalTokens) && totalTokens > 0;
  if (!hasContext && !hasTotal) {
    resetUsage();
    return;
  }

  el.usage.hidden = false;
  const fraction = hasContext ? Math.min(1, contextTokens / contextWindow) : 0;

  // The ring alone is too small to read a value off, so the percentage is
  // spelled out next to it; the token total is what was actually asked for.
  const parts = [];
  if (hasContext) parts.push(`${Math.round(fraction * 100)}%`);
  if (hasTotal) parts.push(`${formatTokens(totalTokens)} tokens`);
  el.usageLabel.textContent = parts.join("  ·  ");

  el.usageRingFill.style.strokeDashoffset = RING_CIRCUMFERENCE * (1 - fraction);
  el.usage.classList.toggle("warn", fraction >= 0.6 && fraction < 0.85);
  el.usage.classList.toggle("high", fraction >= 0.85);

  const tips = [];
  if (hasContext) {
    // Say "input budget", not just "context": the window is the input
    // allowance, with output reserved separately.
    tips.push(
      `${formatTokens(contextTokens)} of ${formatTokens(contextWindow)} input ` +
        `budget (${Math.round(fraction * 100)}%)`
    );
  }
  if (hasTotal) tips.push(`${totalTokens.toLocaleString()} tokens in this conversation`);
  el.usage.title = tips.join("\n");
}

/* -- ChatGPT plan usage ---------------------------------------------------- */

/** "in 3 d", "in 5 h" -- how long until the allowance resets. */
function untilReset(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const days = Math.floor(seconds / 86400);
  if (days >= 1) return `resets in ${days} d`;
  const hours = Math.floor(seconds / 3600);
  if (hours >= 1) return `resets in ${hours} h`;
  return `resets in ${Math.max(1, Math.floor(seconds / 60))} min`;
}

/**
 * Show how much of the ChatGPT plan's weekly allowance is gone.
 *
 * These figures come from the `x-codex-*` response headers, which only arrive
 * on a real request -- so this stays hidden until the first message has gone
 * out on a fresh install.
 */
function updatePlanUsage(usage) {
  const weekly = usage && usage.weekly;
  if (!weekly || !Number.isFinite(weekly.used_percent)) {
    el.planUsage.hidden = true;
    return;
  }

  const fraction = Math.min(1, Math.max(0, weekly.used_percent / 100));
  el.planUsage.hidden = false;
  el.planLabel.textContent = `${Math.round(weekly.used_percent)}% weekly`;
  el.planRingFill.style.strokeDashoffset = RING_CIRCUMFERENCE * (1 - fraction);
  el.planUsage.classList.toggle("warn", fraction >= 0.6 && fraction < 0.85);
  el.planUsage.classList.toggle("high", fraction >= 0.85);

  const lines = [
    `${Math.round(weekly.used_percent)}% of your weekly ChatGPT allowance used`,
  ];
  const reset = untilReset(weekly.reset_after_seconds);
  if (reset) lines.push(reset.charAt(0).toUpperCase() + reset.slice(1));
  if (usage.plan_type) lines.push(`Plan: ${usage.plan_type}`);
  el.planUsage.title = lines.join("\n");
}

async function loadPlanUsage() {
  try {
    const data = await get("/api/usage");
    updatePlanUsage(data.usage);
  } catch {
    // Non-essential: a missing allowance readout must not break startup.
  }
}

/** Recompute the ring from whatever the active conversation currently knows. */
function refreshUsageFromConversation() {
  const active = state.active;
  if (!active) {
    resetUsage();
    return;
  }
  updateUsage({
    contextTokens: active.context_tokens,
    contextWindow: active.context_window,
    totalTokens: active.total_tokens,
  });
}

function connectStream(conversationId) {
  disconnectStream();
  const stream = new EventSource(`/api/conversations/${conversationId}/events`);
  state.stream = stream;

  const on = (name, handler) =>
    stream.addEventListener(name, (event) => {
      let data = {};
      try { data = JSON.parse(event.data); } catch { /* keepalive or noise */ }
      handler(data);
    });

  on("ready", (data) => setBusy(Boolean(data.busy)));
  on("model_routing", () => setBusy(true, "Choosing the best model…"));
  on("model_selected", (data) => {
    append(modelDecisionCard(data));
    setBusy(true, `Auto selected ${modelDisplayName(data)}…`);
    scrollDown();
  });

  on("step", (data) => {
    // A fresh model turn: later text/tool calls belong in their own bubble/group.
    finalizeLiveText();
    state.liveGroup = null;
    state.lastTurnOutputTokens = null;
    setBusy(true, data.step > 1 ? `Working… (step ${data.step})` : "Working…");
  });

  on("text_delta", (data) => {
    if (!state.liveText) state.liveText = append(assistantBubble(""));
    const bubble = state.liveText;
    bubble.dataset.raw += data.delta || "";
    bubble.innerHTML = formatMarkdown(bubble.dataset.raw);
    scrollDown();
  });

  on("reasoning_delta", (data) => {
    if (!state.liveReasoning) state.liveReasoning = append(reasoningBlock(""));
    state.liveReasoning.textContent += data.delta || "";
    scrollDown();
  });

  on("tool_started", (data) => {
    // todo_write renders as its own plan card via the `todos` event.
    if (data.name === "todo_write") { setBusy(true, "Updating the plan…"); return; }
    if (HIDDEN_TOOL_NAMES.has(data.name)) { setBusy(true, "Viewing image…"); return; }
    finalizeLiveText();
    state.liveReasoning = null;
    if (!state.liveGroup) state.liveGroup = append(toolGroup());
    const row = toolRow(state.liveGroup, data.name || "tool", data.arguments);
    state.toolCards.set(data.call_id, { group: state.liveGroup, row, name: data.name, arguments: data.arguments });
    scrollDown();
    setBusy(true, `Running ${data.name}…`);
  });

  on("tool_finished", (data) => {
    const entry = state.toolCards.get(data.call_id);
    if (entry && !entry.hidden) {
      finishToolRow(entry.group, entry.row, data.output, data.ok);
      if (data.ok && entry.name === "showcase_file") {
        try {
          const args = JSON.parse(entry.arguments || "{}");
          const card = showcaseFile(args.path);
          if (card) append(card);
        } catch { /* tool arguments were already reported as invalid */ }
      }
    }
    setBusy(true);
  });

  on("usage", (data) => {
    // Mirror onto the active conversation so the ring survives a reconnect
    // and stays put once the run ends.
    if (state.active) {
      if (Number.isFinite(data.context_tokens)) state.active.context_tokens = data.context_tokens;
      if (Number.isFinite(data.context_window)) state.active.context_window = data.context_window;
      if (Number.isFinite(data.total_tokens)) state.active.total_tokens = data.total_tokens;
    }
    if (Number.isFinite(data.output_tokens)) state.lastTurnOutputTokens = data.output_tokens;
    updateUsage({
      contextTokens: data.context_tokens,
      contextWindow: data.context_window,
      totalTokens: data.total_tokens,
    });
  });

  on("plan_usage", (data) => updatePlanUsage(data));

  on("compaction_started", () => setBusy(true, "Compacting earlier conversation…"));
  on("compacted", (data) => {
    setBusy(true);
    if (state.active && Number.isFinite(data.context_tokens)) {
      state.active.context_tokens = data.context_tokens;
      updateUsage({
        contextTokens: data.context_tokens,
        contextWindow: state.active.context_window,
        totalTokens: state.active.total_tokens,
      });
    }
    toast(`Compacted earlier context; kept the latest ${data.kept_items} items.`);
  });
  on("compaction_failed", (data) => {
    toast(`Could not compact context: ${data.message}`, true);
  });
  on("compaction_skipped", (data) => toast(data.message || "Nothing to compact."));
  on("compaction_finished", () => setBusy(false));

  on("todos", (data) => {
    const todos = data.todos || [];
    if (!todos.length) return;
    // One card per run, updated in place -- appending a fresh copy on every
    // status flip would bury the conversation in near-identical lists.
    if (state.liveTodos && state.liveTodos.isConnected) {
      renderTodos(state.liveTodos, todos);
    } else {
      state.liveTodos = append(todoCard(todos));
    }
    finalizeLiveText();
  });

  on("judge_started", () => setBusy(true, "Safety model is reviewing the command…"));

  on("judge_finished", (data) => {
    if (!data.allowed) toast("The safety model blocked a command.", true);
    setBusy(true);
  });

  on("permission_request", (data) => {
    state.permissionQueue.push(data);
    showNextPermission();
  });

  on("question_request", (data) => {
    state.questionQueue.push(data);
    showNextQuestion();
  });

  on("title", (data) => {
    if (state.active) state.active.title = data.title;
    el.title.textContent = data.title;
    loadConversations();
  });

  on("done", () => { finalizeLiveText(); setBusy(false); resetLive(); });
  on("cancelled", () => {
    finalizeLiveText();
    setBusy(false);
    resetLive();
    toast("Run cancelled.");
  });

  on("error", (data) => {
    finalizeLiveText();
    append(errorBubble(data.message || "Something went wrong."));
    setBusy(false);
    resetLive();
  });

  stream.onerror = () => {
    // EventSource reconnects on its own; only surface a hard failure.
    if (stream.readyState === EventSource.CLOSED) setBusy(false);
  };
}

function disconnectStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
}

/* -- Permissions ---------------------------------------------------------- */

/** Colourise a unified diff. Built by hand rather than with innerHTML on raw
 *  diff text -- the content is file data and must never become markup. */
function renderDiff(diff) {
  el.permDiff.replaceChildren();
  if (!diff) {
    el.permDiff.hidden = true;
    return;
  }
  el.permDiff.hidden = false;
  for (const line of diff.split("\n")) {
    const row = document.createElement("span");
    if (line.startsWith("+++") || line.startsWith("---")) row.className = "hunk";
    else if (line.startsWith("@@")) row.className = "hunk";
    else if (line.startsWith("+")) row.className = "add";
    else if (line.startsWith("-")) row.className = "del";
    row.textContent = `${line}\n`;
    el.permDiff.appendChild(row);
  }
}

function showNextPermission() {
  if (state.currentPermission || !state.permissionQueue.length) return;
  const request = state.permissionQueue.shift();
  state.currentPermission = request;

  el.permKind.textContent =
    request.kind === "shell"
      ? "The agent wants to run a shell command."
      : "The agent wants to write to a file.";
  el.permDetail.textContent = request.detail;
  el.permScope.textContent = request.session_scope
    ? `Session scope: ${request.session_scope}`
    : "";
  renderDiff(request.diff);
  el.permJudge.hidden = !request.judge_reason;
  el.permJudgeReason.textContent = request.judge_reason || "";
  el.permFeedback.value = "";
  el.permOverlay.hidden = false;
  el.permOnce.focus();
}

async function replyPermission(reply) {
  const request = state.currentPermission;
  if (!request || !state.active) return;
  el.permOverlay.hidden = true;
  state.currentPermission = null;
  try {
    await post(`/api/conversations/${state.active.id}/permission/${request.id}`, {
      reply,
      feedback: el.permFeedback.value,
    });
  } catch (error) {
    toast(error.message, true);
  }
  showNextPermission();
}

function showNextQuestion() {
  if (state.currentQuestion || !state.questionQueue.length) return;
  const question = state.questionQueue.shift();
  state.currentQuestion = question;
  el.questionTitle.textContent = question.header || "Question";
  el.questionText.textContent = question.question;
  el.questionOptions.replaceChildren();
  for (const option of question.options || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "question-option";
    const label = document.createElement("strong");
    label.textContent = option.label;
    button.appendChild(label);
    if (option.description) {
      const description = document.createElement("span");
      description.textContent = option.description;
      button.appendChild(description);
    }
    button.addEventListener("click", () => replyQuestion(option.label));
    el.questionOptions.appendChild(button);
  }
  el.questionAnswer.value = "";
  el.questionAnswer.hidden = !question.allow_freeform;
  el.questionSubmit.hidden = !question.allow_freeform;
  el.questionCancel.textContent = question.allow_freeform ? "Cancel" : "Cancel question";
  el.questionOverlay.hidden = false;
  (question.allow_freeform ? el.questionAnswer : el.questionOptions.querySelector("button"))?.focus();
}

async function replyQuestion(answer) {
  const question = state.currentQuestion;
  if (!question || !state.active) return;
  const reply = String(answer || el.questionAnswer.value).trim() || "The user cancelled the question.";
  el.questionOverlay.hidden = true;
  state.currentQuestion = null;
  try {
    await post(`/api/conversations/${state.active.id}/question/${question.id}`, { answer: reply });
  } catch (error) {
    toast(error.message, true);
  }
  showNextQuestion();
}

/* -- Conversation lifecycle ----------------------------------------------- */

async function openConversation(conversationId) {
  const data = await get(`/api/conversations/${conversationId}`).catch((error) => {
    toast(error.message, true);
    return null;
  });
  if (!data) return;

  state.active = data;
  resetLive();
  state.permissionQueue = [];
  state.currentPermission = null;
  el.permOverlay.hidden = true;
  state.questionQueue = [];
  state.currentQuestion = null;
  el.questionOverlay.hidden = true;

  el.title.textContent = data.title || "Untitled";
  el.workspace.textContent = data.workspace;
  el.modeSelect.value = data.permission_mode;
  ensureModelOption(data.model);
  el.modelSelect.value = data.model;
  renderCustomSelect(el.modeSelect);
  renderCustomSelect(el.modelSelect);
  fillEffortSelect(el.effortSelect, data.reasoning_effort || "", data.model);
  renderFastToggle(data.fast_mode, data.model);

  renderEntries(data.entries || []);
  renderConversationList();
  refreshUsageFromConversation();
  setBusy(Boolean(data.busy));
  connectStream(conversationId);

  for (const pending of data.pending_permissions || []) state.permissionQueue.push(pending);
  showNextPermission();
  el.prompt.focus();
}

function closeConversation() {
  disconnectStream();
  state.active = null;
  el.title.textContent = "No conversation";
  el.workspace.textContent = "";
  renderEntries([]);
  setBusy(false);
  resetUsage();
  clearAttachments();
}

function renderAttachments() {
  el.attachments.replaceChildren();
  el.attachments.hidden = state.attachments.length === 0;
  for (const attachment of state.attachments) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `Remove ${attachment.name}`;
    remove.setAttribute("aria-label", remove.title);
    remove.addEventListener("click", () => {
      state.attachments = state.attachments.filter((item) => item.path !== attachment.path);
      renderAttachments();
    });

    if (String(attachment.type || "").startsWith("image/")) {
      const preview = document.createElement("div");
      preview.className = "attachment-preview";
      preview.title = attachment.name;
      const image = document.createElement("img");
      image.src = fileUrl(attachment.path);
      image.alt = attachment.name;
      image.loading = "lazy";
      remove.className = "attachment-preview-remove";
      preview.append(image, remove);
      el.attachments.appendChild(preview);
      continue;
    }

    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    const name = document.createElement("span");
    name.textContent = attachment.name;
    name.title = attachment.path;
    chip.append(name, remove);
    el.attachments.appendChild(chip);
  }
}

function clearAttachments() {
  state.attachments = [];
  renderAttachments();
}

async function uploadAttachment(file) {
  if (!state.active) throw new Error("Open a conversation before pasting a file.");
  if (file.size > 50 * 1024 * 1024) throw new Error("Files must be 50 MB or smaller.");
  const form = new FormData();
  form.append("file", file, file.name || "pasted-file");
  const response = await fetch(`/api/conversations/${state.active.id}/uploads`, {
    method: "POST",
    body: form,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Upload failed (${response.status})`);
  return data;
}

function clipboardFiles(clipboardData) {
  if (!clipboardData) return [];
  const files = [];
  const seen = new Set();
  const add = (file) => {
    if (!(file instanceof File)) return;
    const key = `${file.name}:${file.size}:${file.type}:${file.lastModified}`;
    if (!seen.has(key)) { seen.add(key); files.push(file); }
  };
  for (const file of clipboardData.files || []) add(file);
  for (const item of clipboardData.items || []) {
    if (item.kind === "file") add(item.getAsFile());
  }
  return files;
}

async function navigatorClipboardFiles() {
  if (!navigator.clipboard?.read) return [];
  try {
    const items = await navigator.clipboard.read();
    const files = [];
    for (const item of items) {
      const type = item.types.find((candidate) => !candidate.startsWith("text/"));
      if (!type) continue;
      const blob = await item.getType(type);
      const extension = type === "image/png" ? "png" : type === "image/jpeg" ? "jpg" : "bin";
      files.push(new File([blob], `pasted-file.${extension}`, { type }));
    }
    return files;
  } catch {
    // Clipboard read permission is unavailable in some WebView backends.
    return [];
  }
}

async function nativeClipboardImage() {
  if (!window.pywebview?.api?.read_clipboard_image) return [];
  try {
    const image = await window.pywebview.api.read_clipboard_image();
    if (!image?.data || !image.type) return [];
    const binary = atob(image.data);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return [new File([bytes], image.name || "pasted-screenshot.png", { type: image.type })];
  } catch {
    return [];
  }
}

async function pastedFilesFallback() {
  const browserFiles = await navigatorClipboardFiles();
  return browserFiles.length ? browserFiles : nativeClipboardImage();
}

async function addPastedFiles(files) {
  if (!files.length || !state.active || state.busy) return;
  state.uploadsInProgress += files.length;
  setBusy(true, "Uploading attachment…");
  try {
    const uploaded = await Promise.all([...files].map(uploadAttachment));
    state.attachments.push(...uploaded);
    renderAttachments();
    toast(`${uploaded.length} file${uploaded.length === 1 ? "" : "s"} attached.`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.uploadsInProgress -= files.length;
    setBusy(false);
  }
}

async function sendMessage() {
  const text = el.prompt.value.trim();
  if ((!text && !state.attachments.length) || !state.active || state.busy) return;
  if (state.uploadsInProgress) {
    toast("Wait for the attachment upload to finish.");
    return;
  }

  const attachments = [...state.attachments];
  const attachmentPaths = attachments.map((attachment) => attachment.path);
  const attachmentNote = attachmentPaths.length
    ? `Attached workspace files:\n${attachmentPaths.map((path) => `- ${path}`).join("\n")}`
    : "";
  const message = [attachmentNote, text].filter(Boolean).join("\n\n") || "Please inspect the attached file.";

  el.prompt.value = "";
  el.prompt.style.height = "auto";
  el.emptyState.hidden = true;
  append(userMessage(text, attachments));
  resetLive();
  setBusy(true);

  try {
    await post(`/api/conversations/${state.active.id}/messages`, { text: message, attachments });
    clearAttachments();
  } catch (error) {
    append(errorBubble(error.message));
    setBusy(false);
  }
}

/* -- Setup ---------------------------------------------------------------- */

function ensureModelOption(model) {
  if (!model || [...el.modelSelect.options].some((o) => o.value === model)) return;
  const option = document.createElement("option");
  option.value = model;
  option.textContent = model === "auto" ? "Auto" : model;
  el.modelSelect.appendChild(option);
  renderCustomSelect(el.modelSelect);
}

function fillModes(select, selected) {
  select.replaceChildren();
  for (const mode of state.meta.modes) {
    const option = document.createElement("option");
    option.value = mode.value;
    option.textContent = mode.label;
    select.appendChild(option);
  }
  if (selected) select.value = selected;
  renderCustomSelect(select);
}

/**
 * Which reasoning levels a model accepts, from the catalog.
 *
 * Not a constant: Sol and Terra reach "ultra", Luna stops at "max", GPT-5.5 at
 * "xhigh". Offering a level the model rejects earns an HTTP 400, so the list
 * follows the model. Auto has no single answer, so it gets the full set.
 */
function effortsFor(model) {
  if (model && model !== "auto") {
    const capability = state.capabilities[model];
    if (capability && capability.efforts && capability.efforts.length) return capability.efforts;
  }
  return state.efforts;
}

function supportsFast(model) {
  if (!model) return false;
  // Auto may route to any model, and every routable one offers Fast today.
  if (model === "auto") return Object.values(state.capabilities).some((c) => c.fast);
  const capability = state.capabilities[model];
  return Boolean(capability && capability.fast);
}

/**
 * Fill an effort picker. The empty value comes first and means "use the
 * model's own default", which Codex reports per model -- Sol defaults to low
 * while Terra and Luna default to medium, so hard-coding one here would
 * silently override the model's own tuning.
 */
function fillEffortSelect(select, selected = "", model = "") {
  const efforts = effortsFor(model);
  select.replaceChildren();
  for (const value of ["", ...efforts]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value ? `Effort: ${value}` : "Effort: auto";
    select.appendChild(option);
  }
  select.value = efforts.includes(selected) ? selected : "";
  renderCustomSelect(select);
}

/**
 * Reflect whether this conversation asks for the Fast tier.
 *
 * There is deliberately no "was it granted" state. The response echoes
 * `service_tier: "default"` for every request -- even for a model that has no
 * fast tier at all -- so any such indicator would be invented, and would have
 * reported a working Fast mode as denied.
 */
function renderFastToggle(requested, model) {
  el.fastToggle.hidden = !supportsFast(model);
  el.fastToggle.setAttribute("aria-pressed", String(Boolean(requested)));
}

async function loadModels() {
  try {
    const data = await get("/api/models");
    state.models = ["auto", ...(data.models || []).filter((model) => model !== "auto")];
    state.efforts = data.efforts || ["low", "medium", "high"];
    state.capabilities = data.capabilities || {};
  } catch (error) {
    toast(`Could not load models: ${error.message}`, true);
    state.models = ["auto", state.meta.default_model];
    state.efforts = ["low", "medium", "high"];
    state.capabilities = {};
  }
  el.modelSelect.replaceChildren();
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model === "auto" ? "Auto" : model;
    el.modelSelect.appendChild(option);
  }
  renderCustomSelect(el.modelSelect);
  const active = state.active || {};
  fillEffortSelect(el.effortSelect, active.reasoning_effort || "", el.modelSelect.value);
  fillEffortSelect(el.newEffort, "", el.modelSelect.value);
  renderFastToggle(active.fast_mode, el.modelSelect.value);
}

/* -- Custom selects ------------------------------------------------------- */

const customSelects = new Map();

function closeCustomSelects(except = null) {
  for (const picker of customSelects.values()) {
    if (picker === except) continue;
    picker.root.classList.remove("open");
    picker.trigger.setAttribute("aria-expanded", "false");
    picker.menu.hidden = true;
  }
}

function renderCustomSelect(select) {
  const picker = customSelects.get(select);
  if (!picker) return;
  const selected = select.selectedOptions[0];
  picker.trigger.textContent = selected?.textContent || "Select…";
  picker.menu.replaceChildren();
  for (const option of select.options) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "custom-select-option";
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(option.selected));
    button.textContent = option.textContent;
    button.addEventListener("click", () => {
      select.value = option.value;
      renderCustomSelect(select);
      closeCustomSelects();
      select.dispatchEvent(new Event("change", { bubbles: true }));
      picker.trigger.focus();
    });
    picker.menu.appendChild(button);
  }
}

function changeCustomSelect(select, direction) {
  const index = Math.max(0, [...select.options].findIndex((option) => option.selected));
  const target = select.options[Math.max(0, Math.min(select.options.length - 1, index + direction))];
  if (!target || target.selected) return;
  select.value = target.value;
  renderCustomSelect(select);
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function initCustomSelects() {
  for (const root of document.querySelectorAll("[data-select-picker]")) {
    const select = root.querySelector("select");
    const trigger = root.querySelector(".custom-select-trigger");
    const menu = root.querySelector(".custom-select-menu");
    const picker = { root, trigger, menu };
    customSelects.set(select, picker);
    trigger.setAttribute("aria-label", select.title || root.closest("label")?.querySelector("span")?.textContent || "Select option");
    trigger.addEventListener("click", () => {
      const opening = menu.hidden;
      closeCustomSelects(picker);
      root.classList.toggle("open", opening);
      trigger.setAttribute("aria-expanded", String(opening));
      menu.hidden = !opening;
      if (opening) menu.querySelector("[aria-selected='true']")?.focus();
    });
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (menu.hidden) changeCustomSelect(select, event.key === "ArrowDown" ? 1 : -1);
      } else if ((event.key === "Enter" || event.key === " ") && menu.hidden) {
        event.preventDefault();
        trigger.click();
      }
    });
    renderCustomSelect(select);
  }
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-select-picker]")) closeCustomSelects();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeCustomSelects();
  });
}

function wireEvents() {
  el.authLogin.addEventListener("click", startAuthLogin);
  el.settingsLogin.addEventListener("click", startAuthLogin);
  el.settingsButton.addEventListener("click", async () => {
    await refreshAuth().catch((error) => toast(error.message, true));
    await Promise.all([
      loadGlobalSettings(),
      loadProjectSettings(),
      loadCodexImport(),
      loadBehaviour(),
    ]).catch((error) => toast(error.message, true));
    el.importStatus.textContent = "";
    selectSettingsTab("account");
    el.settingsOverlay.hidden = false;
  });
  for (const tab of el.settingsTabs) {
    tab.addEventListener("click", () => selectSettingsTab(tab.dataset.settingsTab));
  }
  el.behaviourSave.addEventListener("click", () => {
    saveBehaviour(collectBehaviour()).catch((error) => {
      el.behaviourStatus.textContent = error.message;
      toast(error.message, true);
    });
  });
  el.behaviourReset.addEventListener("click", () => {
    if (!state.behaviour) return;
    const defaults = {};
    for (const setting of state.behaviour.schema.settings) defaults[setting.key] = setting.default;
    renderBehaviour(state.behaviour.schema, defaults);
    // Rendered, not saved: restoring is a proposal until the user confirms it
    // with Save, the same as any other edit in this panel.
    el.behaviourStatus.textContent = "Defaults filled in — press Save to apply them.";
  });

  el.codexImport.addEventListener("click", () => {
    runCodexImport().catch((error) => { el.importStatus.textContent = error.message; });
  });
  el.settingsClose.addEventListener("click", () => { el.settingsOverlay.hidden = true; });
  el.globalMemorySave.addEventListener("click", () => {
    saveGlobalMemory().catch((error) => toast(error.message, true));
  });
  el.projectMemorySave.addEventListener("click", () => {
    saveProjectSetting("memory", el.projectMemory).catch((error) => toast(error.message, true));
  });
  el.projectMcpSave.addEventListener("click", () => {
    saveProjectSetting("mcp", el.projectMcpConfig).catch((error) => toast(error.message, true));
  });
  el.projectLspSave.addEventListener("click", () => {
    saveProjectSetting("lsp", el.projectLspConfig).catch((error) => toast(error.message, true));
  });

  el.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });

  el.prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });

  el.prompt.addEventListener("input", () => {
    el.prompt.style.height = "auto";
    el.prompt.style.height = `${Math.min(el.prompt.scrollHeight, 220)}px`;
  });

  document.addEventListener("paste", (event) => {
    const files = clipboardFiles(event.clipboardData);
    if (files.length) {
      event.preventDefault();
      addPastedFiles(files);
      return;
    }
    // WebKitGTK sometimes omits files from the paste event but exposes them
    // through the asynchronous Clipboard API while the key gesture is active.
    const types = [...(event.clipboardData?.types || [])];
    // Empty types are another WebKitGTK quirk, so let the native bridge try
    // them too. Pure text pastes remain untouched.
    if (types.length === 0 || types.some((type) => !type.startsWith("text/"))) {
      pastedFilesFallback().then((clipboardFiles) => {
        if (clipboardFiles.length) addPastedFiles(clipboardFiles);
      });
    }
  });

  el.stopRun.addEventListener("click", async () => {
    if (!state.active) return;
    await post(`/api/conversations/${state.active.id}/cancel`).catch(() => {});
  });

  el.compactContext.addEventListener("click", async () => {
    if (!state.active || state.busy) return;
    setBusy(true, "Compacting earlier conversation…");
    await post(`/api/conversations/${state.active.id}/compact`).catch((error) => {
      setBusy(false);
      toast(error.message, true);
    });
  });

  el.modeSelect.addEventListener("change", async () => {
    if (!state.active) return;
    await patch(`/api/conversations/${state.active.id}`, { mode: el.modeSelect.value })
      .then(() => toast(`Permission mode: ${el.modeSelect.selectedOptions[0].textContent}`))
      .catch((error) => toast(error.message, true));
  });

  el.modelSelect.addEventListener("change", async () => {
    const model = el.modelSelect.value;
    // The level list and Fast availability follow the model, so they have to
    // be rebuilt here -- a level the new model rejects would be a 400.
    fillEffortSelect(el.effortSelect, el.effortSelect.value, model);
    renderFastToggle(state.active && state.active.fast_mode, model);
    if (!state.active) return;
    const body = { model };
    if (el.effortSelect.value !== (state.active.reasoning_effort || "")) {
      body.reasoning_effort = el.effortSelect.value;
    }
    const updated = await patch(`/api/conversations/${state.active.id}`, body)
      .catch((error) => { toast(error.message, true); return null; });
    if (updated) Object.assign(state.active, updated);
  });

  el.renameSave.addEventListener("click", () => {
    commitRename().catch((error) => toast(error.message, true));
  });
  el.renameCancel.addEventListener("click", () => {
    el.renameOverlay.hidden = true;
    state.pendingRename = null;
  });
  el.renameInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    commitRename().catch((error) => toast(error.message, true));
  });

  el.confirmOk.addEventListener("click", () => {
    commitDelete().catch((error) => toast(error.message, true));
  });
  el.confirmCancel.addEventListener("click", () => {
    el.confirmOverlay.hidden = true;
    state.pendingDelete = null;
  });

  // A menu anchored to a row must close on any click elsewhere, or it
  // outlives the row it belongs to. Escape is handled with the overlays below.
  document.addEventListener("click", () => closeConversationMenu());

  el.fastToggle.addEventListener("click", async () => {
    const next = el.fastToggle.getAttribute("aria-pressed") !== "true";
    renderFastToggle(next, el.modelSelect.value);
    if (!state.active) return;
    const updated = await patch(`/api/conversations/${state.active.id}`, { fast_mode: next })
      .catch((error) => { toast(error.message, true); return null; });
    if (updated) Object.assign(state.active, updated);
  });

  el.effortSelect.addEventListener("change", async () => {
    if (!state.active) return;
    // Auto overrides this per run, so say so instead of letting the picker
    // look like it was ignored.
    if (el.modelSelect.value === "auto" && el.effortSelect.value) {
      toast("Auto picks the effort itself; this applies if you choose a model.");
    }
    await patch(`/api/conversations/${state.active.id}`, {
      reasoning_effort: el.effortSelect.value,
    }).catch((error) => toast(error.message, true));
    if (state.active) state.active.reasoning_effort = el.effortSelect.value;
  });

  el.permOnce.addEventListener("click", () => replyPermission("once"));
  el.permSession.addEventListener("click", () => replyPermission("session"));
  el.permDeny.addEventListener("click", () => replyPermission("deny"));
  el.questionSubmit.addEventListener("click", () => replyQuestion());
  el.questionCancel.addEventListener("click", () => replyQuestion("The user cancelled the question."));
  el.questionAnswer.addEventListener("keydown", (event) => {
    if (event.key === "Enter") replyQuestion();
  });

  el.newChat.addEventListener("click", () => {
    el.newWorkspace.value = state.active?.workspace || state.meta.cwd;
    fillModes(el.newMode, state.meta.default_mode);
    el.newOverlay.hidden = false;
    el.newWorkspace.focus();
  });

  el.newCancel.addEventListener("click", () => { el.newOverlay.hidden = true; });

  // The native folder picker only exists inside the pywebview window (not
  // in --headless/browser mode), so it stays hidden until the page confirms
  // window.pywebview.api is actually there.
  const revealBrowseButton = () => { el.browseWorkspace.hidden = false; };
  if (window.pywebview) revealBrowseButton();
  window.addEventListener("pywebviewready", revealBrowseButton);

  el.browseWorkspace.addEventListener("click", async () => {
    try {
      const chosen = await window.pywebview.api.choose_directory(el.newWorkspace.value.trim());
      if (chosen) el.newWorkspace.value = chosen;
    } catch (error) {
      toast(error.message, true);
    }
  });

  el.newCreate.addEventListener("click", async () => {
    try {
      const conversation = await post("/api/conversations", {
        workspace: el.newWorkspace.value.trim(),
        mode: el.newMode.value,
        model: el.modelSelect.value || state.meta.default_model,
        reasoning_effort: el.newEffort.value,
        fast_mode: el.fastToggle.getAttribute("aria-pressed") === "true",
      });
      el.newOverlay.hidden = true;
      await loadConversations();
      await openConversation(conversation.id);
    } catch (error) {
      toast(error.message, true);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeConversationMenu();
    if (!el.newOverlay.hidden) el.newOverlay.hidden = true;
    if (!el.settingsOverlay.hidden) el.settingsOverlay.hidden = true;
    if (!el.renameOverlay.hidden) {
      el.renameOverlay.hidden = true;
      state.pendingRename = null;
    }
    if (!el.confirmOverlay.hidden) {
      el.confirmOverlay.hidden = true;
      state.pendingDelete = null;
    }
    if (!el.questionOverlay.hidden) replyQuestion("The user cancelled the question.");
  });
}

/** Keep "just now" from ageing into a lie without re-rendering the messages. */
function startClock() {
  setInterval(() => {
    for (const node of el.messages.querySelectorAll(".msg-time[data-at]")) {
      node.textContent = relativeTime(node.dataset.at);
    }
  }, 60_000);
}

async function loadAuthenticatedApp() {
  await loadModels();
  if (state.appLoaded) return;
  state.appLoaded = true;
  loadPlanUsage();
  await loadConversations();

  if (!state.conversations.length) {
    // First run, nothing to pick from yet: start straight in the current
    // directory instead of forcing a "create a project" dialog before the
    // app is usable at all. "New chat" still opens that dialog for anything
    // else.
    try {
      const conversation = await post("/api/conversations", {
        workspace: state.meta.cwd,
        mode: state.meta.default_mode,
        model: state.meta.default_model,
      });
      await loadConversations();
      await openConversation(conversation.id);
      return;
    } catch (error) {
      toast(error.message, true);
    }
  }

  if (state.conversations.length) {
    await openConversation(state.conversations[0].id);
  } else {
    setBusy(false);
  }
}

async function init() {
  state.meta = await get("/api/meta");
  el.toolCount.textContent = `${state.meta.tools.length} tools`;
  initCustomSelects();
  fillModes(el.modeSelect, state.meta.default_mode);
  wireEvents();
  startClock();
  const auth = await refreshAuth();
  if (auth.authenticated) {
    await loadAuthenticatedApp();
  } else {
    setBusy(false);
  }
}

init().catch((error) => toast(`Startup failed: ${error.message}`, true));
