const state = {
    mode: "quick",
    outputStyle: "concise",
    outputStructure: "结论,变更,验证,限制,下一步",
    result: "",
    context: "",
    sessionId: "",
    source: "",
    sendMode: "queue",
    sessions: [],
    sessionsLoading: false,
    busy: false,
};
const $ = (selector) => document.querySelector(selector);
const draftEl = $("#draft");
const resultEl = $("#result");
const contextPreview = $("#context-preview");
const contextMeta = $("#context-meta");
const polishButton = $("#polish-button");
const sendButton = $("#send-button");
const copyButton = $("#copy-button");
const restoreButton = $("#restore-button");
const repolishButton = $("#repolish-button");
const loadingOverlay = $("#loading-overlay");
const emptyState = $("#empty-state");
const statusBadge = $("#status-badge");
const bindingSummary = $("#binding-summary");
const bindingNote = $("#binding-note");
const sessionModal = $("#session-modal");
const sessionList = $("#session-list");
const sessionFilter = $("#session-filter");
const settingsModal = $("#settings-modal");
const settingsBody = $("#settings-body");
const toast = $("#toast");
let toastTimer = null;

const SOURCE_LABELS = { codex: "Codex", dsh: "DSH" };
const BRIDGE_FIELD_LABELS = {
    home: "主目录",
    sessions_dir: "会话目录",
    workspace_storage: "会话索引",
    bridge_url: "回写桥地址",
    token_file: "Token 文件",
};
const BRIDGE_DEFAULTS = {
    codex: { home: "~/.codex" },
    dsh: {
        home: "~/.dsh",
        sessions_dir: "~/.dsh/sessions",
        workspace_storage: "~/.dsh/storages/workspace.json",
        bridge_url: "http://127.0.0.1:8766",
        token_file: "~/.dsh/prompt-sidecar-bridge.token",
    },
};

function showToast(message) {
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 1800);
}

function setBusy(busy) {
    state.busy = busy;
    polishButton.disabled = busy;
    repolishButton.disabled = busy || !state.result;
    sendButton.disabled = busy || !state.result || !state.sessionId;
    loadingOverlay.classList.toggle("hidden", !busy);
    statusBadge.className = "status-badge" + (busy ? " loading" : state.result ? " ready" : "");
    statusBadge.textContent = busy ? "润色中" : state.result ? "已生成" : "等待润色";
}

function setResult(text) {
    state.result = text;
    resultEl.value = text;
    emptyState.classList.toggle("hidden", Boolean(text));
    copyButton.disabled = !text;
    sendButton.disabled = !text || !state.sessionId || state.busy;
    restoreButton.disabled = !text;
    repolishButton.disabled = !text || state.busy;
    if (text) {
        statusBadge.className = "status-badge ready";
        statusBadge.textContent = "已生成";
    }
}

function shortId(sessionId) {
    return sessionId ? sessionId.replace(/^session-/, "").slice(0, 8) : "未绑定";
}

function sourceBadge(source) {
    const label = SOURCE_LABELS[source] || source || "未知";
    return `<span class="source-badge ${source}">${label}</span>`;
}

function updateBindingSummary(title, sessionId, source) {
    const label = SOURCE_LABELS[source] || source;
    const name = title || sessionId || "未绑定";
    bindingSummary.innerHTML = sessionId
        ? `${sourceBadge(source)} <strong>${escapeHtml(name)}</strong> <span class="muted">· ${shortId(sessionId)}</span>`
        : "未绑定";
    void label;
}

