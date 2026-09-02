(() => {
  const bar = document.querySelector(".reading-progress span");
  if (bar) {
    const updateProgress = () => {
      const scrollable = document.documentElement.scrollHeight - window.innerHeight;
      const ratio = scrollable > 0 ? window.scrollY / scrollable : 0;
      bar.style.transform = `scaleX(${Math.min(1, Math.max(0, ratio))})`;
    };
    updateProgress();
    window.addEventListener("scroll", updateProgress, { passive: true });
  }

  const toggle = document.querySelector("#special-focus-toggle");
  const consolePanel = document.querySelector("#special-focus-console");
  const form = document.querySelector("#special-focus-form");
  const description = document.querySelector("#special-focus-description");
  const status = document.querySelector("#special-focus-status");
  const cancel = document.querySelector("#special-focus-cancel");
  const submit = document.querySelector("#special-focus-submit");
  const submitLabel = document.querySelector("#special-focus-submit-label");
  const pendingBanner = document.querySelector("#special-focus-pending-banner");
  const pendingCount = document.querySelector("#special-focus-pending-count");
  const pendingList = document.querySelector("#special-focus-pending-list");
  if (!toggle || !consolePanel || !form || !description || !status || !cancel || !submit || !submitLabel || !pendingBanner || !pendingCount || !pendingList) return;

  let pendingRequests = [];
  let editingRequestId = null;

  const setOpen = (open) => {
    toggle.setAttribute("aria-expanded", String(open));
    consolePanel.setAttribute("aria-hidden", String(!open));
    consolePanel.classList.toggle("is-open", open);
    if (open) window.setTimeout(() => description.focus({ preventScroll: true }), 360);
  };

  const setStatus = (message, kind = "") => {
    status.textContent = message;
    status.classList.toggle("is-success", kind === "success");
    status.classList.toggle("is-error", kind === "error");
  };

  const resetEditor = () => {
    editingRequestId = null;
    description.value = "";
    submitLabel.textContent = "加入明日检索";
  };

  const beginEdit = (request) => {
    editingRequestId = request.id;
    description.value = request.description;
    submitLabel.textContent = "保存修改";
    setOpen(true);
    setStatus(`正在编辑 ${request.target_date} 08:00 的焦点描述。`);
  };

  const renderPending = (requests) => {
    pendingRequests = Array.isArray(requests) ? requests.filter(Boolean) : [];
    const hasPending = pendingRequests.length > 0;
    toggle.classList.toggle("has-pending", hasPending);
    toggle.setAttribute("aria-pressed", String(hasPending));
    cancel.hidden = !hasPending;
    pendingBanner.hidden = !hasPending;
    pendingCount.textContent = hasPending ? `${pendingRequests.length} 条待执行` : "待执行";
    pendingList.replaceChildren();
    pendingRequests.forEach((request) => {
      const item = document.createElement("li");
      const edit = document.createElement("button");
      const target = document.createElement("time");
      const prompt = document.createElement("span");
      const editHint = document.createElement("span");
      edit.type = "button";
      edit.className = "focus-pending-item";
      edit.setAttribute("aria-label", `编辑待执行焦点：${request.description}`);
      target.dateTime = request.target_date;
      target.textContent = `${request.target_date} 08:00`;
      prompt.className = "focus-pending-description";
      prompt.textContent = request.description;
      editHint.className = "focus-pending-edit";
      editHint.textContent = "编辑 →";
      edit.append(target, prompt, editHint);
      edit.addEventListener("click", () => beginEdit(request));
      item.append(edit);
      pendingList.append(item);
    });
    if (editingRequestId && !pendingRequests.some((request) => request.id === editingRequestId)) {
      resetEditor();
    }
    if (hasPending) {
      setStatus(
        `已安排 ${pendingRequests.length} 条焦点内容；关闭开关会全部清空。`,
        "success",
      );
    } else {
      setStatus("尚未安排明日焦点。输入内容只会在下一次 08:00 日报中生效一次。");
    }
  };

  const clearPending = async () => {
    toggle.disabled = true;
    cancel.disabled = true;
    setStatus("正在关闭并清空全部未消费内容…");
    try {
      const response = await fetch("/api/special-focus", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clear_all: true }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "清空失败");
      resetEditor();
      renderPending(payload.pending_requests || []);
      setOpen(false);
    } catch (error) {
      setStatus(`清空失败：${error.message}`, "error");
    } finally {
      toggle.disabled = false;
      cancel.disabled = false;
    }
  };

  toggle.addEventListener("click", () => {
    if (pendingRequests.length) {
      clearPending();
      return;
    }
    const open = toggle.getAttribute("aria-expanded") !== "true";
    setOpen(open);
    if (!open) resetEditor();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = description.value.trim();
    if (value.length < 2) {
      setStatus("请至少输入 2 个字符。", "error");
      return;
    }
    submit.disabled = true;
    const editing = Boolean(editingRequestId);
    setStatus(editing ? "正在保存修改…" : "正在加入明日检索…");
    try {
      const response = await fetch("/api/special-focus", {
        method: editing ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description: value,
          ...(editing ? { id: editingRequestId } : {}),
        }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "提交失败");
      resetEditor();
      renderPending(payload.pending_requests || [payload.pending || payload.request]);
      setOpen(false);
    } catch (error) {
      setStatus(`提交失败：${error.message}`, "error");
    } finally {
      submit.disabled = false;
    }
  });

  cancel.addEventListener("click", clearPending);

  fetch("/api/special-focus", { cache: "no-store" })
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "状态读取失败");
      renderPending(payload.pending_requests || (payload.pending ? [payload.pending] : []));
    })
    .catch((error) => setStatus(`暂时无法读取安排状态：${error.message}`, "error"));
})();

