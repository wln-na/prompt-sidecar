const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const appPath = path.join(__dirname, "../frontend/app.js");
const app = fs.readFileSync(appPath, "utf8");
const html = fs.readFileSync(path.join(__dirname, "../frontend/index.html"), "utf8");

function element(initialClasses = "") {
    const classes = new Set(initialClasses.split(/\s+/).filter(Boolean));
    const listeners = new Map();
    return {
        value: "", textContent: "", innerHTML: "", title: "", dataset: {},
        disabled: false, hidden: false, checked: false, children: [],
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
            toggle(name, enabled = !classes.has(name)) {
                if (enabled) classes.add(name);
                else classes.delete(name);
                return enabled;
            },
        },
        addEventListener(type, listener) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(listener);
        },
        async dispatchEvent(event) {
            for (const listener of listeners.get(event.type) || []) {
                await listener({ target: this, ...event });
            }
        },
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; },
        querySelector: () => null,
        querySelectorAll: () => [],
        appendChild(child) { this.children.push(child); },
        focus() { this.focused = true; },
        select() {},
    };
}

function contextResponse(sessionId = "session-one", source = "codex") {
    return {
        available: true, source, session_id: sessionId,
        session_title: "独立面板任务", cwd: "/fixture/project", context: "已读取任务上下文",
        binding_method: "url-explicit", message_count: 3, char_count: 9,
        model_id: "session-model", model_source: "turn_context",
    };
}

async function harness({ search = "?source=codex&session_id=session-one", context } = {}) {
    const elements = new Map();
    for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
        const node = element(match[0].match(/\bclass="([^"]*)"/)?.[1]);
        node.checked = /\bchecked\b/.test(match[0]);
        node.disabled = /\bdisabled\b/.test(match[0]);
        elements.set(`#${match[1]}`, node);
    }
    const modes = ["quick", "structured", "agent", "system"].map((mode) => {
        const node = element(mode === "quick" ? "mode active" : "mode");
        node.dataset.mode = mode;
        return node;
    });
    const documentEvents = element();
    const windowEvents = element();
    const calls = [];
    const unexpected = [];
    const routes = new Map([
        ["GET /api/bootstrap", () => ({
            kind: "prompt-sidecar-local", version: "0.4.0", panel_mode: "hosted",
            api_base: "http://127.0.0.1:8765", local_only: true,
            capabilities: ["sessions", "context", "polish", "next_step", "send"],
        })],
        ["GET /api/config", () => ({ bridges: {
            codex: { adapter: "codex", read_only: false },
        } })],
        ["GET /api/health", () => ({
            model: "polish-model",
            bridges: { codex: { status: "available", capabilities: {
                list_sessions: true, read_context: true, send: true,
                context_mode: "full", send_modes: ["queue", "steer"],
                model_selection: { mode: "fixed", fixed_model_id: "current-thread" },
            } } },
        })],
        ["GET /api/sessions", () => ({ sessions: [
            { source: "codex", session_id: "session-one", title: "会话一", recency_at: 1 },
            { source: "codex", session_id: "session-two", title: "会话二", recency_at: 2 },
        ] })],
        ["GET /api/context", context || ((url) => contextResponse(
            url.searchParams.get("session_id") || "session-one",
            url.searchParams.get("source") || "codex",
        ))],
    ]);
    const location = new URL(`http://127.0.0.1:8765/${search}`);
    const sandbox = vm.createContext({
        URL, URLSearchParams,
        document: {
            querySelector: (selector) => elements.get(selector) || null,
            querySelectorAll: (selector) => selector === ".mode" ? modes : [],
            createElement: () => element(),
            addEventListener: (...args) => documentEvents.addEventListener(...args),
            visibilityState: "visible",
        },
        window: {
            location,
            addEventListener: (...args) => windowEvents.addEventListener(...args),
        },
        history: {
            replaceState(_state, _title, url) { location.href = new URL(url, location).href; },
        },
        setTimeout: () => 1,
        clearTimeout() {},
        async fetch(url, options) {
            const parsed = new URL(url, location);
            const call = { url: parsed, ...options };
            calls.push(call);
            const handler = routes.get(`${options.method || "GET"} ${parsed.pathname}`);
            if (!handler) {
                unexpected.push(call);
                throw new Error(`Unexpected request: ${parsed.pathname}`);
            }
            const body = await handler(parsed, options);
            return { ok: true, status: 200, json: async () => body };
        },
    });
    vm.runInContext(app, sandbox, { filename: appPath });
    const run = (code) => vm.runInContext(code, sandbox);
    const flush = async () => {
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(unexpected.length, 0, "only configured standalone APIs may be requested");
    };
    await flush();
    return {
        run, calls, routes, elements, location, modes, flush,
        requests: (pathname) => calls.filter((call) => call.url.pathname === pathname),
        click: (selector) => elements.get(selector).dispatchEvent({ type: "click" }),
    };
}

