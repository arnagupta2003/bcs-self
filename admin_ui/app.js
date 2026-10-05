const loginScreen = document.querySelector("#login-screen");
const consoleShell = document.querySelector("#console");
const toast = document.querySelector("#toast");
let csrfToken = "";
let state = null;
let originalState = "";
let clearSecrets = new Set();
let toastTimer;

const secretKey = (key) => /password|token|secret|webhook/i.test(key);
const stable = (value) => JSON.stringify(value);
const editableSnapshot = () => stable({
  server_config: state.server_config,
  mod_settings: state.mod_settings,
  roles: state.roles,
  blacklist: state.blacklist,
});

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3600);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set("Content-Type", "application/json");
  if (csrfToken && options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function setPath(target, path, value) {
  let current = target;
  for (const key of path.slice(0, -1)) current = current[key];
  current[path[path.length - 1]] = value;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function updateDirtyState() {
  const changed = state && (editableSnapshot() !== originalState || clearSecrets.size > 0);
  const invalid = document.querySelector(".invalid");
  document.querySelectorAll("[data-save]").forEach((button) => {
    button.disabled = !changed || Boolean(invalid);
  });
}

function updatePathFromControl(control, scope, path, kind) {
  const encodedPath = JSON.parse(control.dataset.path);
  let value;
  if (kind === "boolean") value = control.checked;
  else if (kind === "number") {
    value = control.value === "" ? null : Number(control.value);
    if (value !== null && !Number.isFinite(value)) throw new Error("Enter a valid number.");
  } else if (kind === "json") value = JSON.parse(control.value);
  else value = control.value;
  setPath(state[scope], encodedPath, value);
  if (scope === "mod_settings" && secretKey(String(encodedPath.at(-1))) && value !== "") {
    clearSecrets.delete(encodedPath.join("."));
  }
  control.classList.remove("invalid");
  control.removeAttribute("aria-invalid");
  updateDirtyState();
}

function bindControl(control, scope, path, kind, eventName = "change") {
  control.dataset.scope = scope;
  control.dataset.path = JSON.stringify(path);
  control.addEventListener(eventName, () => {
    try {
      updatePathFromControl(control, scope, path, kind);
      if (scope === "server_config" && path[0] === "party_name") {
        document.querySelector("#overview-name").textContent = control.value || "BombSquad server";
        document.querySelector("#sidebar-name").textContent = control.value || "Unnamed server";
      }
    } catch (error) {
      control.classList.add("invalid");
      control.setAttribute("aria-invalid", "true");
      showToast(error.message, true);
      updateDirtyState();
    }
  });
}

function fieldRow(label, help, control, className = "") {
  const row = element("div", `field-row ${className}`.trim());
  const info = element("div");
  info.append(element("label", "field-label", label));
  if (help) info.append(element("p", "field-help", help));
  const wrap = element("div", "field-control");
  wrap.append(control);
  row.append(info, wrap);
  return row;
}

function renderConfigField(field) {
  const value = state.server_config[field.key];
  let control;
  if (field.type === "boolean") {
    control = element("label", "toggle-control");
    const input = element("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    bindControl(input, "server_config", [field.key], "boolean");
    control.append(input, element("span", "", value ? "Enabled" : "Disabled"));
    input.addEventListener("change", () => { control.querySelector("span").textContent = input.checked ? "Enabled" : "Disabled"; });
  } else if (field.type === "select") {
    control = element("select");
    for (const optionValue of field.options || []) {
      const option = element("option", "", optionValue.toUpperCase());
      option.value = optionValue;
      control.append(option);
    }
    control.value = value ?? "ffa";
    bindControl(control, "server_config", [field.key], "text");
  } else if (field.type === "json") {
    control = element("textarea");
    control.value = JSON.stringify(value, null, 2);
    control.spellcheck = false;
    bindControl(control, "server_config", [field.key], "json");
  } else {
    control = element("input");
    control.type = field.type === "number" ? "number" : "text";
    if (field.type === "number") control.step = "any";
    control.value = value === null || value === undefined ? "" : String(value);
    if (field.nullable) control.placeholder = "Not set";
    bindControl(control, "server_config", [field.key], field.type, "input");
    control.addEventListener("blur", () => {
      try { updatePathFromControl(control, "server_config", [field.key], field.type); }
      catch (error) { control.classList.add("invalid"); control.setAttribute("aria-invalid", "true"); showToast(error.message, true); }
    });
  }
  const help = field.help || (field.type === "json" ? "Enter valid JSON. Arrays and objects are saved as structured settings." : "");
  return fieldRow(field.label, help, control);
}

function renderServerSettings() {
  const container = document.querySelector("#server-settings");
  container.replaceChildren();
  const groups = new Map();
  for (const field of state.server_schema) {
    if (!groups.has(field.group)) groups.set(field.group, []);
    groups.get(field.group).push(field);
  }
  for (const [name, fields] of groups) {
    const section = element("section", "settings-group");
    const header = element("div", "settings-group-head");
    header.append(element("h2", "", name), element("span", "", `${fields.length} SETTINGS`));
    const list = element("div", "settings-list");
    fields.forEach((field) => list.append(renderConfigField(field)));
    section.append(header, list);
    container.append(section);
  }
}

function modControl(key, value, path) {
  const row = element("div", "tree-field");
  row.append(element("label", "field-label", key.replaceAll("_", " ")));
  const area = element("div", "field-control");
  const isSecret = secretKey(key);
  let control;
  if (typeof value === "boolean") {
    control = element("label", "toggle-control");
    const input = element("input");
    input.type = "checkbox";
    input.checked = value;
    bindControl(input, "mod_settings", path, "boolean");
    control.append(input, element("span", "", value ? "Enabled" : "Disabled"));
    input.addEventListener("change", () => { control.querySelector("span").textContent = input.checked ? "Enabled" : "Disabled"; });
  } else if (typeof value === "number") {
    control = element("input");
    control.type = "number";
    control.step = Number.isInteger(value) ? "1" : "any";
    control.value = String(value);
    bindControl(control, "mod_settings", path, "number", "input");
  } else if (Array.isArray(value) || (value !== null && typeof value === "object")) {
    control = element("textarea");
    control.value = JSON.stringify(value, null, 2);
    control.spellcheck = false;
    bindControl(control, "mod_settings", path, "json");
  } else {
    control = element("input");
    control.type = isSecret ? "password" : "text";
    control.value = isSecret ? "" : value ?? "";
    if (isSecret) control.placeholder = state.secret_paths.includes(path.join(".")) ? "Saved; leave blank to keep" : "Not configured";
    bindControl(control, "mod_settings", path, "text", "input");
  }
  area.append(control);
  if (isSecret) {
    const actions = element("div", "secret-actions");
    const clear = element("button", "secret-action clear", "Clear saved value");
    const secretPath = path.join(".");
    clear.type = "button";
    clear.addEventListener("click", () => {
      if (clearSecrets.has(secretPath)) clearSecrets.delete(secretPath);
      else clearSecrets.add(secretPath);
      clear.textContent = clearSecrets.has(secretPath) ? "Will clear on save" : "Clear saved value";
      updateDirtyState();
    });
    actions.append(clear);
    area.append(actions);
  }
  row.append(area);
  return row;
}

function renderModTree(value, path = [], nested = false) {
  const content = element("div", nested ? "tree-nested-content" : "tree-content");
  for (const [key, item] of Object.entries(value || {})) {
    const childPath = [...path, key];
    if (item !== null && typeof item === "object" && !Array.isArray(item)) {
      const nested = element("details", "tree-nested");
      nested.append(element("summary", "", key.replaceAll("_", " ")), renderModTree(item, childPath, true));
      content.append(nested);
    } else content.append(modControl(key, item, childPath));
  }
  return content;
}

function renderModSettings() {
  const container = document.querySelector("#mod-settings");
  container.replaceChildren();
  for (const [key, value] of Object.entries(state.mod_settings || {})) {
    const group = element("details", "tree-group");
    if (container.childElementCount < 2) group.open = true;
    const summary = element("summary", "", key.replaceAll("_", " "));
    const count = element("span", "tree-count", `${Object.keys(value || {}).length || 1} ITEMS`);
    summary.append(count);
    let content;
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      content = renderModTree(value, [key]);
    } else {
      content = element("div", "tree-content");
      content.append(modControl(key, value, [key]));
    }
    group.append(summary, content);
    container.append(group);
  }
}

function bindJsonDocument(selector, stateKey) {
  const input = document.querySelector(selector);
  input.value = JSON.stringify(state[stateKey], null, 2);
  input.addEventListener("change", () => {
    try {
      const parsed = JSON.parse(input.value);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("Expected a JSON object.");
      state[stateKey] = parsed;
      input.classList.remove("invalid");
      input.removeAttribute("aria-invalid");
    } catch (error) {
      input.classList.add("invalid");
      input.setAttribute("aria-invalid", "true");
      showToast(error.message, true);
    }
    updateDirtyState();
  });
}

function updateStatus(status) {
  const badge = document.querySelector("#server-status");
  badge.classList.toggle("status-online", Boolean(status.running));
  badge.classList.toggle("status-offline", !status.running);
  badge.lastChild.textContent = status.running ? "Server running" : "Server offline";
  document.querySelector("#overview-status").textContent = status.running ? "Running" : "Stopped";
  document.querySelector("#overview-pid").textContent = status.running ? `Manager PID ${status.pid}` : "Start the server when ready";
}

function renderOverview() {
  const config = state.server_config;
  document.querySelector("#overview-name").textContent = config.party_name || "Unnamed server";
  document.querySelector("#sidebar-name").textContent = config.party_name || "Unnamed server";
  document.querySelector("#sidebar-port").textContent = `Game port ${config.port}`;
  document.querySelector("#overview-port").textContent = String(config.port);
  document.querySelector("#service-game-port").textContent = `UDP ${config.port}`;
  document.querySelector("#overview-session").textContent = config.playlist_code ? "SHARED" : (config.session_type || "custom").toUpperCase();
  document.querySelector("#overview-playlist").textContent = config.playlist_code ? `Shared playlist ${config.playlist_code}` : "Built-in or inline playlist";
  const quick = document.querySelector("#quick-config");
  quick.replaceChildren();
  for (const [label, value] of [
    ["SERVER NAME", config.party_name || "Unnamed server"],
    ["VISIBILITY", config.party_is_public ? "Public listing" : "Private / direct IP"],
    ["MAX PARTY SIZE", config.max_party_size],
    ["CLIENT AUTHENTICATION", config.authenticate_clients ? "Enabled" : "Disabled"],
  ]) {
    const row = element("div", "quick-row");
    row.append(element("span", "", label), element("strong", "", String(value)));
    quick.append(row);
  }
  updateStatus(state.status);
}

function renderState() {
  originalState = editableSnapshot();
  renderOverview();
  renderServerSettings();
  renderModSettings();
  bindJsonDocument("#roles-json", "roles");
  bindJsonDocument("#blacklist-json", "blacklist");
  updateDirtyState();
}

async function loadState() {
  state = await api("/api/state");
  clearSecrets.clear();
  renderState();
}

async function enterConsole() {
  const session = await api("/api/session");
  csrfToken = session.csrf_token;
  await loadState();
  loginScreen.hidden = true;
  consoleShell.hidden = false;
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#login-error");
  error.textContent = "";
  const passwordInput = document.querySelector("#login-password");
  try {
    const session = await api("/api/login", { method: "POST", body: JSON.stringify({ password: passwordInput.value }) });
    csrfToken = session.csrf_token;
    passwordInput.value = "";
    await enterConsole();
  } catch (failure) { error.textContent = failure.message; }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (_) { /* Session may already be expired. */ }
  csrfToken = "";
  consoleShell.hidden = true;
  loginScreen.hidden = false;
});

function selectPage(page) {
  document.querySelectorAll("[data-view]").forEach((view) => { view.hidden = view.dataset.view !== page; });
  document.querySelectorAll("[data-page]").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  const active = document.querySelector(`[data-page="${page}"]`);
  document.querySelector("#current-section").textContent = active ? active.textContent.trim().replace(/^\d+/, "").trim() : "Overview";
}

document.querySelectorAll("[data-page], [data-open-page]").forEach((button) => {
  button.addEventListener("click", () => selectPage(button.dataset.page || button.dataset.openPage));
});

async function saveAll() {
  if (document.querySelector(".invalid")) return showToast("Correct invalid fields before saving.", true);
  try {
    const response = await api("/api/state", {
      method: "PUT",
      body: JSON.stringify({ ...state, clear_secrets: [...clearSecrets] }),
    });
    state.status = { running: false, pid: null };
    clearSecrets.clear();
    originalState = editableSnapshot();
    updateDirtyState();
    showToast(response.restarting ? "Settings saved. Server restart requested." : "Settings saved.");
    window.setTimeout(async () => {
      try { await loadState(); } catch (error) { showToast(error.message, true); }
    }, 1500);
  } catch (error) { showToast(error.message, true); }
}

document.querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", saveAll));
document.querySelectorAll("[data-server-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    const action = button.dataset.serverAction;
    try {
      await api(`/api/server/${action}`, { method: "POST", body: "{}" });
      showToast(`Server ${action} requested.`);
      window.setTimeout(refreshStatus, 600);
    } catch (error) { showToast(error.message, true); }
  });
});

async function refreshStatus() {
  try {
    const current = await api("/api/status");
    state.status = current;
    updateStatus(current);
  } catch (error) {
    if (error.message.includes("Authentication")) {
      consoleShell.hidden = true;
      loginScreen.hidden = false;
    }
  }
}

api("/api/session").then(async (session) => {
  csrfToken = session.csrf_token;
  await enterConsole();
}).catch(() => {});
window.setInterval(() => { if (!consoleShell.hidden) refreshStatus(); }, 5000);