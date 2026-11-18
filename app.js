const state = {
    mode: "quick",
    outputStyle: "concise",
    outputStructure: "结论,变更,验证,限制,下一步",
    result: "",
    context: "",
    sessionId: "",
    source: "",
    sendMode: "",
    sessions: [],
    bridges: {},
    health: {},
    selectedBridgeSource: "",
    model: "",
    sessionModel: "",
    sessionModelSource: "",
    sessionModelTimestamp: "",
    effectiveModel: "",
    effectiveModelSource: "",
    continuation: null,
    continuationSelection: 0,
    integrationMode: "standalone",
    handoff: null,
    integrationExpired: false,
    integrationSending: false,
    integrationDelivery: "",
    integrationSendKey: "",
    sessionsLoading: false,
    sessionSourceFilter: "all",
    busy: false,
    resultPanel: "analysis",
};
const $ = (selector) => document.querySelector(selector);
const draftEl = $("#draft");
const resultEl = $("#result");
const contextPreview = $("#context-preview");
const contextMeta = $("#context-meta");
const polishButton = $("#polish-button");
const continueButton = $("#continue-button");
const sendButton = $("#send-button");
const copyButton = $("#copy-button");
const restoreButton = $("#restore-button");
const repolishButton = $("#repolish-button");
const loadingOverlay = $("#loading-overlay");
const emptyState = $("#empty-state");
const statusBadge = $("#status-badge");
const bindingSummary = $("#binding-summary");
const bindingNote = $("#binding-note");
const integrationStatus = $("#integration-status");
const integrationExpiry = $("#integration-expiry");
const integrationTarget = $("#integration-target");
const integrationEvidence = $("#integration-evidence");
const sessionModal = $("#session-modal");
const sessionList = $("#session-list");
const sessionFilter = $("#session-filter");
const sessionPlatforms = $("#session-platforms");
const settingsModal = $("#settings-modal");
const settingsBody = $("#settings-body");
const bridgeStatus = $("#bridge-status");
const sendModeRow = $("#send-mode-row");
const sendModeGroup = $("#send-mode-group");
const sendModeHint = $("#send-mode-hint");
const sendEffectiveModel = $("#send-effective-model");
const targetModelLabel = $("#target-model-label");
const continuationPanel = $("#continuation-panel");
const analysisPanel = $("#analysis-panel");
const decisionPanel = $("#decision-panel");
const decisionEmpty = $("#decision-empty");
const decisionContent = $("#decision-content");
const analysisPanelTab = $("#analysis-panel-tab");
const decisionPanelTab = $("#decision-panel-tab");
const decisionBadge = $("#decision-badge");
const handoffPacket = $("#handoff-packet");
const toast = $("#toast");
let toastTimer = null;
let contextLoadGeneration = 0;

function isAgentXRayMode() {
    return state.integrationMode === "agentxray";
}

function handoffExpiry(handoff) {
    if (!handoff || !handoff.expires_at) return null;
    const expiry = new Date(handoff.expires_at);
    return Number.isNaN(expiry.getTime()) ? null : expiry;
}

function isHandoffExpired(handoff = state.handoff) {
    const expiry = handoffExpiry(handoff);
    return !expiry || expiry.getTime() <= Date.now();
}

function integrationRequestFields() {
    if (!isAgentXRayMode()) return {};
    return {
        integration_mode: "agentxray",
        handoff: state.handoff,
        source: state.source,
        session_id: state.sessionId,
        cwd: state.handoff?.target?.cwd || "",
    };
}

function integrationQueryFields() {
    return new URLSearchParams({mode: "agentxray", handoff_id: state.handoff?.handoff_id || ""});
}

function integrationStorageKey() {
    return `sidecar-intervention:${state.handoff?.handoff_id || ""}`;
}

function saveIntegrationAttempt() {
    try {
        sessionStorage.setItem(integrationStorageKey(), JSON.stringify({
            key: state.integrationSendKey, status: state.integrationDelivery,
            result: state.result, draft: draftEl.value,
        }));
    } catch { /* Storage can be disabled; server remains authoritative. */ }
}

function applyImportedHandoff(handoff) {
    state.handoff = handoff;
    const aliases = {"claude-code": "claude", workbuddy: "workbuddy_local"};
    state.source = aliases[handoff.target.platform] || handoff.target.platform;
    state.sessionId = handoff.target.session_id;
    draftEl.value = handoff.user_request || "";
    try {
        const saved = JSON.parse(sessionStorage.getItem(integrationStorageKey()) || "null");
        if (saved) {
            state.integrationSendKey = saved.key || "";
            state.integrationDelivery = saved.status === "sending" ? "delivery_unknown" : saved.status;
            draftEl.value = saved.draft || draftEl.value;
            setResult(saved.result || "");
        }
    } catch { /* No saved draft available. */ }
    $("#draft-count").textContent = `${draftEl.value.length} 字`;
    if (["awaiting_agent", "advanced", "sent"].includes(handoff.status)) {
        state.integrationDelivery = "sent";
    } else if (handoff.status === "delivery_unknown" || handoff.error?.code === "delivery_unknown") {
        state.integrationDelivery = "delivery_unknown";
    }
    renderIntegrationState();
}

function renderIntegrationState() {
    if (!integrationStatus) return;
    const enabled = isAgentXRayMode();
    if (targetModelLabel) targetModelLabel.hidden = enabled;
    integrationStatus.classList.toggle("hidden", !enabled);
    if (!enabled) return;
    const handoff = state.handoff || {};
    const target = handoff.target || {};
    const expiry = handoffExpiry(handoff);
    state.integrationExpired = state.integrationExpired || isHandoffExpired(handoff);
    integrationExpiry.textContent = state.integrationExpired
        ? "已过期"
        : expiry ? `有效至 ${expiry.toLocaleString()}` : "过期时间无效";
    integrationExpiry.className = `integration-expiry${state.integrationExpired ? " expired" : ""}`;
    integrationTarget.innerHTML = `
        <strong>锁定目标</strong>
        <span>${escapeHtml(sourceLabel(state.source || target.platform))}</span>
        <span>${escapeHtml(target.title || "会话")} · ${escapeHtml(target.session_id || "未知")}</span>
        <span>${escapeHtml(target.cwd || "未知")}</span>`;
    integrationEvidence.textContent = [
        handoff.reason,
        handoff.recommended_action ? `建议：${handoff.recommended_action}` : "",
        state.integrationDelivery === "sent" ? "已发送，等待 AgentXRay 观察后续进展" : "",
        state.integrationDelivery === "delivery_unknown"
            ? "发送结果未知，草稿已保留；请返回 AgentXRay 核对，勿重复发送" : "",
    ].filter(Boolean).join(" · ");
    $("#open-session-modal").disabled = enabled;
    $("#refresh-sessions").disabled = enabled;
    $("#refresh-context").disabled = enabled && state.integrationExpired;
    updateSendControls();
}