test("full standalone initialization keeps URL binding, capabilities and model evidence", async () => {
    const h = await harness({ search: "?mode=embedded&source=codex&session_id=session-one" });
    assert.deepEqual([...new Set(h.calls.map((call) => call.url.pathname))].sort(), [
        "/api/bootstrap", "/api/config", "/api/context", "/api/health", "/api/sessions",
    ]);
    const query = h.requests("/api/context")[0].url.searchParams;
    assert.deepEqual([...query], [["source", "codex"], ["session_id", "session-one"]]);
    assert.equal(h.run("state.source"), "codex");
    assert.equal(h.run("state.sessionId"), "session-one");
    assert.equal(h.run("state.context"), "已读取任务上下文");
    assert.equal(h.run("state.configReady"), true);
    assert.equal(h.run("state.sendMode"), "queue");
    assert.equal(h.run("state.sessions[0].session_id"), "session-two");
    assert.match(h.elements.get("#context-meta").textContent, /URL 显式绑定/);
    assert.match(h.elements.get("#target-model-label").textContent, /session-model/);
    assert.match(h.elements.get("#model-label").textContent, /polish-model/);
    assert.match(h.elements.get("#connector-status").textContent, /已连接/);
    for (const selector of ["#open-session-modal", "#refresh-sessions", "#open-settings-modal"]) {
        assert.equal(h.elements.get(selector).disabled, false);
    }
    assert.equal(h.elements.get("#send-button").disabled, true);
});

test("hosted panel uses the loopback connector API from the URL", async () => {
    const h = await harness({
        search: "?api=http%3A%2F%2F127.0.0.1%3A9876&source=codex&session_id=session-one",
    });
    assert.equal(h.requests("/api/bootstrap")[0].url.origin, "http://127.0.0.1:9876");
    assert.equal(h.requests("/api/context")[0].url.origin, "http://127.0.0.1:9876");
    assert.match(h.elements.get("#connector-status").textContent, /已连接/);
});

test("unbound initialization opens the picker and explicit binding reloads context", async () => {
    const h = await harness({ search: "", context: () => ({
        available: false, requires_binding: true,
    }) });
    assert.equal(h.requests("/api/context")[0].url.search, "");
    assert.equal(h.elements.get("#session-modal").classList.contains("hidden"), false);
    assert.equal(h.elements.get("#session-filter").focused, true);
    assert.equal(h.run("state.sessionId"), "");
    h.routes.set("GET /api/context", (url) => contextResponse(url.searchParams.get("session_id")));
    h.run('bindSession("codex", "session-two")');
    await h.flush();
    assert.equal(h.run("state.sessionId"), "session-two");
    assert.equal(h.location.searchParams.get("session_id"), "session-two");
    assert.equal(h.location.searchParams.get("source"), "codex");
    assert.match(h.elements.get("#target-model-label").textContent, /session-model/);
});

test("polish uses the standalone payload and keeps results editable without sending", async () => {
    const h = await harness();
    h.routes.set("POST /api/polish", () => ({ ok: true, polished: "润色后的提示词", elapsed_ms: 12 }));
    h.elements.get("#draft").value = "  原始草稿  ";
    await h.modes[1].dispatchEvent({ type: "click" });
    h.elements.get("#output-style").value = "detailed";
    await h.elements.get("#output-style").dispatchEvent({ type: "change" });
    h.elements.get("#output-structure").value = "结论,验证";
    await h.elements.get("#output-structure").dispatchEvent({ type: "input" });
    await h.click("#polish-button");
    const [request] = h.requests("/api/polish");
    assert.deepEqual(JSON.parse(request.body), {
        draft: "原始草稿", mode: "structured", use_context: true,
        context: "已读取任务上下文", session_id: "session-one", source: "codex",
        output_style: "detailed", output_structure: "结论,验证",
    });
    assert.equal(request.headers["Content-Type"], "application/json");
    assert.equal(request.cache, "no-store");
    assert.equal(h.run("state.result"), "润色后的提示词");
    assert.equal(h.elements.get("#result").value, "润色后的提示词");
    assert.equal(h.elements.get("#send-button").disabled, false);
    assert.equal(h.run("state.busy"), false);
    assert.equal(h.requests("/api/send").length, 0);
    h.elements.get("#result").value = "手动修改结果";
    await h.elements.get("#result").dispatchEvent({ type: "input" });
    assert.equal(h.run("state.result"), "手动修改结果");
    await h.click("#restore-button");
    assert.equal(h.run("state.result"), "  原始草稿  ");
});

