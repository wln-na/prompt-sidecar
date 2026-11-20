const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
    return {
        textContent: "", innerHTML: "", value: "", dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        setAttribute() {},
        removeAttribute() {},
    };
}

function settingsHarness(fixture) {
    const elements = new Map();
    const sandbox = vm.createContext({
        document: {
            querySelector(selector) {
                if (!elements.has(selector)) elements.set(selector, element());
                return elements.get(selector);
            },
            createElement: element,
        },
        window: { location: { href: "http://localhost/", pathname: "/" } },
        history: { replaceState() {} },
        URL, URLSearchParams, setTimeout, clearTimeout,
        fixture,
    });
    const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
    vm.runInContext(app.slice(0, app.indexOf('polishButton.addEventListener("click"')), sandbox);
    const inputs = Object.entries(fixture.fields).map(([field, value]) => ({
        dataset: { field }, value,
    }));
    const section = {
        dataset: { source: fixture.source },
        querySelector(selector) {
            if (selector.includes('data-field="enabled"')) return { checked: true };
            if (selector.includes('data-field="read_only"')) return { checked: false };
            return null;
        },
        querySelectorAll: () => inputs,
    };
    elements.get("#settings-body").querySelector = () => section;
    return {
        run: (code) => vm.runInContext(code, sandbox),
        sandbox,
        elements,
    };
}

async function settingsPayload(fixture) {
    const { run, sandbox } = settingsHarness(fixture);
    run(`
        applyBridgeConfig({bridges: fixture.bridges});
        closeSettingsModal = () => {};
        loadBridgeStatus = async () => ({});
        loadSessions = async () => {};
        fetchJson = async (url, options) => {
            if (url !== "/api/config" || options.method !== "POST") {
                throw new Error("Unexpected settings request");
            }
            globalThis.savedPayload = JSON.parse(options.body);
            return {ok: true};
        };
        showToast = () => {};
    `);
    await run("saveSettings()");
    assert.ok(sandbox.savedPayload, "settings form must submit a config request");
    return JSON.parse(JSON.stringify(sandbox.savedPayload));
}

async function main() {
    if (process.argv.includes("--payload")) {
        const fixture = JSON.parse(fs.readFileSync(0, "utf8"));
        process.stdout.write(JSON.stringify(await settingsPayload(fixture)));
        return;
    }
    const cases = [
        ["dsh", "dsh", "sessions_dir"],
        ["claude", "claude", "projects_dir"],
        ["workbuddy_local", "workbuddy", "db_path"],
        ["gemini", "gemini", "sessions_dir"],
        ["custom", "dsh", "token_file"],
    ];
    for (const [source, adapter, child] of cases) {
        const payload = await settingsPayload({
            source,
            bridges: { [source]: { adapter, [child]: "/fixture/explicit" } },
            fields: { home: "/fixture/new-home", [child]: "" },
        });
        assert.equal(payload.bridges[source][child], "", `${source} must submit path clearing`);
        assert.equal(payload.bridges[source].home, "/fixture/new-home");
    }
    const { run, elements } = settingsHarness({source: "codex", fields: {home: "/fixture/new"}});
    run(`
        var configOffline = true;
        var posted = [];
        showToast = () => {};
        renderSettings = () => { settingsBody.innerHTML = "settings rendered"; };
        loadSessions = async () => {};
        fetchJson = async (url, options = {}) => {
            if (options.method === "POST") {
                posted.push(JSON.parse(options.body));
                return {ok: true};
            }
            if (url === "/api/config") {
                if (configOffline) throw new Error("config offline");
                return {bridges: {codex: {adapter: "codex"}, dsh: {
                    adapter: "dsh", home: "/fixture/dsh", sessions_dir: "",
                }}};
            }
            return {bridges: {dsh: {
                adapter: "dsh", home: "/fixture/dsh", sessions_dir: "/fixture/dsh/sessions",
            }}};
        };
    `);
    await run("loadBridgeStatus({renderConfig: true})");
    assert.equal(run("Object.keys(state.bridges).length"), 0, "health must not become config");
    assert.equal(elements.get("#settings-save").disabled, true);
    await run("saveSettings()");
    assert.equal(run("posted.length"), 0, "config failure must block saving");
    run("configOffline = false");
    await run("loadBridgeStatus({renderConfig: true})");
    assert.equal(elements.get("#settings-save").disabled, false);
    await run("saveSettings()");
    assert.equal(run("posted.length"), 1, "successful retry must restore saving");
    assert.equal(run("posted[0].bridges.dsh.sessions_dir"), "");
    run("configOffline = true");
    await run("loadBridgeStatus({renderConfig: true})");
    await run("saveSettings()");
    assert.equal(run("posted.length"), 1, "failed refresh must block saving stale config");
    run(`
        var pendingConfigs = [];
        fetchJson = async (url) => {
            if (url === "/api/config") {
                return new Promise((resolve, reject) => pendingConfigs.push({resolve, reject}));
            }
            return {bridges: {}};
        };
        var olderStatus = loadBridgeStatus({renderConfig: true});
        var newerStatus = loadBridgeStatus();
        pendingConfigs[1].reject(new Error("new config request failed"));
    `);
    await run("newerStatus");
    assert.match(elements.get("#settings-body").innerHTML, /读取配置失败/);
    run('pendingConfigs[0].resolve({bridges: {dsh: {sessions_dir: "/fixture/stale"}}})');
    await run("olderStatus");
    assert.equal(run("state.configReady"), false, "old success must not override new failure");
    assert.equal(elements.get("#settings-save").disabled, true);
    await run("saveSettings()");
    assert.equal(run("posted.length"), 1, "old success must not reopen saving");
    run(`
        pendingConfigs = [];
        olderStatus = loadBridgeStatus({renderConfig: true});
        newerStatus = loadBridgeStatus();
        pendingConfigs[1].resolve({bridges: {codex: {adapter: "codex"}}});
    `);
    await run("newerStatus");
    run('pendingConfigs[0].reject(new Error("old request failed"))');
    await run("olderStatus");
    assert.equal(run("state.configReady"), true, "old failure must not override new success");
    assert.equal(elements.get("#settings-save").disabled, false);
    assert.doesNotMatch(elements.get("#settings-body").innerHTML, /读取配置失败/);
    console.log("Settings path form regression checks passed (5 adapter cases)");
    console.log("Settings config failure, retry and health isolation checks passed");
    console.log("Settings out-of-order config response checks passed");
}

if (require.main === module) main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