const SOURCE_LABELS = {
    codex: "Codex",
    claude: "Claude",
    openclaw: "OpenClaw",
    openhands: "OpenHands",
    cursor: "Cursor",
    copilot: "Copilot",
    gemini: "Gemini CLI",
    devin: "Devin",
    doubao_work: "豆包工作",
    workbuddy: "WorkBuddy",
    workbuddy_cloud: "WorkBuddy",
    workbuddy_local: "WorkBuddy",
    dsh: "DSH",
};
const BRIDGE_FIELDS = [
    { key: "adapter", label: "适配器", placeholder: "generic" },
    { key: "label", label: "显示名称", placeholder: "来源名称" },
    { key: "transport", label: "传输", placeholder: "auto / http / cli" },
    { key: "surface", label: "界面", placeholder: "cloud_task / local" },
    { key: "endpoint", label: "端点", placeholder: "http://127.0.0.1:..." },
    { key: "auth_ref", label: "认证引用", placeholder: "WORKBUDDY_ACCESS_TOKEN" },
    { key: "session_binding", label: "会话绑定", placeholder: "route / explicit" },
    { key: "home", label: "主目录", placeholder: "~/.agent" },
    { key: "projects_dir", label: "会话目录", placeholder: "~/.claude/projects" },
    { key: "db_path", label: "会话数据库", placeholder: "~/.workbuddy/workbuddy.db" },
    { key: "sessions_dir", label: "会话目录", placeholder: "~/.agents/sessions" },
    { key: "workspace_storage", label: "会话索引", placeholder: "~/.agents/workspace.json" },
    { key: "bin", label: "CLI 路径", placeholder: "claude" },
    { key: "script", label: "CLI 脚本", placeholder: "codebuddy-lite-wb.mjs" },
    { key: "organization_id", label: "组织 ID", placeholder: "org-..." },
    { key: "repo", label: "仓库", placeholder: "owner/repo" },
    { key: "api_version", label: "API 版本", placeholder: "2026-03-10" },
    { key: "bridge_url", label: "本地桥地址", placeholder: "http://127.0.0.1:8766" },
    { key: "token_file", label: "Token 文件", placeholder: "~/.agent/bridge.token" },
    {
        key: "timeout_ms",
        label: "超时 (ms)",
        placeholder: "30000",
        type: "number",
        inputMode: "numeric",
        min: "1",
    },
    {
        key: "retry_count",
        label: "重试次数",
        placeholder: "1",
        type: "number",
        inputMode: "numeric",
        min: "0",
    },
];
const CAPABILITY_LABELS = {
    list_sessions: "会话列表",
    read_context: "读取上下文",
    send: "发送",
};
const SEND_MODE_LABELS = {
    queue: "排队",
    steer: "插入",
};
const HEALTH_STATUS_LABELS = {
    available: "可用",
    ok: "可用",
    healthy: "健康",
    ready: "可用",
    degraded: "降级",
    partial: "部分可用",
    configured: "已配置",
    tool_only: "MCP 只读",
    unverified: "未验证",
    unavailable: "不可用",
    offline: "离线",
    error: "异常",
    failed: "失败",
    auth_missing: "缺少认证",
    disabled: "已停用",
    unknown: "未知",
};

function showToast(message) {
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 1800);
}

function sourceKey(source) {
    const key = String(source || "generic")
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-")
        .replace(/^-+|-+$/g, "");
    return key || "generic";
}

function humanizeIdentifier(value) {
    return String(value || "")
        .replace(/[_-]+/g, " ")
        .trim()
        .replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
}

function bridgeFor(source) {
    const configured = state.bridges[source] && typeof state.bridges[source] === "object"
        ? state.bridges[source]
        : {};
    const healthEntry = state.health[source] && typeof state.health[source] === "object"
        ? state.health[source]
        : {};
    return { ...configured, ...healthEntry };
}

function sourceLabel(source) {
    if (!source) return "通用桥接";
    return bridgeFor(source).label || SOURCE_LABELS[source] || humanizeIdentifier(source);
}

function normalizeModelCapability(value) {
    const capability = value && typeof value === "object" ? value : {};
    const mode = ["dynamic", "fixed", "unsupported"].includes(capability.mode)
        ? capability.mode
        : "unsupported";
    const options = Array.isArray(capability.options)
        ? capability.options
            .filter((option) => option && typeof option === "object" && option.id)
            .map((option) => ({
                id: String(option.id),
                label: String(option.label || option.id),
            }))
        : [];
    return {
        mode,
        default_model_id: String(capability.default_model_id || ""),
        fixed_model_id: String(capability.fixed_model_id || ""),
        fixed_model_label: String(capability.fixed_model_label || ""),
        options,
        allow_custom: capability.allow_custom === true,
        reason: String(capability.reason || ""),
    };
}

function capabilitiesFor(source) {
    const bridge = bridgeFor(source);
    const capabilities = bridge.capabilities && typeof bridge.capabilities === "object"
        ? bridge.capabilities
        : {};
    const rawModes = capabilities.send_modes;
    const sendModes = Array.isArray(rawModes)
        ? rawModes.map((mode) => String(mode).trim()).filter(Boolean)
        : String(rawModes || "").split(",").map((mode) => mode.trim()).filter(Boolean);
    return {
        list_sessions: capabilities.list_sessions === true,
        read_context: capabilities.read_context === true,
        send: capabilities.send === true,
        send_modes: [...new Set(sendModes)],
        context_mode: String(capabilities.context_mode || ""),
        model_selection: normalizeModelCapability(capabilities.model_selection),
    };
}

function modelLabel(modelId, capability = null) {
    const normalized = String(modelId || "").trim();
    if (!normalized) return "未指定";
    if (normalized === "session-default") return "会话默认模型";
    if (normalized === "current-thread") return "当前会话模型";
    if (normalized === "session") return "会话模型";
    const option = capability && capability.options
        ? capability.options.find((item) => item.id === normalized)
        : null;
    return option ? option.label : normalized;
}

function effectiveModelLabel(modelId, capability = null) {
    const normalized = String(modelId || "").trim();
    if (!normalized) return "未知";
    if (["session-default", "current-thread", "session", "default"].includes(normalized)) {
        return "未知（跟随会话）";
    }
    if (["unknown", "<synthetic>"].includes(normalized)) return "未知";
    const label = modelLabel(normalized, capability);
    return label === normalized ? normalized : `${label} (${normalized})`;
}

function applySessionModel(packet = {}) {
    const modelId = typeof packet.model_id === "string" ? packet.model_id.trim() : "";
    const source = typeof packet.model_source === "string" ? packet.model_source : "";
    const known = modelId && source && !["session-default", "current-thread", "session",
        "default", "unknown", "<synthetic>"].includes(modelId);
    state.sessionModel = known ? modelId : "";
    state.sessionModelSource = known ? source : "";
    state.sessionModelTimestamp = known ? String(packet.model_timestamp || "") : "";
}

function targetModelText(source) {
    if (source !== state.source || !state.sessionId) return "目标模型 · 未知（未绑定会话）";
    return state.sessionModel
        ? `目标模型 · ${state.sessionModel}（最近记录）`
        : "目标模型 · 未知（桥接未提供可确认的模型名）";
}

function renderTargetModel() {
    if (!targetModelLabel) return;
    targetModelLabel.textContent = targetModelText(state.source);
    targetModelLabel.title = state.sessionModel
        ? `来源：${state.sessionModelSource}${state.sessionModelTimestamp
            ? ` · ${state.sessionModelTimestamp}` : ""}；最近记录不能保证下一次执行使用同一模型。`
        : "固定表示跟随目标会话的模型；Sidecar 润色模型不能用来推断目标模型。";
    const fixedModel = settingsBody?.querySelector("[data-model-source]");
    if (fixedModel) fixedModel.textContent = targetModelText(fixedModel.dataset.modelSource);
}

function renderEffectiveModel() {
    if (!sendEffectiveModel) return;
    const modelId = String(state.effectiveModel || "").trim();
    if (!modelId) {
        sendEffectiveModel.classList.add("hidden");
        sendEffectiveModel.textContent = "";
        sendEffectiveModel.removeAttribute("title");
        delete sendEffectiveModel.dataset.modelId;
        return;
    }
    const source = state.effectiveModelSource || state.source;
    const capability = capabilitiesFor(source).model_selection;
    const label = effectiveModelLabel(modelId, capability);
    const unknown = label.startsWith("未知");
    sendEffectiveModel.textContent = `实际使用模型 · ${label}`;
    sendEffectiveModel.title = unknown
        ? "桥接仅确认发送策略，没有提供执行时使用的具体模型。"
        : `effective_model: ${modelId}`;
    sendEffectiveModel.classList.toggle("unknown", unknown);
    if (unknown) delete sendEffectiveModel.dataset.modelId;
    else sendEffectiveModel.dataset.modelId = modelId;
    sendEffectiveModel.classList.remove("hidden");
}