test("continue analyzes the bound session and requires explicit send", async () => {
    const h = await harness();
    h.routes.set("POST /api/next-step", () => ({
        ok: true, status: "ready", final_prompt: "继续执行任务", pending: ["补充验证"],
        recommendations: [{ title: "完成验证", prompt: "继续执行任务" }],
    }));
    await h.click("#continue-button");
    assert.deepEqual(JSON.parse(h.requests("/api/next-step")[0].body), {
        source: "codex", session_id: "session-one", context: "已读取任务上下文",
    });
    assert.equal(h.run("state.result"), "继续执行任务");
    assert.equal(h.run("state.continuation.status"), "ready");
    assert.equal(h.run("state.resultPanel"), "analysis");
    assert.equal(h.elements.get("#continuation-panel").classList.contains("hidden"), false);
    assert.match(h.elements.get("#continuation-panel").innerHTML, /session-one.*排队/s);
    assert.equal(h.requests("/api/send").length, 0);
    assert.equal(h.elements.get("#send-button").disabled, false);
});

test("continue shows decisions and leaves send disabled when no prompt is ready", async () => {
    const h = await harness();
    h.routes.set("POST /api/next-step", () => ({
        ok: true, status: "needs_user_decision", final_prompt: "",
        clarifying_questions: ["请选择验证范围"],
    }));
    await h.click("#continue-button");
    assert.equal(h.run("state.resultPanel"), "decision");
    assert.match(h.elements.get("#decision-content").innerHTML, /请选择验证范围/);
    assert.equal(h.elements.get("#send-button").disabled, true);
    assert.equal(h.requests("/api/send").length, 0);
});

for (const mode of ["queue", "steer"]) {
    test(`explicit ${mode} send uses standalone API and displays effective model`, async () => {
        const h = await harness();
        h.routes.set("POST /api/send", () => ({ ok: true, mode, effective_model: "execution-model" }));
        h.run('setResult("确认后的提示词")');
        const buttons = h.elements.get("#send-mode-group").children;
        await buttons.find((button) => button.dataset.sendMode === mode)
            .dispatchEvent({ type: "click" });
        assert.equal(h.requests("/api/send").length, 0);
        await h.click("#send-button");
        assert.deepEqual(JSON.parse(h.requests("/api/send")[0].body), {
            session_id: "session-one", message: "确认后的提示词", source: "codex", mode,
        });
        assert.equal(h.elements.get("#status-badge").textContent, "已发送");
        assert.equal(h.run("state.effectiveModel"), "execution-model");
        assert.match(h.elements.get("#send-effective-model").textContent, /execution-model/);
        assert.equal(h.elements.get("#send-button").disabled, false);
    });
}

test("send rejects absent binding, absent output and undeclared capabilities", async () => {
    const h = await harness();
    await h.run("sendToCurrentSession()");
    h.run('setResult("待发送提示词"); state.sessionId = ""');
    await h.run("sendToCurrentSession()");
    h.run('state.sessionId = "session-one"; state.health.codex.capabilities.send = false');
    await h.run("sendToCurrentSession()");
    h.run('state.health.codex.capabilities.send = true; state.sendMode = "unsupported"');
    await h.run("sendToCurrentSession()");
    assert.equal(h.requests("/api/send").length, 0);
    assert.match(h.elements.get("#toast").textContent, /不支持所选发送方式/);
});

test("failed send preserves the result and restores controls without retrying", async () => {
    const h = await harness();
    h.routes.set("POST /api/send", () => { throw new Error("fixture offline"); });
    h.run('setResult("保留的提示词")');
    await h.click("#send-button");
    await h.flush();
    assert.equal(h.requests("/api/send").length, 1);
    assert.equal(h.run("state.result"), "保留的提示词");
    assert.equal(h.elements.get("#send-button").disabled, false);
    assert.match(h.elements.get("#toast").textContent, /fixture offline/);
});

