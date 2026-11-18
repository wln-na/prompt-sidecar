const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
    const classes = new Set();
    return {
        textContent: "", innerHTML: "", value: "", disabled: false,
        dataset: {}, className: "", hidden: false,
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            toggle: (name, value) => value ? classes.add(name) : classes.delete(name),
            contains: (name) => classes.has(name),
        },
        addEventListener() {},
        querySelector: () => null,
        querySelectorAll: () => [],
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; },
        select() {},
    };
}

const elements = new Map();
const sandbox = vm.createContext({
    document: {
        querySelector(selector) {
            if (!elements.has(selector)) elements.set(selector, element());
            return elements.get(selector);
        },
        querySelectorAll: () => [],
        createElement: element,
    },
    URL, URLSearchParams,
    window: { location: { href: "http://127.0.0.1:8765/", pathname: "/" }, addEventListener() {} },
    history: { replaceState() {} },
    sessionStorage: {
        values: new Map(),
        getItem(key) { return this.values.get(key) || null; },
        setItem(key, value) { this.values.set(key, value); },
    },
    setTimeout, clearTimeout,
});
const app = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
vm.runInContext(app.slice(0, app.indexOf('polishButton.addEventListener("click"')), sandbox);
const run = (code) => vm.runInContext(code, sandbox);

run(`
    state.integrationMode = "agentxray";
    applyImportedHandoff({
        handoff_id: "h1", attention_id: "a1", source: "agentxray",
        user_request: "原始用户需求", reason: "等待用户决定", recommended_action: "确认方案",
        expires_at: "2099-01-01T00:00:00Z", target_fingerprint: "hidden",
        snapshot_version: "hidden", target: {
            platform: "claude-code", agent: null, session_id: "s1", cwd: null, title: "目标会话",
        }, session_refs: []
    });
`);
assert.equal(run("state.source"), "claude");
assert.equal(run("state.sessionId"), "s1");
assert.equal(run("draftEl.value"), "原始用户需求");
assert.match(run('integrationEvidence.textContent'), /等待用户决定/);
assert.doesNotMatch(run('integrationEvidence.textContent'), /hidden/);
assert.match(run('integrationQueryFields().toString()'), /handoff_id=h1/);
run('state.result = "最终提示词"; updateSendControls()');
assert.equal(run("sendButton.disabled"), false);
run('state.integrationDelivery = "sent"; updateSendControls()');
assert.equal(run("sendButton.disabled"), true);
assert.match(run("sendButton.textContent"), /已发送/);
console.log("AgentXRay integration frontend checks passed");