function healthFor(source) {
    const bridge = bridgeFor(source);
    const configured = bridge.health && typeof bridge.health === "object" ? bridge.health : {};
    const remoteBridge = state.health[source];
    const remote = remoteBridge && remoteBridge.health && typeof remoteBridge.health === "object"
        ? remoteBridge.health
        : remoteBridge;
    return { ...configured, ...(remote && typeof remote === "object" ? remote : {}) };
}

function healthTone(health) {
    const status = String(health.status || "").toLowerCase();
    if (health.available === true) return "available";
    if (["degraded", "partial", "unverified", "configured", "tool_only", "warning", "warn"].includes(status)) return "degraded";
    if (["disabled", "unknown"].includes(status)) return "unknown";
    if (health.available === false || ["unavailable", "offline", "error", "failed"].includes(status)) {
        return "unavailable";
    }
    return "unknown";
}

function healthLabel(health) {
    const status = String(health.status || "").toLowerCase();
    if (HEALTH_STATUS_LABELS[status]) return HEALTH_STATUS_LABELS[status];
    if (humanizeIdentifier(status)) return humanizeIdentifier(status);
    if (health.available === true) return "可用";
    if (health.available === false) return "不可用";
    return "未知";
}

function healthBadge(source) {
    const health = healthFor(source);
    const tone = healthTone(health);
    const status = health.status || (health.available ? "available" : "unknown");
    const title = [status, health.message].filter(Boolean).join(" · ");
    return `<span class="health-badge ${tone}" title="${escapeHtml(title)}">${escapeHtml(healthLabel(health))}</span>`;
}

function capabilityBadge(label, enabled, title = "") {
    const tone = enabled ? "on" : "off";
    const detail = title || (enabled ? "已声明" : "未声明");
    return `<span class="capability-badge ${tone}" title="${escapeHtml(detail)}">${escapeHtml(label)}</span>`;
}

function capabilityBadges(source, { includeModes = true } = {}) {
    const capabilities = capabilitiesFor(source);
    const badges = Object.entries(CAPABILITY_LABELS).map(([key, label]) => (
        capabilityBadge(label, capabilities[key])
    ));
    if (includeModes && capabilities.send_modes.length) {
        capabilities.send_modes.forEach((mode) => {
            badges.push(capabilityBadge(sendModeLabel(mode), true, `send_modes: ${mode}`));
        });
    }
    if (capabilities.read_context && capabilities.context_mode !== "none") {
        const contextModeLabels = {
            full: "可读取会话内容",
            best_effort: "可读取部分上下文",
            metadata: "仅会话元数据",
        };
        const contextLabel = contextModeLabels[capabilities.context_mode]
            || `上下文 ${capabilities.context_mode}`;
        badges.push(capabilityBadge(
            contextLabel,
            true,
            `context_mode: ${capabilities.context_mode}`,
        ));
    }
    const modelMode = capabilities.model_selection.mode;
    const modelModeLabels = {
        dynamic: "模型可调",
        fixed: "跟随会话模型",
        unsupported: "不支持模型选择",
    };
    badges.push(capabilityBadge(
        modelModeLabels[modelMode] || "模型未知",
        modelMode === "dynamic" || modelMode === "fixed",
        modelMode === "fixed"
            ? "跟随目标会话模型；Sidecar 不能覆盖模型，不表示模型不可用。"
            : capabilities.model_selection.reason || `model_selection: ${modelMode}`,
    ));
    return badges.join("");
}

function updateBridgeStatus() {
    if (!bridgeStatus) return;
    if (!state.source) {
        bridgeStatus.className = "bridge-status empty";
        bridgeStatus.textContent = "绑定会话后显示桥接健康状态与能力";
        return;
    }
    bridgeStatus.className = "bridge-status";
    const healthMessage = healthFor(state.source).message;
    bridgeStatus.innerHTML = `
        <span class="bridge-status-source">${escapeHtml(sourceLabel(state.source))}</span>
        ${healthBadge(state.source)}
        ${healthMessage ? `<span class="bridge-health-message">${escapeHtml(healthMessage)}</span>` : ""}
        <span class="capability-list">${capabilityBadges(state.source)}</span>`;
}

function sendModeLabel(mode) {
    return SEND_MODE_LABELS[mode] || humanizeIdentifier(mode) || mode;
}

function sendModeDescription(mode) {
    if (mode === "queue") return "加入待处理队列";
    if (mode === "steer") return "尝试插入当前轮次";
    return `使用 ${mode} 方式发送`;
}

function currentSendModes() {
    return capabilitiesFor(state.source).send_modes;
}

function updateSendControls() {
    if (isAgentXRayMode()) {
        const ready = Boolean(state.handoff && state.sessionId && state.result)
            && !isHandoffExpired() && !state.integrationDelivery;
        sendButton.disabled = state.busy || state.integrationSending || !ready;
        sendButton.textContent = state.integrationExpired
            ? "handoff 已过期"
            : state.integrationDelivery === "sent" ? "已发送，等待会话进展"
            : state.integrationDelivery === "delivery_unknown" ? "请核对发送结果"
            : "发送到当前会话";
        sendButton.title = state.integrationExpired
            ? "请从 AgentXRay 重新打开有效的 handoff"
            : "发送将由 AgentXRay 校验并投递到锁定目标";
        return;
    }
    const capabilities = capabilitiesFor(state.source);
    const modeReady = capabilities.send && currentSendModes().includes(state.sendMode);
    sendButton.disabled = state.busy || !state.result || !state.sessionId || !modeReady;
    if (!state.source) {
        sendButton.textContent = "发送到当前会话";
        sendButton.title = "先绑定会话";
    } else if (!capabilities.send) {
        sendButton.textContent = "当前桥接不支持发送";
        sendButton.title = "当前桥接的 capabilities.send 为 false";
    } else if (!capabilities.send_modes.length) {
        sendButton.textContent = "未配置发送方式";
        sendButton.title = "当前桥接未声明 capabilities.send_modes";
    } else {
        sendButton.textContent = "发送到当前会话";
        sendButton.title = modeReady ? "按所选方式发送润色结果" : "请选择发送方式";
    }
}

function renderSendModes() {
    if (!sendModeRow || !sendModeGroup || !sendModeHint) return;
    if (isAgentXRayMode()) {
        sendModeGroup.hidden = true;
        sendModeHint.hidden = false;
        sendModeHint.textContent = "集成模式固定通过 AgentXRay handoff 发送";
        state.sendMode = "handoff";
        updateSendControls();
        renderContinuationDestination();
        return;
    }
    const capabilities = capabilitiesFor(state.source);
    let hint = "";
    if (!state.source) {
        hint = "绑定会话后读取发送能力";
    } else if (!capabilities.send) {
        hint = "当前桥接未声明发送能力";
    } else if (!capabilities.send_modes.length) {
        hint = "当前桥接未声明发送方式";
    }

    sendModeGroup.innerHTML = "";
    if (hint) {
        sendModeGroup.hidden = true;
        sendModeHint.hidden = false;
        sendModeHint.textContent = hint;
        state.sendMode = "";
        updateSendControls();
        renderContinuationDestination();
        return;
    }

    const modes = capabilities.send_modes;
    if (!modes.includes(state.sendMode)) state.sendMode = modes[0];
    sendModeGroup.hidden = false;
    sendModeHint.hidden = true;
    modes.forEach((mode) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "send-mode" + (mode === state.sendMode ? " active" : "");
        button.dataset.sendMode = mode;
        button.textContent = sendModeLabel(mode);
        button.title = sendModeDescription(mode);
        button.addEventListener("click", () => {
            state.sendMode = mode;
            renderSendModes();
        });
        sendModeGroup.appendChild(button);
    });
    updateSendControls();
    renderContinuationDestination();
}