function updateSendModeAvailability() {
    const steerButton = document.querySelector('[data-send-mode="steer"]');
    const queueButton = document.querySelector('[data-send-mode="queue"]');
    const steerSupported = state.source !== "codex";
    steerButton.disabled = !steerSupported;
    steerButton.title = steerSupported ? "打断当前轮次并插入" : "Codex 会话只支持排队发送";
    if (!steerSupported) {
        state.sendMode = "queue";
        queueButton.classList.add("active");
        steerButton.classList.remove("active");
    }
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function cleanSessionId(value) {
    const normalized = String(value || "").trim();
    return ["", "undefined", "null"].includes(normalized) ? "" : normalized;
}

function renderSessionList() {
    const keyword = sessionFilter.value.trim().toLowerCase();
    const sessions = state.sessions.filter((session) => {
        if (!keyword) return true;
        const haystack = `${session.title || ""} ${session.cwd || ""} ${session.session_id}`.toLowerCase();
        return haystack.includes(keyword);
    });
    sessionList.innerHTML = "";
    const groups = new Map();
    sessions.forEach((session) => {
        if (!groups.has(session.source)) groups.set(session.source, []);
        groups.get(session.source).push(session);
    });
    const sources = [...groups.keys()];
    if (!sources.length) {
        const empty = document.createElement("p");
        empty.className = "session-empty muted";
        empty.textContent = state.sessions.length
            ? "没有匹配的会话"
            : state.sessionsLoading
              ? "正在加载会话..."
              : "未发现会话";
        sessionList.appendChild(empty);
    }
    sources.forEach((source) => {
        const items = groups.get(source);
        const head = document.createElement("div");
        head.className = "session-group-head";
        head.innerHTML = `${sourceBadge(source)}<strong>${escapeHtml(SOURCE_LABELS[source] || source)}</strong><span class="muted">${items.length} 个</span>`;
        sessionList.appendChild(head);
        items.forEach((session) => {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "session-item" + (session.session_id === state.sessionId && session.source === state.source ? " active" : "");
            const title = session.title || session.cwd || "未命名会话";
            item.innerHTML = `
                <span class="session-item-head"><strong>${escapeHtml(title.slice(0, 60))}</strong></span>
                <span class="session-item-meta">${escapeHtml(session.cwd || "")} · ${shortId(session.session_id)} · ${escapeHtml(session.updated_at || "")}</span>`;
            item.addEventListener("click", () => {
                closeSessionModal();
                bindSession(session.source, session.session_id);
            });
            sessionList.appendChild(item);
        });
    });
    const total = state.sessions.length;
    const loadingSuffix = state.sessionsLoading ? " · 正在加载" : "";
    $("#session-count").textContent = keyword
        ? `${sessions.length} / ${total} 个会话${loadingSuffix}`
        : total
          ? `${total} 个会话${loadingSuffix}`
          : state.sessionsLoading
            ? "正在加载会话..."
            : "未发现会话";
}

function openSessionModal() {
    renderSessionList();
    sessionModal.classList.remove("hidden");
    sessionFilter.focus();
}

function closeSessionModal() {
    sessionModal.classList.add("hidden");
    sessionFilter.value = "";
}

function renderSettings(bridges) {
    settingsBody.innerHTML = "";
    const sources = Object.keys(bridges);
    if (!sources.length) {
        settingsBody.innerHTML = '<p class="session-empty muted">暂无桥接配置</p>';
        return;
    }
    sources.forEach((source) => {
        const bridge = bridges[source] || {};
        const section = document.createElement("div");
        section.className = "settings-section";
        section.dataset.source = source;
        const defaults = BRIDGE_DEFAULTS[source] || {};
        const enabledHtml = `
            <label class="settings-enabled">
                <input type="checkbox" data-field="enabled" ${bridge.enabled === false ? "" : "checked"}>
                <span>启用</span>
            </label>`;
        const fieldsHtml = Object.keys(BRIDGE_FIELD_LABELS)
            .filter((field) => field in bridge || field in defaults)
            .map((field) => `
                <label class="settings-field">
                    <span>${BRIDGE_FIELD_LABELS[field]}</span>
                    <input type="text" data-field="${field}" value="${escapeHtml(bridge[field] || "")}"
                        placeholder="${escapeHtml(defaults[field] || "")}">
                </label>`)
            .join("");
        section.innerHTML = `
            <div class="settings-section-head">
                ${sourceBadge(source)}<strong>${escapeHtml(SOURCE_LABELS[source] || source)}</strong>
                ${enabledHtml}
            </div>
            ${fieldsHtml}`;
        settingsBody.appendChild(section);
    });
}

async function openSettingsModal() {
    settingsModal.classList.remove("hidden");
    settingsBody.innerHTML = '<p class="session-empty muted">正在读取配置...</p>';
    try {
        const data = await (await fetch("/api/config", { cache: "no-store" })).json();
        renderSettings(data.bridges || {});
    } catch {
        settingsBody.innerHTML = '<p class="session-empty muted">读取配置失败，请检查本地服务。</p>';
    }
}

function closeSettingsModal() {
    settingsModal.classList.add("hidden");
}

async function saveSettings() {
    const bridges = {};
    settingsBody.querySelectorAll(".settings-section").forEach((section) => {
        const source = section.dataset.source;
        const entry = {};
        const enabled = section.querySelector('[data-field="enabled"]');
        entry.enabled = Boolean(enabled && enabled.checked);
        section.querySelectorAll('input[type="text"][data-field]').forEach((input) => {
            const value = input.value.trim();
            if (value) entry[input.dataset.field] = value;
        });
        bridges[source] = entry;
    });
    if (!Object.keys(bridges).length) {
        showToast("没有可保存的桥接配置");
        return;
    }
    try {
        const response = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bridges }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "保存失败");
        closeSettingsModal();
        showToast("桥接设置已保存并生效");
        await loadSessions();
    } catch (error) {
        showToast(error.message || "保存失败");
    }
}

