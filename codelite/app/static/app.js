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
  modeSelect: $("mode-select"),
  composer: $("composer"),
  prompt: $("prompt"),
  send: $("send"),
  statusBar: $("status-bar"),
  statusText: $("status-text"),
  stopRun: $("stop-run"),
  newChat: $("new-chat"),
  toolCount: $("tool-count"),
  toast: $("toast"),
  permOverlay: $("permission-overlay"),
  permKind: $("perm-kind"),
  permDetail: $("perm-detail"),
  permJudge: $("perm-judge"),
  permJudgeReason: $("perm-judge-reason"),
  permFeedback: $("perm-feedback"),
  permOnce: $("perm-once"),
  permSession: $("perm-session"),
  permDeny: $("perm-deny"),
  newOverlay: $("new-overlay"),
  newWorkspace: $("new-workspace"),
  newMode: $("new-mode"),
  newCreate: $("new-create"),
  newCancel: $("new-cancel"),
};

const state = {
  meta: null,
  models: [],
  conversations: [],
  active: null,
  stream: null,
  busy: false,
  liveText: null,
  liveReasoning: null,
  toolCards: new Map(),
  permissionQueue: [],
  currentPermission: null,
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
        .map((block) => {
          const lines = block.split("\n");
          const isList = lines.every((line) => /^\s*[-*]\s+/.test(line));
          const inline = (s) =>
            escapeHtml(s)
              .replace(/`([^`]+)`/g, "<code>$1</code>")
              .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
          if (isList) {
            const items = lines
              .map((line) => `<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`)
              .join("");
            return `<ul>${items}</ul>`;
          }
          return `<p>${inline(block).replace(/\n/g, "<br />")}</p>`;
        })
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

function toolCard(name, rawArguments) {
  const card = document.createElement("div");
  card.className = "tool";

  const head = document.createElement("button");
  head.className = "tool-head";
  head.type = "button";
  head.innerHTML =
    `<span class="tool-name"></span><span class="tool-arg"></span>` +
    `<span class="tool-state run">running</span>`;
  head.querySelector(".tool-name").textContent = name;
  head.querySelector(".tool-arg").textContent = summarizeArgs(rawArguments);
  head.addEventListener("click", () => card.classList.toggle("open"));

  const body = document.createElement("div");
  body.className = "tool-body";
  const pre = document.createElement("pre");
  pre.textContent = "…";
  body.appendChild(pre);

  card.append(head, body);
  card._state = head.querySelector(".tool-state");
  card._output = pre;
  return card;
}

function finishToolCard(card, output, ok) {
  card._output.textContent = output || "(no output)";
  card._state.textContent = ok ? "done" : "blocked";
  card._state.className = `tool-state ${ok ? "ok" : "fail"}`;
  if (!ok) card.classList.add("open");
}

/** Render a stored conversation from its Responses-API items. */
function renderItems(items) {
  el.messages.replaceChildren();
  const cards = new Map();

  for (const item of items) {
    if (!item || typeof item !== "object") continue;

    if (item.role === "user") {
      el.messages.appendChild(userBubble(textFromContent(item.content)));
      continue;
    }
    if (item.type === "message" || item.role === "assistant") {
      const text = textFromContent(item.content);
      if (text.trim()) el.messages.appendChild(assistantBubble(text));
      continue;
    }
    if (item.type === "function_call") {
      const card = toolCard(item.name || "tool", item.arguments);
      cards.set(item.call_id || item.id, card);
      el.messages.appendChild(card);
      continue;
    }
    if (item.type === "function_call_output") {
      const card = cards.get(item.call_id);
      const output = typeof item.output === "string" ? item.output : JSON.stringify(item.output);
      if (card) finishToolCard(card, output, !/^Error:|did not allow/.test(output));
    }
  }

  el.messages.appendChild(el.emptyState);
  el.emptyState.hidden = items.length > 0;
  scrollDown(true);
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

function renderConversationList() {
  el.conversations.replaceChildren();
  for (const conversation of state.conversations) {
    const row = document.createElement("button");
    row.className = "conv" + (conversation.id === state.active?.id ? " active" : "");
    row.type = "button";

    const title = document.createElement("span");
    title.className = "conv-title";
    title.textContent = conversation.title || "Untitled";
    title.title = conversation.workspace;

    const remove = document.createElement("span");
    remove.className = "conv-del";
    remove.textContent = "×";
    remove.title = "Delete conversation";
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      await del(`/api/conversations/${conversation.id}`).catch((e) => toast(e.message, true));
      if (state.active?.id === conversation.id) closeConversation();
      await loadConversations();
    });

    row.append(title, remove);
    row.addEventListener("click", () => openConversation(conversation.id));
    el.conversations.appendChild(row);
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
}

function resetLive() {
  state.liveText = null;
  state.liveReasoning = null;
  state.toolCards.clear();
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

  on("step", (data) => {
    // A fresh model turn: later text belongs in its own bubble.
    state.liveText = null;
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
    state.liveText = null;
    state.liveReasoning = null;
    const card = append(toolCard(data.name || "tool", data.arguments));
    state.toolCards.set(data.call_id, card);
    setBusy(true, `Running ${data.name}…`);
  });

  on("tool_finished", (data) => {
    const card = state.toolCards.get(data.call_id);
    if (card) finishToolCard(card, data.output, data.ok);
    setBusy(true);
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

  on("title", (data) => {
    if (state.active) state.active.title = data.title;
    el.title.textContent = data.title;
    loadConversations();
  });

  on("done", () => { setBusy(false); resetLive(); });
  on("cancelled", () => { setBusy(false); resetLive(); toast("Run cancelled."); });

  on("error", (data) => {
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

function showNextPermission() {
  if (state.currentPermission || !state.permissionQueue.length) return;
  const request = state.permissionQueue.shift();
  state.currentPermission = request;

  el.permKind.textContent =
    request.kind === "shell"
      ? "The agent wants to run a shell command."
      : "The agent wants to write to a file.";
  el.permDetail.textContent = request.detail;
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

  el.title.textContent = data.title || "Untitled";
  el.workspace.textContent = data.workspace;
  el.modeSelect.value = data.permission_mode;
  ensureModelOption(data.model);
  el.modelSelect.value = data.model;

  renderItems(data.items || []);
  renderConversationList();
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
  renderItems([]);
  setBusy(false);
}

async function sendMessage() {
  const text = el.prompt.value.trim();
  if (!text || !state.active || state.busy) return;

  el.prompt.value = "";
  el.prompt.style.height = "auto";
  el.emptyState.hidden = true;
  append(userBubble(text));
  resetLive();
  setBusy(true);

  try {
    await post(`/api/conversations/${state.active.id}/messages`, { text });
  } catch (error) {
    append(errorBubble(error.message));
    setBusy(false);
  }
}

/* -- Setup ---------------------------------------------------------------- */

function ensureModelOption(model) {
  if (!model || [...el.modelSelect.options].some((o) => o.value === model)) return;
  const option = document.createElement("option");
  option.value = option.textContent = model;
  el.modelSelect.appendChild(option);
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
}

async function loadModels() {
  try {
    const data = await get("/api/models");
    state.models = data.models || [];
  } catch (error) {
    toast(`Could not load models: ${error.message}`, true);
    state.models = [state.meta.default_model];
  }
  el.modelSelect.replaceChildren();
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = option.textContent = model;
    el.modelSelect.appendChild(option);
  }
}

function wireEvents() {
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

  el.stopRun.addEventListener("click", async () => {
    if (!state.active) return;
    await post(`/api/conversations/${state.active.id}/cancel`).catch(() => {});
  });

  el.modeSelect.addEventListener("change", async () => {
    if (!state.active) return;
    await patch(`/api/conversations/${state.active.id}`, { mode: el.modeSelect.value })
      .then(() => toast(`Permission mode: ${el.modeSelect.selectedOptions[0].textContent}`))
      .catch((error) => toast(error.message, true));
  });

  el.modelSelect.addEventListener("change", async () => {
    if (!state.active) return;
    await patch(`/api/conversations/${state.active.id}`, { model: el.modelSelect.value })
      .catch((error) => toast(error.message, true));
  });

  el.permOnce.addEventListener("click", () => replyPermission("once"));
  el.permSession.addEventListener("click", () => replyPermission("session"));
  el.permDeny.addEventListener("click", () => replyPermission("deny"));

  el.newChat.addEventListener("click", () => {
    el.newWorkspace.value = state.active?.workspace || state.meta.cwd;
    fillModes(el.newMode, state.meta.default_mode);
    el.newOverlay.hidden = false;
    el.newWorkspace.focus();
  });

  el.newCancel.addEventListener("click", () => { el.newOverlay.hidden = true; });

  el.newCreate.addEventListener("click", async () => {
    try {
      const conversation = await post("/api/conversations", {
        workspace: el.newWorkspace.value.trim(),
        mode: el.newMode.value,
        model: el.modelSelect.value || state.meta.default_model,
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
    if (!el.newOverlay.hidden) el.newOverlay.hidden = true;
  });
}

async function init() {
  state.meta = await get("/api/meta");
  el.toolCount.textContent = `${state.meta.tools.length} tools`;
  fillModes(el.modeSelect, state.meta.default_mode);
  await loadModels();
  wireEvents();
  await loadConversations();

  if (state.conversations.length) {
    await openConversation(state.conversations[0].id);
  } else {
    setBusy(false);
  }
}

init().catch((error) => toast(`Startup failed: ${error.message}`, true));