function updateSourceUi() {
    renderSendModes();
    updateBridgeStatus();
    renderTargetModel();
}

function setBusy(busy) {
    state.busy = busy;
    polishButton.disabled = busy;
    continueButton.disabled = busy;
    repolishButton.disabled = busy || !state.result;
    updateSendControls();
    loadingOverlay.classList.toggle("hidden", !busy);
    statusBadge.className = "status-badge" + (busy ? " loading" : state.result ? " ready" : "");
    statusBadge.textContent = busy ? "润色中" : state.result ? "已生成" : "等待润色";
}

function setResult(text) {
    state.result = text;
    state.effectiveModel = "";
    state.effectiveModelSource = "";
    renderEffectiveModel();
    resultEl.value = text;
    emptyState.classList.toggle("hidden", Boolean(text));
    copyButton.disabled = !text;
    restoreButton.disabled = !text;
    repolishButton.disabled = !text || state.busy;
    updateSendControls();
    if (text) {
        statusBadge.className = "status-badge ready";
        statusBadge.textContent = "已生成";
    }
}

function continuationStatusLabel(status) {
    return {
        ready: "可继续",
        in_progress: "进行中",
        blocked: "存在阻塞",
        needs_user_decision: "等待用户决定",
        completed: "任务已完成",
        insufficient_context: "上下文不足",
    }[status] || status || "未分类";
}