test("polish and continue failures release busy controls and retain previous output", async () => {
    const h = await harness();
    h.run('setResult("原有结果")');
    h.elements.get("#draft").value = "草稿";
    h.routes.set("POST /api/polish", () => ({ ok: false, error: "润色请求失败" }));
    h.routes.set("POST /api/next-step", () => { throw new Error("分析请求失败"); });
    await h.click("#polish-button");
    assert.match(h.elements.get("#toast").textContent, /润色请求失败/);
    await h.click("#continue-button");
    assert.match(h.elements.get("#toast").textContent, /分析请求失败/);
    assert.equal(h.run("state.busy"), false);
    assert.equal(h.run("state.result"), "原有结果");
    assert.equal(h.elements.get("#continue-button").disabled, false);
    assert.equal(h.elements.get("#polish-button").disabled, false);
    assert.equal(h.requests("/api/send").length, 0);
});

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((onResolve, onReject) => {
        resolve = onResolve;
        reject = onReject;
    });
    return { promise, resolve, reject };
}

for (const [source, sessionId] of [["codex", "session-two"], ["dsh", "session-one"]]) {
    test(`binding ${source}/${sessionId} clears output before context returns`, async () => {
        const h = await harness();
        const context = deferred();
        h.routes.set("GET /api/context", () => context.promise);
        h.elements.get("#draft").value = "旧会话草稿";
        h.run(`
            renderContinuation({status: "needs_user_decision", clarifying_questions: ["旧问题"]});
            setResultPanel("decision");
            setResult("旧会话结果");
            state.effectiveModel = "old-model";
        `);
        h.run(`bindSession(${JSON.stringify(source)}, ${JSON.stringify(sessionId)})`);
        assert.equal(h.run("state.result"), "");
        assert.equal(h.elements.get("#result").value, "");
        assert.equal(h.elements.get("#draft").value, "");
        assert.equal(h.run("state.context"), "");
        assert.equal(h.run("state.continuation"), null);
        assert.equal(h.run("state.effectiveModel"), "");
        assert.equal(h.run("state.resultPanel"), "analysis");
        assert.equal(h.elements.get("#continuation-panel").classList.contains("hidden"), true);
        assert.equal(h.elements.get("#decision-content").innerHTML, "");
        for (const selector of ["#send-button", "#copy-button", "#restore-button"]) {
            assert.equal(h.elements.get(selector).disabled, true);
        }
        context.reject(new Error("fixture context unavailable"));
        await h.flush();
        assert.equal(h.run("state.result"), "");
        assert.equal(h.elements.get("#send-button").disabled, true);
    });
}

for (const action of ["polish", "next-step"]) {
    for (const outcome of ["success", "failure"]) {
        test(`late ${action} ${outcome} is discarded after binding changes`, async () => {
            const h = await harness();
            const response = deferred();
            const selector = action === "polish" ? "#polish-button" : "#continue-button";
            h.elements.get("#draft").value = "旧任务草稿";
            h.routes.set(`POST /api/${action}`, () => response.promise);
            const request = h.click(selector);
            assert.equal(h.run("state.busy"), true);
            h.run('bindSession("codex", "session-two")');
            await h.flush();
            assert.equal(h.run("state.busy"), false);
            assert.equal(h.elements.get("#loading-overlay").classList.contains("hidden"), true);
            const status = h.elements.get("#status-badge").textContent;
            const toast = h.elements.get("#toast").textContent;
            if (outcome === "failure") response.reject(new Error("旧请求失败"));
            else response.resolve({
                ok: true, polished: "迟到润色", final_prompt: "迟到建议",
                status: "needs_user_decision", clarifying_questions: ["迟到问题"],
            });
            await request;
            assert.equal(h.run("state.result"), "");
            assert.equal(h.run("state.continuation"), null);
            assert.equal(h.elements.get("#decision-content").innerHTML, "");
            assert.equal(h.elements.get("#status-badge").textContent, status);
            assert.equal(h.elements.get("#toast").textContent, toast);
            assert.equal(h.elements.get("#send-button").disabled, true);
            assert.equal(h.requests("/api/send").length, 0);
        });
    }

    test(`late ${action} cannot overwrite a newer request after switching away and back`, async () => {
        const h = await harness();
        const oldResponse = deferred();
        const newResponse = deferred();
        const selector = action === "polish" ? "#polish-button" : "#continue-button";
        h.elements.get("#draft").value = "旧草稿";
        h.routes.set(`POST /api/${action}`, () => oldResponse.promise);
        const oldRequest = h.click(selector);
        h.run('bindSession("codex", "session-two")');
        await h.flush();
        h.run('bindSession("codex", "session-one")');
        await h.flush();
        h.elements.get("#draft").value = "新草稿";
        h.routes.set(`POST /api/${action}`, () => newResponse.promise);
        const newRequest = h.click(selector);
        assert.equal(h.requests(`/api/${action}`).length, 2);
        oldResponse.resolve({ok: true, polished: "旧结果", final_prompt: "旧结果"});
        await oldRequest;
        assert.equal(h.run("state.result"), "");
        assert.equal(h.run("state.busy"), true);
        assert.equal(h.elements.get(selector).disabled, true);
        newResponse.resolve({ok: true, polished: "新结果", final_prompt: "新结果"});
        await newRequest;
        assert.equal(h.run("state.result"), "新结果");
        assert.equal(h.run("state.busy"), false);
    });
}

