"use strict";

const loginPanel = document.querySelector("#loginPanel");
const dashboard = document.querySelector("#dashboard");
const loginForm = document.querySelector("#loginForm");
const usernameInput = document.querySelector("#usernameInput");
const passwordInput = document.querySelector("#passwordInput");
const passwordConfirmationInput = document.querySelector("#passwordConfirmationInput");
const registrationPasscodeInput = document.querySelector("#registrationPasscodeInput");
const setupFields = document.querySelector("#setupFields");
const loginTitle = document.querySelector("#loginTitle");
const loginDescription = document.querySelector("#loginDescription");
const loginSubmitButton = document.querySelector("#loginSubmitButton");
const loginMessage = document.querySelector("#loginMessage");
const logoutButton = document.querySelector("#logoutButton");
const refreshButton = document.querySelector("#refreshButton");
const summaryText = document.querySelector("#summaryText");
const notice = document.querySelector("#notice");
const userList = document.querySelector("#userList");

let sessionToken = sessionStorage.getItem("riverbank.admin.session") || "";
let currentUserId = "";
let firstSetup = false;
const servicePrefix = window.location.pathname.replace(/\/admin\/?$/, "");

function escapeText(value) {
  return String(value ?? "");
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (sessionToken) headers.set("Authorization", `Bearer ${sessionToken}`);
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${servicePrefix}${path}`, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    const message = (await response.text()).trim() || `请求失败（${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function showNotice(message, error = false) {
  notice.textContent = message;
  notice.classList.toggle("error", error);
  notice.classList.toggle("hidden", !message);
}

function showLogin(message = "") {
  sessionToken = "";
  currentUserId = "";
  sessionStorage.removeItem("riverbank.admin.session");
  loginPanel.classList.remove("hidden");
  dashboard.classList.add("hidden");
  logoutButton.classList.add("hidden");
  loginMessage.textContent = message;
  passwordInput.value = "";
  passwordConfirmationInput.value = "";
  registrationPasscodeInput.value = "";
}

async function configureLogin() {
  try {
    const config = await api("/api/v1/auth/config");
    firstSetup = !config.configured;
    setupFields.classList.toggle("hidden", !firstSetup);
    usernameInput.value = firstSetup ? (config.initial_admin_username || "Geo") : usernameInput.value;
    usernameInput.readOnly = firstSetup;
    passwordInput.autocomplete = firstSetup ? "new-password" : "current-password";
    passwordConfirmationInput.required = firstSetup;
    registrationPasscodeInput.required = firstSetup;
    loginTitle.textContent = firstSetup ? "创建 Geo 管理员" : "管理员登录";
    loginDescription.textContent = firstSetup
      ? "设备尚未建立账户。设置管理员密码并输入注册通行码，旧聊天和任务会自动归属 Geo。"
      : "只有管理员账户可以读取或修改用户、会话、缓存和任务状态。";
    loginSubmitButton.textContent = firstSetup ? "创建并进入后台" : "安全登录";
  } catch (error) {
    loginMessage.textContent = error.message;
  }
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function metric(label, value) {
  const element = document.createElement("div");
  element.className = "metric";
  const strong = document.createElement("strong");
  strong.textContent = escapeText(value);
  const caption = document.createElement("p");
  caption.textContent = label;
  element.append(strong, caption);
  return element;
}

function actionButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function renderUser(user) {
  const card = document.createElement("article");
  card.className = "user-card";

  const identity = document.createElement("div");
  identity.className = "identity";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = String(user.display_name || user.username).slice(0, 1).toUpperCase();
  const copy = document.createElement("div");
  const name = document.createElement("h3");
  name.textContent = user.display_name || user.username;
  const username = document.createElement("p");
  username.textContent = `@${user.username}`;
  const badges = document.createElement("div");
  badges.className = "badges";
  const roleBadge = document.createElement("span");
  roleBadge.className = `badge ${user.role === "admin" ? "admin" : ""}`;
  roleBadge.textContent = user.role === "admin" ? "管理员" : "普通用户";
  badges.append(roleBadge);
  if (user.disabled) {
    const disabledBadge = document.createElement("span");
    disabledBadge.className = "badge disabled";
    disabledBadge.textContent = "已停用";
    badges.append(disabledBadge);
  }
  copy.append(name, username, badges);
  identity.append(avatar, copy);

  const metrics = document.createElement("div");
  metrics.className = "metrics";
  metrics.append(
    metric("对话", user.chat.conversations),
    metric("附件", `${user.chat.attachments} · ${formatBytes(user.chat.attachment_bytes)}`),
    metric("任务", ["queued", "running", "waiting_input", "completed", "failed", "cancelled"].reduce((total, key) => total + Number(user.tasks[key] || 0), 0)),
    metric("登录设备", user.active_sessions),
  );

  const actions = document.createElement("div");
  actions.className = "actions";
  const role = document.createElement("select");
  role.setAttribute("aria-label", `${user.username} 的权限`);
  for (const [value, label] of [["user", "普通用户"], ["admin", "管理员"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = value === user.role;
    role.append(option);
  }
  role.addEventListener("change", async () => {
    const label = role.value === "admin" ? "管理员" : "普通用户";
    if (!window.confirm(`确认将 ${user.username} 设置为${label}？`)) {
      role.value = user.role;
      return;
    }
    try {
      await api(`/api/v1/auth/users/${encodeURIComponent(user.id)}`, {
        method: "PATCH", body: JSON.stringify({ role: role.value }),
      });
      showNotice(`${user.username} 的权限已更新`);
      await loadUsers();
    } catch (error) {
      showNotice(error.message, true);
      role.value = user.role;
    }
  });
  actions.append(role);
  actions.append(actionButton(user.disabled ? "恢复账户" : "停用账户", "soft", async () => {
    if (user.id === currentUserId && !user.disabled) {
      showNotice("不能停用当前登录的管理员账户", true); return;
    }
    if (!window.confirm(`确认${user.disabled ? "恢复" : "停用"}账户 ${user.username}？`)) return;
    try {
      await api(`/api/v1/auth/users/${encodeURIComponent(user.id)}`, {
        method: "PATCH", body: JSON.stringify({ disabled: !user.disabled }),
      });
      showNotice(`${user.username} 的账户状态已更新`);
      await loadUsers();
    } catch (error) { showNotice(error.message, true); }
  }));
  actions.append(actionButton("退出所有设备", "soft", async () => {
    if (!window.confirm(`让 ${user.username} 的所有设备退出登录？`)) return;
    try {
      const result = await api(`/api/v1/admin/users/${encodeURIComponent(user.id)}/revoke-sessions`, { method: "POST" });
      showNotice(`已撤销 ${result.revoked_sessions} 个会话`);
      await loadUsers();
    } catch (error) { showNotice(error.message, true); }
  }));
  actions.append(actionButton("清理聊天缓存", "danger", async () => {
    if (!window.confirm(`将永久删除 ${user.username} 的聊天记录与上传附件，确认继续？`)) return;
    try {
      const result = await api(`/api/v1/admin/users/${encodeURIComponent(user.id)}/cache`, { method: "DELETE" });
      showNotice(`已清理 ${result.deleted_conversations} 个对话和 ${result.deleted_attachments} 个附件`);
      await loadUsers();
    } catch (error) { showNotice(error.message, true); }
  }));
  actions.append(actionButton("清理历史任务", "danger", async () => {
    if (!window.confirm(`将永久删除 ${user.username} 已结束的任务记录，确认继续？`)) return;
    try {
      const result = await api(`/api/v1/admin/users/${encodeURIComponent(user.id)}/tasks`, { method: "DELETE" });
      showNotice(`已清理 ${result.deleted} 项任务`);
      await loadUsers();
    } catch (error) { showNotice(error.message, true); }
  }));

  card.append(identity, metrics, actions);
  return card;
}

async function loadUsers() {
  const payload = await api("/api/v1/admin/users");
  userList.replaceChildren(...payload.users.map(renderUser));
  const enabled = payload.users.filter(user => !user.disabled).length;
  summaryText.textContent = `${payload.count} 个账户 · ${enabled} 个可用 · 数据按用户隔离`;
}

async function restoreSession() {
  if (!sessionToken) return showLogin();
  try {
    const me = await api("/api/v1/auth/me");
    if (me.user.role !== "admin") throw new Error("该账户不是管理员");
    currentUserId = me.user.id;
    loginPanel.classList.add("hidden");
    dashboard.classList.remove("hidden");
    logoutButton.classList.remove("hidden");
    await loadUsers();
  } catch (error) {
    showLogin(error.status === 401 ? "登录已过期，请重新登录" : error.message);
  }
}

loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  loginMessage.textContent = "正在验证…";
  try {
    if (firstSetup && passwordInput.value !== passwordConfirmationInput.value) {
      throw new Error("两次输入的密码不一致");
    }
    const result = await api(firstSetup ? "/api/v1/auth/register" : "/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: usernameInput.value.trim(),
        password: passwordInput.value,
        device_name: "Admin Console",
        registration_passcode: firstSetup ? registrationPasscodeInput.value : undefined,
      }),
    });
    if (result.user.role !== "admin") {
      sessionToken = result.token;
      await api("/api/v1/auth/logout", { method: "POST" });
      throw new Error("该账户没有管理员权限");
    }
    sessionToken = result.token;
    currentUserId = result.user.id;
    sessionStorage.setItem("riverbank.admin.session", sessionToken);
    passwordInput.value = "";
    firstSetup = false;
    await restoreSession();
  } catch (error) {
    sessionToken = "";
    loginMessage.textContent = error.status === 401 ? "用户名或密码不正确" : error.message;
  }
});

logoutButton.addEventListener("click", async () => {
  try { await api("/api/v1/auth/logout", { method: "POST" }); } catch (_) { /* local logout still applies */ }
  showLogin();
});
refreshButton.addEventListener("click", async () => {
  showNotice("");
  try { await loadUsers(); } catch (error) { showNotice(error.message, true); }
});

restoreSession();
configureLogin();