function mergeSessionsForSource(source, sessions) {
    const others = state.sessions.filter((session) => session.source !== source);
    state.sessions = [...others, ...sessions].sort(
        (left, right) => Number(right.recency_at || 0) - Number(left.recency_at || 0),
    );
}

async function refreshSessionsBySource() {
    await Promise.all(["codex", "dsh"].map(async (source) => {
        try {
            const response = await fetch(
                `/api/sessions?source=${encodeURIComponent(source)}`,
                { cache: "no-store" },
            );
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || "加载会话失败");
            mergeSessionsForSource(source, data.sessions || []);
        } catch {
            // Keep sessions already loaded from the other source.
        }
        renderSessionList();
    }));
}

let sessionsLoadPromise = null;

async function loadSessions() {
    if (sessionsLoadPromise) return sessionsLoadPromise;
    state.sessionsLoading = true;
    renderSessionList();
    sessionsLoadPromise = refreshSessionsBySource().finally(() => {
        state.sessionsLoading = false;
        sessionsLoadPromise = null;
        renderSessionList();
    });
    return sessionsLoadPromise;
}

async function loadContext(sessionId = null, source = null) {
    const requestedSessionId = cleanSessionId(sessionId);
    const wantedSource = source || state.source || "";
    const previousSessionId = state.sessionId;
    contextMeta.textContent = "正在读取...";
    contextPreview.textContent = "正在读取当前会话...";
    const params = new URLSearchParams();
    if (wantedSource) params.set("source", wantedSource);
    if (requestedSessionId) params.set("session_id", requestedSessionId);
    const suffix = params.size ? `?${params.toString()}` : "";
    try {
        const data = await (await fetch(`/api/context${suffix}`, { cache: "no-store" })).json();
        state.context = data.context || "";
        state.source = data.source || wantedSource || "codex";
        const nextSessionId = cleanSessionId(data.session_id) || requestedSessionId || "";
        if (previousSessionId && nextSessionId && previousSessionId !== nextSessionId) {
            draftEl.value = "";
            $("#draft-count").textContent = "0 字";
            setResult("");
            statusBadge.className = "status-badge";
            statusBadge.textContent = "等待润色";
        }
        state.sessionId = nextSessionId;
        sendButton.disabled = !state.result || !state.sessionId;
        updateSendModeAvailability();
        updateBindingSummary(data.session_title || data.cwd, state.sessionId, state.source);
        if (data.requires_binding) {
            contextMeta.textContent = "需要选择会话";
            contextPreview.textContent = "Sidecar 无法明确判断当前归属会话，请打开会话列表选择。";
            bindingNote.textContent = "未自动绑定；请选择一个会话，不会使用最新文件猜测。";
            await loadSessions();
            openSessionModal();
            history.replaceState(null, "", window.location.pathname);
            return;
        }
        if (!data.available) {
            contextMeta.textContent = "未发现会话记录";
            contextPreview.textContent = data.error || "没有可用上下文";
            bindingNote.textContent = data.error || "上下文不可用";
            return;
        }
        const methodLabels = {
            "url-explicit": "URL 显式绑定",
            "right-panel-route": "右侧面板自动绑定",
            "path-explicit": "路径绑定",
            "dsh-file-log": "DSH 会话日志绑定",
        };
        const method = methodLabels[data.binding_method] || data.binding_method;
        contextMeta.textContent = `${method} · ${data.message_count} 条消息 · ${data.char_count} 字`;
        contextPreview.textContent = data.context;
        const title = data.session_title || data.cwd || "未命名会话";
        bindingNote.textContent = `${SOURCE_LABELS[state.source] || state.source} · ${title} · ${state.sessionId}`;
        const url = new URL(window.location.href);
        url.searchParams.set("source", state.source);
        url.searchParams.set("session_id", state.sessionId);
        history.replaceState(null, "", url);
        await loadSessions();
    } catch (error) {
        contextMeta.textContent = "读取失败";
        contextPreview.textContent = String(error);
        bindingNote.textContent = "读取失败，请检查本地服务。";
    }
}