(() => {
  const panels = [...document.querySelectorAll("[data-paper-chat]")];
  if (!panels.length) return;

  const pollTimers = new Map();

  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, { cache: "no-store", ...options });
    const text = await response.text();
    let payload = {};
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_error) {
      payload = {};
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || text || `请求失败（${response.status}）`);
    }
    return payload;
  };

  const setPanelStatus = (panel, text, kind = "") => {
    const status = panel.querySelector("[data-paper-chat-status]");
    status.textContent = text;
    status.classList.toggle("is-error", kind === "error");
    status.classList.toggle("is-ready", kind === "ready");
  };

  const setComposerEnabled = (panel, enabled) => {
    const input = panel.querySelector("[data-paper-chat-input]");
    const send = panel.querySelector("[data-paper-chat-send]");
    input.disabled = !enabled;
    send.disabled = !enabled;
  };

  const messageContent = (message) => {
    if (message.state === "failed") return message.error || "回答失败，请稍后重试。";
    if (message.state === "cancelled") return "本次回答已停止。";
    if (message.role === "assistant" && ["queued", "running"].includes(message.state) && !message.content) {
      return "正在查阅当天阅读缓存…";
    }
    return message.content || "";
  };

  const renderMessages = (panel, messages) => {
    const container = panel.querySelector("[data-paper-chat-messages]");
    container.replaceChildren();
    if (!messages.length) {
      const empty = document.createElement("p");
      empty.className = "paper-chat-empty";
      empty.textContent = "可以询问方法细节、实验设置、消融结果或局限性。";
      container.append(empty);
      return;
    }
    messages.forEach((message) => {
      const row = document.createElement("div");
      const label = document.createElement("span");
      const bubble = document.createElement("p");
      const role = message.role === "user" ? "user" : "assistant";
      row.className = `paper-chat-message ${role}`;
      if (message.state === "failed") row.classList.add("is-error");
      if (["queued", "running"].includes(message.state)) row.classList.add("is-pending");
      label.textContent = role === "user" ? "你" : "具身智讯";
      bubble.textContent = messageContent(message);
      row.append(label, bubble);
      container.append(row);
    });
    container.scrollTop = container.scrollHeight;
  };

  const clearPoll = (panel) => {
    const timer = pollTimers.get(panel);
    if (timer) window.clearTimeout(timer);
    pollTimers.delete(panel);
  };

  const loadHistory = async (panel, { silent = false } = {}) => {
    const paperId = panel.dataset.paperId;
    if (!paperId) return;
    if (!silent) setPanelStatus(panel, "正在读取已保留的对话…");
    try {
      const payload = await requestJson(`/api/paper-chat/${encodeURIComponent(paperId)}`);
      const messages = Array.isArray(payload.messages) ? payload.messages : [];
      const responding = messages.some(
        (item) => item.role === "assistant" && ["queued", "running"].includes(item.state),
      );
      renderMessages(panel, messages);
      panel.dataset.loaded = "true";
      if (!payload.knowledge_available) {
        setComposerEnabled(panel, false);
        setPanelStatus(panel, "历史对话已保留；当前知识库已更新到其他日报。", "error");
        clearPoll(panel);
        return;
      }
      setComposerEnabled(panel, !responding);
      const sourceReady = payload.paper?.source_state === "arxiv_html";
      setPanelStatus(
        panel,
        responding ? "正在生成回答…" : sourceReady ? "当天全文缓存已就绪" : "当天阅读笔记已就绪",
        responding ? "" : "ready",
      );
      clearPoll(panel);
      if (responding && panel.closest("details")?.open) {
        pollTimers.set(panel, window.setTimeout(() => loadHistory(panel, { silent: true }), 700));
      }
    } catch (error) {
      setComposerEnabled(panel, false);
      setPanelStatus(panel, `暂时无法读取对话：${error.message}`, "error");
      clearPoll(panel);
    }
  };

  panels.forEach((panel) => {
    const details = panel.closest("details");
    const form = panel.querySelector("[data-paper-chat-form]");
    const input = panel.querySelector("[data-paper-chat-input]");
    details?.addEventListener("toggle", () => {
      if (details.open) loadHistory(panel, { silent: panel.dataset.loaded === "true" });
      else clearPoll(panel);
    });
    input.addEventListener("keydown", (event) => {
      if (
        event.key !== "Enter" ||
        event.shiftKey ||
        event.isComposing ||
        event.keyCode === 229
      ) return;
      event.preventDefault();
      if (!input.disabled && input.value.trim()) form.requestSubmit();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const content = input.value.trim();
      if (!content) return;
      setComposerEnabled(panel, false);
      setPanelStatus(panel, "正在提交问题…");
      try {
        await requestJson(`/api/paper-chat/${encodeURIComponent(panel.dataset.paperId)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        });
        input.value = "";
        await loadHistory(panel, { silent: true });
      } catch (error) {
        setComposerEnabled(panel, true);
        setPanelStatus(panel, `提交失败：${error.message}`, "error");
      }
    });
  });
})();
