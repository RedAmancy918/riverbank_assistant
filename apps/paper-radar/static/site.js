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