function bindSession(source, sessionId) {
    state.source = source;
    state.sessionId = sessionId;
    loadContext(sessionId, source);
}

async function loadHealth() {
    try {
        const data = await (await fetch("/api/health", { cache: "no-store" })).json();
        $("#model-label").textContent = data.model || "本地模型";
    } catch {
        $("#model-label").textContent = "模型不可用";
    }
}

async function polish() {
    const draft = draftEl.value.trim();
    if (!draft || state.busy) {
        if (!draft) {
            showToast("先写一段草稿");
            draftEl.focus();
        }
        return;
    }
    setBusy(true);
    emptyState.classList.add("hidden");
    try {
        const response = await fetch("/api/polish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                draft,
                mode: state.mode,
                use_context: $("#use-context").checked,
                context: state.context,
                session_id: state.sessionId,
                source: state.source,
                output_style: state.outputStyle,
                output_structure: state.outputStructure,
            }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "润色失败");
        setResult(data.polished);
        const autoCopy = $("#auto-copy").checked;
        const copied = autoCopy ? await copyResult({ silent: true }) : false;
        let message = `润色完成 · ${data.elapsed_ms} ms`;
        if (autoCopy) {
            message += copied ? " · 已自动复制" : " · 自动复制失败，请点击“复制结果”";
        }
        showToast(message);
    } catch (error) {
        emptyState.classList.remove("hidden");
        statusBadge.textContent = "失败";
        showToast(error.message || "润色失败");
    } finally {
        setBusy(false);
    }
}

async function sendToCurrentSession() {
    if (!state.result || !state.sessionId) {
        showToast(state.sessionId ? "先生成润色结果" : "请先绑定会话");
        return;
    }
    sendButton.disabled = true;
    const previousStatus = statusBadge.textContent;
    statusBadge.className = "status-badge loading";
    statusBadge.textContent = "发送中";
    try {
        const response = await fetch("/api/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                session_id: state.sessionId,
                message: state.result,
                source: state.source,
                mode: state.sendMode,
            }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "发送失败");
        statusBadge.className = "status-badge ready";
        statusBadge.textContent = "已发送";
        const modeLabel = data.mode === "steer" ? "已插入当前轮" : "已排队";
        showToast(
            data.queue_id
                ? `已发送到当前会话 · ${data.queue_id}`
                : `已发送到当前会话 · ${modeLabel}`,
        );
    } catch (error) {
        statusBadge.textContent = previousStatus;
        showToast(error.message || "发送失败");
    } finally {
        sendButton.disabled = !state.result || !state.sessionId || state.busy;
    }
}


