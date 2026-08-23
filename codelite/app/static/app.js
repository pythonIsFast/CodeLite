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
  newOverlay: $("new-overlay"),
  newWorkspace: $("new-workspace"),
  browseWorkspace: $("browse-workspace"),
  newMode: $("new-mode"),
  newCreate: $("new-create"),
  newCancel: $("new-cancel"),
  settingsButton: $("settings-button"),
  settingsOverlay: $("settings-overlay"),
  settingsClose: $("settings-close"),
  settingsLogin: $("settings-login"),
  accountName: $("account-name"),
  accountDetail: $("account-detail"),
  settingsAuthStatus: $("settings-auth-status"),
  authOverlay: $("auth-overlay"),
  authLogin: $("auth-login"),
  authStatus: $("auth-status"),
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
  liveGroup: null,
  liveTodos: null,
  lastTurnTokens: null,
  toolCards: new Map(),
  permissionQueue: [],
  currentPermission: null,
  auth: null,
  appLoaded: false,
  authPoll: null,
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
 * written and what the turn cost.
 */
function attachFooter(bubble, { at, tokens } = {}) {
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

  if (Number.isFinite(tokens) && tokens > 0) {
    const cost = document.createElement("span");
    cost.className = "msg-tokens";
    cost.textContent = `${formatTokens(tokens)} tokens`;
    cost.title = `${tokens.toLocaleString()} tokens for this turn`;
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
    tokens: state.lastTurnTokens,
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

const TOOL_GROUP_ICON =
  `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">` +
  `<path d="M2 3.5A1.5 1.5 0 0 1 3.5 2h4l1.5 1.5H12.5A1.5 1.5 0 0 1 14 5v7a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12z"` +
  ` fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>`;

const CHEVRON_ICON =
  `<svg class="tool-group-chevron" viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">` +
  `<path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6"` +
  ` stroke-linecap="round" stroke-linejoin="round"/></svg>`;

/** Categorize a tool call for the group summary line ("Read 2 files, ran 1 command…"). */
function toolCategory(name) {
  if (name === "shell") return "shell";
  if (name === "grep" || name === "find_files") return "search";
  if (name === "write_file" || name === "edit_file") return "write";
  return "read";
}

const CATEGORY_LABEL = {
  read: ["Read", "file", "files"],
  write: ["Edited", "file", "files"],
  search: ["Searched", "location", "locations"],
  shell: ["Ran", "command", "commands"],
};

/** A collapsible group of one or more tool calls, rendered together like a single agent step. */
function toolGroup() {
  const group = document.createElement("div");
  group.className = "tool-group";

  const head = document.createElement("button");
  head.className = "tool-group-head";
  head.type = "button";
  head.innerHTML = `${TOOL_GROUP_ICON}<span class="tool-group-summary">Working…</span>${CHEVRON_ICON}`;
  head.addEventListener("click", () => group.classList.toggle("open"));

  const body = document.createElement("div");
  body.className = "tool-group-body";

  group.append(head, body);
  group._summary = head.querySelector(".tool-group-summary");
  group._body = body;
  group._counts = new Map();
  return group;
}

function refreshGroupSummary(group) {
  const parts = [];
  for (const [category, count] of group._counts) {
    const [verb, singular, plural] = CATEGORY_LABEL[category];
    parts.push(`${verb} ${count} ${count === 1 ? singular : plural}`);
  }
  group._summary.textContent = parts.join(", ") || "Working…";
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
      el.messages.appendChild(userBubble(textFromContent(item.content)));
      continue;
    }
    if (item.type === "message" || item.role === "assistant") {
      const text = textFromContent(item.content);
      if (text.trim()) {
        group = null;
        const bubble = el.messages.appendChild(assistantBubble(text));
        attachFooter(bubble, { at: entry.created_at, tokens: meta.tokens });
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
      if (!group) { group = toolGroup(); el.messages.appendChild(group); }
      const row = toolRow(group, item.name || "tool", item.arguments);
      rows.set(item.call_id || item.id, { group, row });
      continue;
    }
    if (item.type === "function_call_output") {
      const entry = rows.get(item.call_id);
      const output = typeof item.output === "string" ? item.output : JSON.stringify(item.output);
      if (entry) finishToolRow(entry.group, entry.row, output, !/^Error:|did not allow/.test(output));
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

function renderConversationList() {
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
      body.appendChild(row);
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

  on("step", (data) => {
    // A fresh model turn: later text/tool calls belong in their own bubble/group.
    finalizeLiveText();
    state.liveGroup = null;
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
    finalizeLiveText();
    state.liveReasoning = null;
    if (!state.liveGroup) state.liveGroup = append(toolGroup());
    const row = toolRow(state.liveGroup, data.name || "tool", data.arguments);
    state.toolCards.set(data.call_id, { group: state.liveGroup, row });
    scrollDown();
    setBusy(true, `Running ${data.name}…`);
  });

  on("tool_finished", (data) => {
    const entry = state.toolCards.get(data.call_id);
    if (entry) finishToolRow(entry.group, entry.row, data.output, data.ok);
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
    if (Number.isFinite(data.turn_tokens)) state.lastTurnTokens = data.turn_tokens;
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
    toast(`Compacted earlier context; kept the latest ${data.kept_items} items.`);
  });
  on("compaction_failed", (data) => {
    toast(`Could not compact context: ${data.message}`, true);
  });

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
  el.authLogin.addEventListener("click", startAuthLogin);
  el.settingsLogin.addEventListener("click", startAuthLogin);
  el.settingsButton.addEventListener("click", async () => {
    await refreshAuth().catch((error) => toast(error.message, true));
    el.settingsOverlay.hidden = false;
  });
  el.settingsClose.addEventListener("click", () => { el.settingsOverlay.hidden = true; });

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
    if (!el.settingsOverlay.hidden) el.settingsOverlay.hidden = true;
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