test("context refresh on the same binding preserves editable output", async () => {
    const h = await harness();
    h.run('setResult("当前结果")');
    await h.run('loadContext("session-one", "codex")');
    assert.equal(h.run("state.result"), "当前结果");
});

test("pending send stays locked through mode changes, control refreshes and direct calls", async () => {
    const h = await harness();
    const response = deferred();
    h.routes.set("POST /api/send", () => response.promise);
    h.run('setResult("确认结果")');
    const send = h.click("#send-button");
    assert.equal(h.run("state.sending"), true);
    assert.equal(h.elements.get("#send-button").disabled, true);
    const modeButtons = h.elements.get("#send-mode-group").children.slice(-2);
    assert.equal(modeButtons.every((button) => button.disabled), true);
    await modeButtons.find((button) => button.dataset.sendMode === "steer")
        .dispatchEvent({type: "click"});
    assert.equal(h.run("state.sendMode"), "queue");
    h.run('setResult("编辑后的结果"); updateSourceUi()');
    assert.equal(h.elements.get("#send-button").disabled, true);
    await h.run("sendToCurrentSession()");
    assert.equal(h.requests("/api/send").length, 1);
    assert.equal(JSON.parse(h.requests("/api/send")[0].body).message, "确认结果");
    response.resolve({ok: true, mode: "queue"});
    await send;
    assert.equal(h.run("state.sending"), false);
    assert.equal(h.elements.get("#send-button").disabled, false);
    assert.equal(h.elements.get("#send-mode-group").children.slice(-2)
        .every((button) => !button.disabled), true);
});

test("failed send releases the lock for an explicit retry", async () => {
    const h = await harness();
    h.routes.set("POST /api/send", () => { throw new Error("fixture offline"); });
    h.run('setResult("确认结果")');
    await h.click("#send-button");
    assert.equal(h.run("state.sending"), false);
    h.routes.set("POST /api/send", () => ({ok: true}));
    await h.click("#send-button");
    assert.equal(h.requests("/api/send").length, 2);
    assert.equal(h.elements.get("#status-badge").textContent, "已发送");
});

test("binding change keeps a pending send locked and ignores its late failure", async () => {
    const h = await harness();
    const response = deferred();
    h.routes.set("POST /api/send", () => response.promise);
    h.run('setResult("旧结果")');
    const send = h.click("#send-button");
    h.run('bindSession("codex", "session-two")');
    await h.flush();
    h.run('setResult("新结果")');
    await h.run("sendToCurrentSession()");
    assert.equal(h.requests("/api/send").length, 1);
    const status = h.elements.get("#status-badge").textContent;
    response.reject(new Error("旧发送失败"));
    await send;
    assert.equal(h.elements.get("#status-badge").textContent, status);
    assert.doesNotMatch(h.elements.get("#toast").textContent, /旧发送失败/);
    assert.equal(h.run("state.sending"), false);
    assert.equal(h.elements.get("#send-button").disabled, false);
});

for (const readOnly of [true, undefined, false]) {
    test(`send requires writable config and capability (read_only=${readOnly})`, async () => {
        const h = await harness();
        h.routes.set("POST /api/send", () => ({ok: true}));
        h.run(`
            state.bridges.codex.read_only = ${readOnly};
            state.health.codex.read_only = false;
            setResult("待发送结果");
            renderSendModes();
        `);
        assert.equal(h.elements.get("#send-button").disabled, readOnly !== false);
        await h.run("sendToCurrentSession()");
        assert.equal(h.requests("/api/send").length, readOnly === false ? 1 : 0);
        h.run('state.health.codex.capabilities.send = false; updateSourceUi()');
        assert.equal(h.elements.get("#send-button").disabled, true);
        await h.run("sendToCurrentSession()");
        assert.equal(h.requests("/api/send").length, readOnly === false ? 1 : 0);
    });
}