async function copyResult({ silent = false } = {}) {
    if (!state.result) return false;
    let copied = false;
    try {
        await navigator.clipboard.writeText(state.result);
        copied = true;
    } catch {
        resultEl.focus();
        resultEl.select();
        copied = document.execCommand("copy");
    }
    if (!silent) {
        showToast(copied ? "结果已复制，可粘贴回 Composer" : "复制失败，请手动选择结果");
    }
    return copied;
}

function restoreOriginal() {
    if (!draftEl.value) return;
    setResult(draftEl.value);
    showToast("已恢复为原始草稿");
}

polishButton.addEventListener("click", polish);
repolishButton.addEventListener("click", polish);
sendButton.addEventListener("click", sendToCurrentSession);
copyButton.addEventListener("click", () => copyResult());
restoreButton.addEventListener("click", restoreOriginal);
$("#refresh-context").addEventListener("click", () => {
    const url = new URL(window.location.href);
    url.searchParams.delete("session_id");
    history.replaceState(null, "", url);
    loadContext(null, state.source);
});
$("#open-session-modal").addEventListener("click", openSessionModal);
$("#close-session-modal").addEventListener("click", closeSessionModal);
sessionModal.addEventListener("click", (event) => {
    if (event.target === sessionModal) closeSessionModal();
});
sessionFilter.addEventListener("input", renderSessionList);
$("#open-settings-modal").addEventListener("click", openSettingsModal);
$("#close-settings-modal").addEventListener("click", closeSettingsModal);
$("#settings-cancel").addEventListener("click", closeSettingsModal);
$("#settings-save").addEventListener("click", saveSettings);
settingsModal.addEventListener("click", (event) => {
    if (event.target === settingsModal) closeSettingsModal();
});
$("#modal-refresh").addEventListener("click", async () => {
    await loadSessions();
    showToast("会话列表已刷新");
});
$("#refresh-sessions").addEventListener("click", async () => {
    await loadSessions();
    showToast("会话列表已刷新");
});
window.addEventListener("focus", loadSessions);
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadSessions();
});
$("#sample-button").addEventListener("click", () => {
    draftEl.value = "我觉得现在这个插件方向不太对，帮我把它改成独立侧边面板，既能结合当前对话上下文，又不要把润色流程塞进主会话。";
    draftEl.dispatchEvent(new Event("input"));
    draftEl.focus();
});
$("#output-style").addEventListener("change", (event) => {
    state.outputStyle = event.target.value;
});
$("#output-structure").addEventListener("input", (event) => {
    state.outputStructure = event.target.value;
});
draftEl.addEventListener("input", () => {
    $("#draft-count").textContent = `${draftEl.value.length} 字`;
});
draftEl.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        polish();
    }
});
document.querySelectorAll(".mode").forEach((button) => {
    button.addEventListener("click", () => {
        document.querySelectorAll(".mode").forEach((item) => {
            item.classList.toggle("active", item === button);
        });
        state.mode = button.dataset.mode;
    });
});
document.querySelectorAll(".send-mode").forEach((button) => {
    button.addEventListener("click", () => {
        if (button.disabled) return;
        document.querySelectorAll(".send-mode").forEach((item) => {
            item.classList.toggle("active", item === button);
        });
        state.sendMode = button.dataset.sendMode;
    });
});
const urlParams = new URLSearchParams(window.location.search);
loadSessions();
loadContext(cleanSessionId(urlParams.get("session_id")), urlParams.get("source"));
loadHealth();