function continuationList(items, empty = "暂无") {
    if (!Array.isArray(items) || !items.length) return `<span class="muted">${escapeHtml(empty)}</span>`;
    return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function completedList(items) {
    if (!Array.isArray(items) || !items.length) return '<span class="muted">暂无可确认的已完成事项</span>';
    return `<ul>${items.map((item) => {
        const title = typeof item === "string" ? item : item.item;
        const evidence = typeof item === "object" ? item.evidence : "";
        return `<li>${escapeHtml(title || "")}${evidence ? ` <span class="muted">（${escapeHtml(evidence)}）</span>` : ""}</li>`;
    }).join("")}</ul>`;
}

function renderContinuationDestination() {
    const destination = continuationPanel?.querySelector(".continuation-destination");
    if (!destination) return;
    destination.innerHTML = `目标 session：<strong>${escapeHtml(sourceLabel(state.source))}</strong> · ${escapeHtml(state.sessionId || "未绑定")} · 发送方式：<strong>${escapeHtml(state.sendMode ? sendModeLabel(state.sendMode) : "未选择")}</strong>`;
}

function setResultPanel(panel) {
    state.resultPanel = panel === "decision" ? "decision" : "analysis";
    const analysisActive = state.resultPanel === "analysis";
    analysisPanel?.classList.toggle("hidden", !analysisActive);
    decisionPanel?.classList.toggle("hidden", analysisActive);
    analysisPanelTab?.classList.toggle("active", analysisActive);
    decisionPanelTab?.classList.toggle("active", !analysisActive);
    analysisPanelTab?.setAttribute("aria-selected", String(analysisActive));
    decisionPanelTab?.setAttribute("aria-selected", String(!analysisActive));
}

function decisionItems(data) {
    return [
        ["需要你决定的事项", data.clarifying_questions],
        ["信息不足", data.missing],
        ["存在冲突", data.conflicts],
        ["当前阻塞", data.blockers],
    ].filter(([, items]) => Array.isArray(items) && items.length);
}

function renderDecisionPanel(data) {
    if (!decisionContent || !decisionEmpty) return;
    const sections = decisionItems(data);
    const count = sections.reduce((total, [, items]) => total + items.length, 0);
    const hasDecision = count > 0 || data.status === "needs_user_decision";
    decisionBadge?.classList.toggle("hidden", !hasDecision);
    if (decisionBadge) decisionBadge.textContent = String(count || 1);
    decisionEmpty.classList.toggle("hidden", hasDecision);
    decisionContent.classList.toggle("hidden", !hasDecision);
    if (!hasDecision) {
        decisionContent.innerHTML = "";
        return;
    }
    decisionContent.innerHTML = sections.map(([title, items]) => `
        <section class="decision-section">
            <h3>${escapeHtml(title)}</h3>
            ${continuationList(items)}
        </section>`).join("");
}

function renderContinuation(data) {
    if (!continuationPanel) return;
    state.continuation = data;
    state.continuationSelection = 0;
    const recommendations = Array.isArray(data.recommendations) ? data.recommendations : [];
    const status = continuationStatusLabel(data.status);
    const tabs = recommendations.map((item, index) => (
        `<button type="button" class="recommendation-tab${index === 0 ? " active" : ""}" data-recommendation-index="${index}">
            ${index === 0 ? "主要推荐" : `备选 ${index}`}：${escapeHtml(item.title || "下一步")}
        </button>`
    )).join("");
    const primary = recommendations[0] || {};
    continuationPanel.innerHTML = `
        <div class="continuation-head">
            <div><span class="eyebrow">ANALYSIS</span><strong>分析结果</strong></div>
            <span class="continuation-status">${escapeHtml(status)}</span>
        </div>
        <div class="continuation-destination">目标 session：<strong>${escapeHtml(sourceLabel(state.source))}</strong> · ${escapeHtml(state.sessionId || "未绑定")} · 发送方式：<strong>${escapeHtml(state.sendMode ? sendModeLabel(state.sendMode) : "未选择")}</strong></div>
        <div class="continuation-grid">
            <div class="continuation-block"><strong>当前进展</strong><p>${escapeHtml(progressSummary(data))}</p></div>
            <div class="continuation-block"><strong>建议下一步</strong><p>${escapeHtml(primary.title || "暂无推荐")}</p><p class="muted">${escapeHtml(data.recommendation_reason || primary.reason || "")}</p></div>
        </div>
        <div class="recommendation-tabs" role="tablist">${tabs}</div>`;
    continuationPanel.classList.remove("hidden");
    renderDecisionPanel(data);
    renderContinuationDestination();
    continuationPanel.querySelectorAll("[data-recommendation-index]").forEach((button) => {
        button.addEventListener("click", () => {
            const index = Number(button.dataset.recommendationIndex || 0);
            state.continuationSelection = index;
            continuationPanel.querySelectorAll(".recommendation-tab").forEach((tab, tabIndex) => {
                tab.classList.toggle("active", tabIndex === index);
            });
            setResult(recommendations[index]?.prompt || "");
        });
    });
}

function progressSummary(data) {
    const completed = Array.isArray(data.completed) ? data.completed : [];
    const pending = Array.isArray(data.pending) ? data.pending : [];
    const completedText = completed.length
        ? `已完成：${completed.map((item) => typeof item === "string" ? item : item.item).filter(Boolean).join("、")}`
        : "已完成当前会话上下文读取与任务分析";
    const pendingText = pending.length
        ? `还剩：${pending.join("、")}`
        : "当前没有待完成事项";
    return `${completedText}；${pendingText}。`;
}

function shortId(sessionId) {
    return sessionId ? sessionId.replace(/^session-/, "").slice(0, 8) : "未绑定";
}

function sourceBadge(source) {
    const key = sourceKey(source);
    const known = Object.prototype.hasOwnProperty.call(SOURCE_LABELS, source);
    const genericClass = known || key === "generic" ? "" : " generic source-generic";
    return `<span class="source-badge ${key} source-${key}${genericClass}">${escapeHtml(sourceLabel(source))}</span>`;
}

function updateBindingSummary(title, sessionId, source) {
    const name = title || sessionId || "未绑定";
    bindingSummary.innerHTML = sessionId
        ? `${sourceBadge(source)} <strong>${escapeHtml(name)}</strong> <span class="muted">· ${shortId(sessionId)}</span>`
        : "未绑定";
}

function projectName(cwd) {
    const parts = String(cwd || "").split(/[\\/]/).filter(Boolean);
    return parts.at(-1) || "未识别项目";
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

function sessionPlatformSources() {
    const sources = new Map();
    state.sessions.forEach((session) => {
        const source = session.source || "";
        if (source) sources.set(source, (sources.get(source) || 0) + 1);
    });
    const order = ["codex", "claude", "gemini", "cursor", "openhands", "openclaw"];
    return [...sources.entries()].sort((a, b) => {
        const aIndex = order.indexOf(a[0]);
        const bIndex = order.indexOf(b[0]);
        if (aIndex !== -1 || bIndex !== -1) {
            return (aIndex === -1 ? 99 : aIndex) - (bIndex === -1 ? 99 : bIndex);
        }
        return sourceLabel(a[0]).localeCompare(sourceLabel(b[0]));
    });
}

function renderSessionPlatforms() {
    if (!sessionPlatforms) return;
    sessionPlatforms.innerHTML = "";
    const addPlatform = (source, count, badgeHtml, label) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `session-platform${state.sessionSourceFilter === source ? " active" : ""}`;
        button.innerHTML = `${badgeHtml}<span class="session-platform-label">${escapeHtml(label)}</span><span class="session-platform-count">${count}</span>`;
        button.addEventListener("click", () => {
            state.sessionSourceFilter = source;
            renderSessionList();
        });
        sessionPlatforms.appendChild(button);
    };
    addPlatform("all", state.sessions.length, '<span class="session-platform-icon all">✦</span>', "全部平台");
    sessionPlatformSources().forEach(([source, count]) => {
        const symbol = escapeHtml(sourceLabel(source).slice(0, 1).toUpperCase());
        addPlatform(source, count, `<span class="session-platform-icon platform-source source-${sourceKey(source)}">${symbol}</span>`, sourceLabel(source));
    });
}

function renderSessionList() {
    const keyword = sessionFilter.value.trim().toLowerCase();
    const sessions = state.sessions
        .filter((session) => state.sessionSourceFilter === "all" || session.source === state.sessionSourceFilter)
        .filter((session) => {
            if (!keyword) return true;
            const haystack = `${session.title || ""} ${session.cwd || ""} ${session.session_id} ${sourceLabel(session.source)}`
                .toLowerCase();
            return haystack.includes(keyword);
        })
        .sort((a, b) => Number(b.recency_at || 0) - Number(a.recency_at || 0));
    sessionList.innerHTML = "";
    renderSessionPlatforms();
    const groups = new Map();
    sessions.forEach((session) => {
        const source = session.source || "";
        if (!groups.has(source)) groups.set(source, []);
        groups.get(source).push(session);
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

    const renderSessionItem = (session, source) => {
        const item = document.createElement("button");
        item.type = "button";
        const isActive = session.session_id === state.sessionId && source === state.source;
        item.className = "session-item" + (isActive ? " active" : "");
        const title = session.title || session.cwd || "未命名会话";
        item.innerHTML = `
            <span class="session-item-main">
                <span class="session-item-radio" aria-hidden="true">${isActive ? "✓" : ""}</span>
                <span class="session-item-copy"><strong>${escapeHtml(title.slice(0, 60))}</strong><small>${escapeHtml(session.cwd || "")}</small></span>
            </span>
            <span class="session-item-meta">${shortId(session.session_id)} · ${escapeHtml(session.updated_at || "")}</span>`;
        item.addEventListener("click", () => {
            closeSessionModal();
            bindSession(source, session.session_id);
        });
        return item;
    };

    if (state.sessionSourceFilter === "all" && !keyword && sessions.length) {
        const recentGroup = document.createElement("section");
        recentGroup.className = "session-group session-group-recent";
        const recent = sessions.slice(0, 3);
        recentGroup.innerHTML = `<div class="session-group-head"><span class="session-group-dot recent"></span><strong>最近使用 · 全平台</strong><span class="muted">${recent.length} 个</span></div>`;
        const recentItems = document.createElement("div");
        recentItems.className = "session-group-items";
        recent.forEach((session) => recentItems.appendChild(renderSessionItem(session, session.source || "")));
        recentGroup.appendChild(recentItems);
        sessionList.appendChild(recentGroup);
    }

    sources.forEach((source) => {
        const items = groups.get(source);
        const group = document.createElement("section");
        group.className = "session-group";
        group.innerHTML = `<div class="session-group-head">${sourceBadge(source)}<strong>${escapeHtml(sourceLabel(source))}</strong><span class="muted">${items.length} 个会话</span></div>`;
        const groupItems = document.createElement("div");
        groupItems.className = "session-group-items";
        items.forEach((session) => groupItems.appendChild(renderSessionItem(session, source)));
        group.appendChild(groupItems);
        sessionList.appendChild(group);
    });
    const total = state.sessions.length;
    const loadingSuffix = state.sessionsLoading ? " · 正在加载" : "";
    $("#session-count").textContent = keyword || state.sessionSourceFilter !== "all"
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
    state.sessionSourceFilter = "all";
}

function bridgeOptionLabel(source, bridge) {
    const health = healthFor(source);
    const capabilities = capabilitiesFor(source);
    const enabled = bridge.enabled === false ? "停用" : "启用";
    const sendable = capabilities.send
        ? `可发送${capabilities.send_modes.length ? `(${capabilities.send_modes.join("/")})` : ""}`
        : "只读";
    return `${sourceLabel(source)} · ${enabled} · ${healthLabel(health)} · ${sendable}`;
}

function visibleBridgeFields(source, bridge) {
    const common = new Set([
        "adapter", "label", "transport", "surface", "endpoint", "auth_ref",
        "session_binding", "timeout_ms", "retry_count",
    ]);
    const adapter = String(bridge.adapter || source);
    const surface = String(bridge.surface || "");
    if (adapter === "codex") common.add("home");
    if (adapter === "dsh") ["home", "sessions_dir", "workspace_storage", "bridge_url", "token_file"].forEach((key) => common.add(key));
    if (adapter === "claude") ["home", "projects_dir", "bin"].forEach((key) => common.add(key));
    if (adapter === "gemini") ["home", "sessions_dir", "bin"].forEach((key) => common.add(key));
    if (adapter === "workbuddy" && surface === "local") ["home", "db_path", "projects_dir", "script"].forEach((key) => common.add(key));
    if (adapter === "workbuddy" && surface === "cloud_task") ["endpoint", "auth_ref"].forEach((key) => common.add(key));
    if (adapter === "devin") common.add("organization_id");
    if (adapter === "copilot") ["repo", "api_version"].forEach((key) => common.add(key));
    return BRIDGE_FIELDS.filter((field) => common.has(field.key));
}

function bridgeFieldsHtml(source, bridge) {
    const visibleKeys = new Set(visibleBridgeFields(source, bridge).map((field) => field.key));
    return BRIDGE_FIELDS
        .filter((field) => visibleKeys.has(field.key))
        .map((field) => `
            <label class="settings-field">
                <span>${field.label}</span>
                <input type="${field.type || "text"}" data-field="${field.key}"
                    value="${escapeHtml(bridge[field.key] ?? "")}"
                    placeholder="${escapeHtml(field.placeholder || "")}"
                    ${field.inputMode ? `inputmode="${field.inputMode}"` : ""}
                    ${field.min ? `min="${field.min}"` : ""}>
            </label>`)
        .join("");
}

function modelSelectionConfigFor(bridge) {
    const value = bridge && bridge.model_selection && typeof bridge.model_selection === "object"
        ? bridge.model_selection
        : {};
    return {
        mode: value.mode === "explicit" ? "explicit" : "bridge_default",
        model_id: String(value.model_id || ""),
    };
}

function modelSelectionHtml(source, bridge) {
    const capability = capabilitiesFor(source).model_selection;
    const config = modelSelectionConfigFor(bridge);
    if (capability.mode === "dynamic") {
        const options = capability.options.map((option) => (
            `<option value="${escapeHtml(option.id)}">${escapeHtml(option.label)}</option>`
        )).join("");
        const listId = `model-options-${sourceKey(source)}`;
        return `
            <div class="settings-model" data-model-section>
                <div class="settings-model-head">
                    <strong>模型选择</strong>
                    <span class="capability-badge on">可配置</span>
                </div>
                <label class="settings-field">
                    <span>取值方式</span>
                    <select data-model-mode>
                        <option value="bridge_default" ${config.mode === "bridge_default" ? "selected" : ""}>跟随会话默认</option>
                        <option value="explicit" ${config.mode === "explicit" ? "selected" : ""}>指定模型</option>
                    </select>
                </label>
                <label class="settings-field">
                    <span>模型 ID</span>
                    <input data-model-id list="${escapeHtml(listId)}"
                        value="${escapeHtml(config.model_id)}"
                        placeholder="${escapeHtml(capability.options[0]?.id || "模型名称")}">
                </label>
                <datalist id="${escapeHtml(listId)}">${options}</datalist>
                ${capability.reason ? `<p class="settings-model-note">${escapeHtml(capability.reason)}</p>` : ""}
            </div>`;
    }
    if (capability.mode === "fixed") {
        return `
            <div class="settings-model" data-model-section>
                <div class="settings-model-head">
                    <strong>模型选择</strong>
                    <span class="capability-badge on">跟随会话</span>
                </div>
                <p class="settings-model-fixed" data-model-source="${escapeHtml(source)}">${escapeHtml(
                    targetModelText(source),
                )}</p>
                <p class="settings-model-note">跟随目标会话模型；Sidecar 不支持覆盖模型。</p>
                ${capability.reason ? `<p class="settings-model-note">${escapeHtml(capability.reason)}</p>` : ""}
            </div>`;
    }
    return `
        <div class="settings-model" data-model-section>
            <div class="settings-model-head">
                <strong>模型选择</strong>
                <span class="capability-badge off">不可用</span>
            </div>
            <p class="settings-model-note">${escapeHtml(capability.reason || "当前桥接不支持模型选择。")}</p>
        </div>`;
}

function bindModelSelectionControls(detail) {
    const mode = detail.querySelector("[data-model-mode]");
    const modelId = detail.querySelector("[data-model-id]");
    if (!mode || !modelId) return;
    const sync = () => {
        modelId.disabled = mode.value !== "explicit";
    };
    mode.addEventListener("change", sync);
    sync();
}

function renderBridgeDetail(source) {
    const detail = $("#bridge-detail");
    if (!detail) return;
    const bridge = state.bridges[source] || {};
    const health = healthFor(source);
    const enabledHtml = `
        <label class="settings-enabled">
            <input type="checkbox" data-field="enabled" ${bridge.enabled === false ? "" : "checked"}>
            <span>启用</span>
        </label>`;
    const readOnlyHtml = `
        <label class="settings-check">
            <input type="checkbox" data-field="read_only" ${bridge.read_only === true ? "checked" : ""}>
            <span>只读桥接</span>
            <small>开启后禁止向该来源发送内容</small>
        </label>`;
    detail.innerHTML = `
        <div class="settings-section" data-source="${escapeHtml(source)}">
            <div class="settings-section-head">
                ${sourceBadge(source)}<strong>${escapeHtml(sourceLabel(source))}</strong>
                <span class="settings-source-id">${escapeHtml(source)}</span>
                ${enabledHtml}
            </div>
            <div class="settings-badges">
                ${healthBadge(source)}
                <span class="capability-list">${capabilityBadges(source)}</span>
            </div>
            ${health.message ? `<p class="settings-health-message">${escapeHtml(health.message)}</p>` : ""}
            ${modelSelectionHtml(source, bridge)}
            <div class="settings-grid">${bridgeFieldsHtml(source, bridge)}</div>
            ${readOnlyHtml}
        </div>`;
    bindModelSelectionControls(detail);
}

function captureBridgeForm() {
    const section = settingsBody.querySelector(".settings-section");
    if (!section) return null;
    const source = section.dataset.source;
    const bridge = state.bridges[source] || {};
    const enabled = section.querySelector('[data-field="enabled"]');
    const readOnly = section.querySelector('[data-field="read_only"]');
    bridge.enabled = Boolean(enabled && enabled.checked);
    bridge.read_only = Boolean(readOnly && readOnly.checked);
    const modelMode = section.querySelector("[data-model-mode]");
    if (modelMode) {
        const modelId = section.querySelector("[data-model-id]");
        const mode = modelMode.value === "explicit" ? "explicit" : "bridge_default";
        const value = modelId ? modelId.value.trim() : "";
        if (mode === "explicit" && !value) {
            throw new Error(`${source} 指定模型时必须填写模型 ID`);
        }
        bridge.model_selection = {
            mode,
            model_id: mode === "explicit" ? value : "",
        };
    }
    section.querySelectorAll('input[data-field]:not([type="checkbox"])').forEach((input) => {
        const field = input.dataset.field;
        const value = input.value.trim();
        if (!value) {
            if (field === "auth_ref") bridge[field] = "";
            else delete bridge[field];
            return;
        }
        if (field === "timeout_ms" || field === "retry_count") {
            const number = Number(value);
            const minimum = field === "retry_count" ? 0 : 1;
            if (!Number.isFinite(number) || number < minimum || !Number.isInteger(number)) {
                throw new Error(`${source} 的 ${field} 必须是${minimum === 0 ? "非负" : "正"}整数`);
            }
            bridge[field] = number;
            return;
        }
        bridge[field] = value;
    });
    state.bridges[source] = bridge;
    return source;
}

function renderSettings(bridges) {
    const sources = Object.keys(bridges);
    if (!sources.length) {
        settingsBody.innerHTML = '<p class="session-empty muted">暂无桥接配置</p>';
        return;
    }
    if (!sources.includes(state.selectedBridgeSource)) {
        state.selectedBridgeSource = sources.includes(state.source) ? state.source : sources[0];
    }
    const options = sources.map((source) => {
        const label = bridgeOptionLabel(source, bridges[source] || {});
        return `<option value="${escapeHtml(source)}" ${source === state.selectedBridgeSource ? "selected" : ""}>${escapeHtml(label)}</option>`;
    }).join("");
    settingsBody.innerHTML = `
        <div class="settings-picker">
            <label class="settings-picker-label" for="bridge-selector">桥接来源</label>
            <select id="bridge-selector" class="bridge-selector">${options}</select>
            <span class="settings-picker-hint">${sources.length} 个桥接 · 选择后查看详细配置</span>
        </div>
        <div id="bridge-detail"></div>`;
    $("#bridge-selector").addEventListener("change", (event) => {
        try {
            captureBridgeForm();
        } catch (error) {
            showToast(error.message);
            event.target.value = state.selectedBridgeSource;
            return;
        }
        state.selectedBridgeSource = event.target.value;
        renderBridgeDetail(state.selectedBridgeSource);
    });
    renderBridgeDetail(state.selectedBridgeSource);
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const data = await response.json();
    if (!response.ok) {
        const error = new Error(data.error || `请求失败 (${response.status})`);
        error.code = data.error_code || "";
        error.status = response.status;
        throw error;
    }
    return data;
}

function applyBridgeConfig(data) {
    if (data && data.bridges && typeof data.bridges === "object") {
        state.bridges = data.bridges;
    }
    updateSourceUi();
}

function applyHealth(data) {
    if (data && data.bridges && typeof data.bridges === "object") {
        state.health = data.bridges;
        if (!Object.keys(state.bridges).length) {
            const configLike = Object.fromEntries(
                Object.entries(data.bridges).filter(([, bridge]) => (
                    bridge && (bridge.capabilities || bridge.label || bridge.adapter)
                )),
            );
            if (Object.keys(configLike).length) state.bridges = configLike;
        }
    }
    if (data && data.model) {
        state.model = data.model;
        $("#model-label").textContent = `润色模型 · ${data.model}`;
    }
    updateSourceUi();
}

async function loadBridgeStatus({ renderConfig = false } = {}) {
    const [configResult, healthResult] = await Promise.allSettled([
        fetchJson("/api/config"),
        fetchJson("/api/health"),
    ]);
    if (configResult.status === "fulfilled") applyBridgeConfig(configResult.value);
    if (healthResult.status === "fulfilled") applyHealth(healthResult.value);
    if (healthResult.status === "rejected" && !state.model) {
        $("#model-label").textContent = "润色模型 · 不可用";
    }

    if (renderConfig) {
        if (Object.keys(state.bridges).length) {
            renderSettings(state.bridges);
        } else {
            settingsBody.innerHTML = '<p class="session-empty muted">读取配置失败，请检查本地服务。</p>';
        }
    }
    return {
        configError: configResult.status === "rejected" ? configResult.reason : null,
        healthError: healthResult.status === "rejected" ? healthResult.reason : null,
    };
}

async function openSettingsModal() {
    settingsModal.classList.remove("hidden");
    settingsBody.innerHTML = '<p class="session-empty muted">正在读取配置...</p>';
    const result = await loadBridgeStatus({ renderConfig: true });
    if (result.configError && !Object.keys(state.bridges).length) {
        showToast(result.configError.message || "读取配置失败");
    }
}

function closeSettingsModal() {
    settingsModal.classList.add("hidden");
}

async function saveSettings() {
    try {
        captureBridgeForm();
    } catch (error) {
        showToast(error.message || "配置不合法");
        return;
    }
    const bridges = {};
    Object.entries(state.bridges).forEach(([source, bridge]) => {
        const entry = {
            enabled: bridge.enabled !== false,
            read_only: bridge.read_only === true,
        };
        BRIDGE_FIELDS.forEach(({ key }) => {
            if (bridge[key] !== undefined && bridge[key] !== "") entry[key] = bridge[key];
        });
        if (bridge.model_selection && typeof bridge.model_selection === "object") {
            entry.model_selection = {
                mode: bridge.model_selection.mode === "explicit" ? "explicit" : "bridge_default",
                model_id: String(bridge.model_selection.model_id || ""),
            };
        }
        if (bridge.auth_ref === "") entry.auth_ref = "";
        bridges[source] = entry;
    });
    if (!Object.keys(bridges).length) {
        showToast("没有可保存的桥接配置");
        return;
    }
    const saveButton = $("#settings-save");
    saveButton.disabled = true;
    try {
        const data = await fetchJson("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ bridges }),
        });
        if (data.ok === false) throw new Error(data.error || "保存失败");
        closeSettingsModal();
        showToast("桥接设置已保存并生效");
        const status = await loadBridgeStatus();
        if (status.configError) applyBridgeConfig(data);
        await loadSessions();
    } catch (error) {
        showToast(error.message || "保存失败");
    } finally {
        saveButton.disabled = false;
    }
}

async function refreshSessions() {
    try {
        const data = await fetchJson("/api/sessions");
        const sessions = Array.isArray(data.sessions) ? data.sessions : [];
        state.sessions = sessions.sort(
            (left, right) => Number(right.recency_at || 0) - Number(left.recency_at || 0),
        );
    } catch {
        // Keep the last successful list while the local service reconnects.
    }
    renderSessionList();
}

let sessionsLoadPromise = null;

async function loadSessions() {
    if (isAgentXRayMode()) return;
    if (sessionsLoadPromise) return sessionsLoadPromise;
    state.sessionsLoading = true;
    renderSessionList();
    sessionsLoadPromise = refreshSessions().finally(() => {
        state.sessionsLoading = false;
        sessionsLoadPromise = null;
        renderSessionList();
    });
    return sessionsLoadPromise;
}

async function loadContext(sessionId = null, source = null) {
    const generation = ++contextLoadGeneration;
    const requestedSessionId = cleanSessionId(sessionId);
    const wantedSource = source || state.source || "";
    const previousSessionId = state.sessionId;
    applySessionModel();
    renderTargetModel();
    contextMeta.textContent = "正在读取...";
    contextPreview.textContent = "正在读取当前会话...";
    const params = new URLSearchParams();
    if (isAgentXRayMode()) {
        params.set("mode", "agentxray");
        params.set("handoff_id", state.handoff?.handoff_id || "");
    }
    if (wantedSource) params.set("source", wantedSource);
    if (requestedSessionId) params.set("session_id", requestedSessionId);
    const suffix = params.size ? `?${params.toString()}` : "";
    try {
        const data = await fetchJson(`/api/context${suffix}`);
        if (generation !== contextLoadGeneration) return;
        if (isAgentXRayMode() && (data.source !== state.source || data.session_id !== state.sessionId)) {
            throw new Error("上下文目标与锁定会话不一致");
        }
        state.context = data.context || "";
        state.source = data.source || wantedSource || "";
        const nextSessionId = cleanSessionId(data.session_id) || requestedSessionId || "";
        if (previousSessionId && nextSessionId && previousSessionId !== nextSessionId) {
            draftEl.value = "";
            $("#draft-count").textContent = "0 字";
            setResult("");
            statusBadge.className = "status-badge";
            statusBadge.textContent = "等待润色";
        }
        state.sessionId = nextSessionId;
        applySessionModel(data);
        updateSourceUi();
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
            "file-log": "会话日志绑定",
        };
        const method = methodLabels[data.binding_method]
            || humanizeIdentifier(data.binding_method)
            || "自动绑定";
        contextMeta.textContent = `${method} · ${data.message_count} 条消息 · ${data.char_count} 字`;
        contextPreview.textContent = data.context;
        const title = data.session_title || data.cwd || "未命名会话";
        bindingNote.textContent = `项目：${projectName(data.cwd)} · session ${shortId(state.sessionId)}`;
        bindingNote.title = data.cwd || "";
        const url = new URL(window.location.href);
        if (state.source) url.searchParams.set("source", state.source);
        else url.searchParams.delete("source");
        url.searchParams.set("session_id", state.sessionId);
        history.replaceState(null, "", url);
        await loadSessions();
        if (generation !== contextLoadGeneration) return;
    } catch (error) {
        if (generation !== contextLoadGeneration) return;
        contextMeta.textContent = "读取失败";
        contextPreview.textContent = String(error);
        bindingNote.textContent = "读取失败，请检查本地服务。";
    }
}

function bindSession(source, sessionId) {
    if (isAgentXRayMode()) return;
    state.source = source;
    state.sessionId = sessionId;
    state.effectiveModel = "";
    state.effectiveModelSource = "";
    applySessionModel();
    renderEffectiveModel();
    updateSourceUi();
    loadContext(sessionId, source);
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
        const data = await fetchJson("/api/polish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...integrationRequestFields(),
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
        if (data.ok === false) throw new Error(data.error || "润色失败");
        state.continuation = null;
        continuationPanel.classList.add("hidden");
        renderDecisionPanel({status: "ready"});
        setResultPanel("analysis");
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

async function continueTask() {
    if (state.busy) return;
    setBusy(true);
    emptyState.classList.add("hidden");
    try {
        const data = await fetchJson("/api/next-step", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...integrationRequestFields(),
                source: state.source,
                session_id: state.sessionId,
                context: state.context,
                handoff_packet: handoffPacket.value.trim() || null,
            }),
        });
        if (data.ok === false) throw new Error(data.error || "下一步分析失败");
        renderContinuation(data);
        setResult(data.final_prompt || "");
        setResultPanel(
            decisionItems(data).length || data.status === "needs_user_decision"
                ? "decision"
                : "analysis",
        );
        if (!data.final_prompt) {
            statusBadge.className = "status-badge";
            statusBadge.textContent = continuationStatusLabel(data.status);
        }
        showToast(data.status === "insufficient_context" ? "上下文不足，请补充信息" : "下一步建议已生成，请确认后发送");
    } catch (error) {
        emptyState.classList.remove("hidden");
        statusBadge.className = "status-badge";
        statusBadge.textContent = "失败";
        showToast(error.message || "下一步分析失败");
    } finally {
        setBusy(false);
    }
}

