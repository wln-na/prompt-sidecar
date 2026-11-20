const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
    const classes = new Set();
    return {
        textContent: "", innerHTML: "", dataset: {}, value: "", title: "",
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        appendChild() {},
    };
}

const elements = new Map();
const sandbox = vm.createContext({
    document: {
        querySelector(selector) {
            if (!elements.has(selector)) elements.set(selector, element());
            return elements.get(selector);
        },
        createElement: element,
    },
    URL, URLSearchParams,
    window: { location: { href: "http://localhost/", pathname: "/" } },
    history: { replaceState() {} },
    setTimeout, clearTimeout,
});
const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
vm.runInContext(app.slice(0, app.indexOf('polishButton.addEventListener("click"')), sandbox);
const run = (code) => vm.runInContext(code, sandbox);

(async () => {
    run(`
        state.source = "codex";
        state.sessionId = "session-one";
        state.health.codex = { capabilities: {
            read_context: true,
            context_mode: "full",
            model_selection: { mode: "fixed", fixed_model_id: "current-thread" },
        }};
        state.health.dsh = { capabilities: {
            read_context: true,
            context_mode: "best_effort",
            model_selection: { mode: "fixed", fixed_model_id: "session-default" },
        }};
        state.health.copilot = { capabilities: {
            read_context: true,
            context_mode: "metadata",
            model_selection: { mode: "unsupported" },
        }};
    `);
    const badges = run('capabilityBadges("codex")');
    assert.match(badges, /capability-badge on[^>]*>跟随会话模型/);
    assert.match(badges, /可读取会话内容/);
    assert.match(run('capabilityBadges("dsh")'), /可读取部分上下文/);
    assert.match(run('capabilityBadges("copilot")'), /仅会话元数据/);
    assert.match(badges, /跟随.*会话/);
    assert.equal(run('effectiveModelLabel("current-thread")'), "未知（跟随会话）");
    assert.equal(run('effectiveModelLabel("session-default")'), "未知（跟随会话）");
    assert.equal(run('effectiveModelLabel("")'), "未知");
    assert.equal(run('effectiveModelLabel("<synthetic>")'), "未知");

    run('renderTargetModel()');
    assert.match(elements.get("#target-model-label").textContent, /未知/);
    const settingsModel = element();
    settingsModel.dataset.modelSource = "codex";
    elements.get("#settings-body").querySelector = (selector) => (
        selector === "[data-model-source]" ? settingsModel : null
    );
    run(`
        applySessionModel({model_id: "gpt-evidence", model_source: "turn_context"});
        renderTargetModel();
    `);
    assert.match(elements.get("#target-model-label").textContent, /gpt-evidence.*最近记录/);
    assert.match(settingsModel.textContent, /gpt-evidence/);
    assert.match(run('modelSelectionHtml("codex", {})'), /gpt-evidence/);
    assert.doesNotMatch(run('modelSelectionHtml("dsh", {})'), /gpt-evidence/);
    assert.match(run('modelSelectionHtml("dsh", {})'), /未知/);
    assert.match(run('modelSelectionHtml("codex", {})'), /跟随会话/);
    run(`
        state.effectiveModel = "current-thread";
        renderEffectiveModel();
    `);
    assert.match(elements.get("#send-effective-model").textContent, /未知（跟随会话）/);
    assert.equal(elements.get("#send-effective-model").dataset.modelId, undefined);
    run(`
        state.effectiveModel = "actual-model-id";
        renderEffectiveModel();
    `);
    assert.match(elements.get("#send-effective-model").textContent, /actual-model-id/);
    assert.equal(elements.get("#send-effective-model").dataset.modelId, "actual-model-id");
    run(`
        state.source = "codex";
        state.sessionId = "session-old";
        state.result = "message";
        state.sendMode = "queue";
        state.health.codex.capabilities.send = true;
        state.health.codex.capabilities.send_modes = ["queue"];
        fetchJson = async () => ({ok: true, effective_model: "old-model"});
        var oldSend = sendToCurrentSession();
        state.sessionId = "session-new";
    `);
    await run('oldSend');
    assert.notEqual(run('state.effectiveModel'), "old-model");
    run('applyHealth({model: "polish-only"})');
    assert.match(elements.get("#model-label").textContent, /润色模型.*polish-only/);
    assert.doesNotMatch(elements.get("#target-model-label").textContent, /polish-only/);

    run(`
        loadSessions = async () => {};
        openSessionModal = () => {};
        fetchJson = async () => ({source: "codex", session_id: "session-two", available: false});
    `);
    await run('loadContext("session-two", "codex")');
    assert.match(elements.get("#target-model-label").textContent, /未知/);
    assert.match(settingsModel.textContent, /未知/);
    assert.doesNotMatch(elements.get("#target-model-label").textContent, /gpt-evidence/);
    run('applySessionModel({model_id: "another-model", model_source: "turn_context"})');
    run('fetchJson = async () => { throw new Error("offline"); }');
    await run('loadContext("session-two", "codex")');
    assert.match(elements.get("#target-model-label").textContent, /未知/);
    run(`
        var finishOldContext;
        fetchJson = () => new Promise((resolve) => { finishOldContext = resolve; });
        var oldContext = loadContext("session-old", "codex");
        fetchJson = async () => ({
            source: "codex", session_id: "session-new", model_id: "new-model",
            model_source: "turn_context", available: false,
        });
    `);
    await run('loadContext("session-new", "codex")');
    run(`finishOldContext({
        source: "codex", session_id: "session-old", model_id: "old-model",
        model_source: "turn_context", available: false,
    })`);
    await run('oldContext');
    assert.match(elements.get("#target-model-label").textContent, /new-model/);
    assert.doesNotMatch(elements.get("#target-model-label").textContent, /old-model/);
    run(`applySessionModel({model_id: "current-thread", model_source: "turn_context"});
        renderTargetModel();`);
    assert.match(elements.get("#target-model-label").textContent, /未知/);
    console.log("Frontend model display regression checks passed");
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