async function sendToCurrentSession() {
    if (state.integrationSending || (isAgentXRayMode() && state.integrationDelivery)) return;
    if (!state.result || !state.sessionId) {
        showToast(state.sessionId ? "先生成润色结果" : "请先绑定会话");
        return;
    }
    if (isAgentXRayMode()) {
        if (!state.handoff) {
            showToast("未加载 AgentXRay handoff");
            return;
        }
        if (isHandoffExpired()) {
            state.integrationExpired = true;
            showToast("handoff 已过期，请从 AgentXRay 重新打开");
            return;
        }
    }
    const capabilities = capabilitiesFor(state.source);
    if (!isAgentXRayMode() && !capabilities.send) {
        showToast("当前桥接未声明发送能力");
        return;
    }
    if (!isAgentXRayMode() && !capabilities.send_modes.includes(state.sendMode)) {
        showToast("当前桥接不支持所选发送方式");
        return;
    }
    sendButton.disabled = true;
    if (isAgentXRayMode()) {
        state.integrationSendKey ||= crypto.randomUUID();
        state.integrationSending = true;
        state.integrationDelivery = "sending";
        saveIntegrationAttempt();
    }
    const previousStatus = statusBadge.textContent;
    const requestSource = state.source;
    const requestSessionId = state.sessionId;
    statusBadge.className = "status-badge loading";
    statusBadge.textContent = "发送中";
    try {
        const data = await fetchJson("/api/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...integrationRequestFields(),
                session_id: state.sessionId,
                message: state.result,
                source: state.source,
                mode: state.sendMode,
                ...(isAgentXRayMode() ? {
                    mode: "handoff", confirmed: true, idempotency_key: state.integrationSendKey,
                } : {}),
            }),
        });
        if (data.ok === false) throw new Error(data.error || "发送失败");
        if (isAgentXRayMode()) {
            if (data.accepted !== true || !data.receipt_id) {
                const error = new Error("未收到明确回执，请返回 AgentXRay 核对");
                error.code = "delivery_unknown";
                throw error;
            }
            state.integrationDelivery = "sent";
            saveIntegrationAttempt();
        }
        if (state.source !== requestSource || state.sessionId !== requestSessionId) return;
        state.effectiveModel = String(data.effective_model || "unknown").trim();
        state.effectiveModelSource = state.source;
        renderEffectiveModel();
        statusBadge.className = "status-badge ready";
        statusBadge.textContent = "已发送";
        const sentMode = data.mode || state.sendMode;
        const modeLabel = sentMode === "steer"
            ? "已插入当前轮"
            : sentMode === "queue"
              ? "已排队"
              : `已通过${sendModeLabel(sentMode)}发送`;
        const effectiveModel = data.effective_model
            ? ` · ${effectiveModelLabel(data.effective_model, capabilities.model_selection)}`
            : "";
        showToast(
            data.queue_id
                ? `已发送到当前会话 · ${data.queue_id}${effectiveModel}`
                : `已发送到当前会话 · ${modeLabel}${effectiveModel}`,
        );
    } catch (error) {
        if (isAgentXRayMode()) {
            const uncertain = !error.status || ["delivery_unknown", "delivery_in_progress"].includes(error.code);
            state.integrationDelivery = uncertain ? "delivery_unknown" : "";
            if (["handoff_expired", "expired", "stale_context", "session_closed", "target_not_found",
                "target_mismatch", "idempotency_conflict"].includes(error.code)) {
                state.integrationExpired = true;
            }
            saveIntegrationAttempt();
        }
        statusBadge.textContent = previousStatus;
        showToast(error.message || "发送失败");
    } finally {
        state.integrationSending = false;
        if (isAgentXRayMode()) renderIntegrationState();
        updateSendControls();
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
        showToast(copied ? "结果已复制，可粘贴到目标会话" : "复制失败，请手动选择结果");
    }
    return copied;
}

function restoreOriginal() {
    if (!draftEl.value) return;
    setResult(draftEl.value);
    showToast("已恢复为原始草稿");
}

polishButton.addEventListener("click", polish);
continueButton.addEventListener("click", continueTask);
repolishButton.addEventListener("click", polish);
sendButton.addEventListener("click", sendToCurrentSession);
copyButton.addEventListener("click", () => copyResult());
restoreButton.addEventListener("click", restoreOriginal);
analysisPanelTab.addEventListener("click", () => setResultPanel("analysis"));
decisionPanelTab.addEventListener("click", () => setResultPanel("decision"));
$("#refresh-context").addEventListener("click", () => {
    loadContext(state.sessionId || null, state.source);
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
$("#refresh-sessions").addEventListener("click", async () => {
    await loadSessions();
    showToast("会话列表已刷新");
});
window.addEventListener("focus", () => {
    loadSessions();
    loadBridgeStatus();
});
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        loadSessions();
        loadBridgeStatus();
    }
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
resultEl.addEventListener("input", () => {
    state.result = resultEl.value;
    copyButton.disabled = !state.result;
    restoreButton.disabled = !state.result;
    updateSendControls();
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
const urlParams = new URLSearchParams(window.location.search);
state.integrationMode = urlParams.get("mode") === "agentxray" ? "agentxray" : "standalone";
if (isAgentXRayMode()) {
    state.sendMode = "handoff";
    ["#open-session-modal", "#refresh-sessions", "#open-settings-modal"].forEach((selector) => {
        const element = $(selector);
        if (element) element.disabled = true;
    });
    renderIntegrationState();
}
updateSourceUi();
loadBridgeStatus();
if (isAgentXRayMode()) {
    const handoffId = urlParams.get("handoff_id");
    fetchJson(`/api/agentxray/intervention?${new URLSearchParams({
        handoff_id: handoffId || "",
        agentxray_url: urlParams.get("agentxray_url") || "",
    })}`)
        .then((data) => {
            applyImportedHandoff(data.handoff);
            updateSourceUi();
            return loadContext(state.sessionId, state.source);
        })
        .catch((error) => {
            renderIntegrationState();
            showToast(error.message || "AgentXRay handoff 加载失败");
        });
} else {
    loadSessions();
    loadContext(cleanSessionId(urlParams.get("session_id")), urlParams.get("source"));
}

window.addEventListener("pagehide", () => {
    if (isAgentXRayMode() && state.handoff) saveIntegrationAttempt();
});
