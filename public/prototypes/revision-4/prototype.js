"use strict";

// Isolated design demo: no application APIs or real message delivery.
const byId = (id) => document.getElementById(id);
const sample = "帮我梳理本地 Agent 中枢架构，找出当前实现的缺口，并给出下一步建议。";
let originalResult = "";
let toastTimer;

function toast(message) {
    byId("toast").textContent = message;
    byId("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { byId("toast").hidden = true; }, 2600);
}

function info(title, body) {
    byId("info-title").textContent = title;
    byId("info-body").textContent = body;
    byId("info-modal").showModal();
}

function showState(state, polished = false) {
    byId("preview-state").value = state;
    byId("empty-state").hidden = state !== "default";
    byId("generated-state").hidden = state === "default";
    byId("decision").hidden = state !== "decision";
    byId("analysis").hidden = polished;
    if (state === "default") {
        byId("result").value = "";
        byId("send").disabled = true;
        return;
    }
    byId("next-step").textContent = state === "decision"
        ? "先确认接入方向，再核对接口，避免错误假设带来返工。"
        : "先核对现有入口与调用链，再明确模块边界，避免基于错误假设返工。";
    const draft = byId("draft").value.trim() || sample;
    originalResult = state === "decision"
        ? "请先确认调度模块的接入方式：采用独立调度器，还是沿用现有入口？\n确认后再细化模块边界与验证方案。"
        : polished
            ? `${draft}\n\n请基于可核对的证据，给出明确结论、具体行动与验证方式；信息不足时先说明需要补充的内容。`
            : "请基于现有代码核对会话读取与消息投递链路，整理模块职责、调用关系及异常处理缺口。\n给出建议的模块边界与最小验证方案，并说明每一步的依据。";
    byId("result").value = originalResult;
    byId("send").disabled = false;
}

async function copyResult() {
    try {
        await navigator.clipboard.writeText(byId("result").value);
        toast("已复制提示词");
    } catch {
        toast("请选中右侧提示词手动复制");
    }
}

function polish() {
    if (!byId("draft").value.trim()) {
        byId("draft").focus();
        toast("先写下草稿，再润色");
        return;
    }
    showState("result", true);
    if (byId("auto-copy").checked) copyResult();
}

byId("preview-state").addEventListener("change", (event) => {
    if (event.target.value !== "default" && !byId("draft").value.trim()) {
        byId("draft").value = sample;
    }
    showState(event.target.value);
});
byId("draft").addEventListener("keydown", (event) => {
    if (!event.isComposing && (event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        polish();
    }
});
byId("sample").addEventListener("click", () => { byId("draft").value = sample; });
byId("polish").addEventListener("click", polish);
byId("repolish").addEventListener("click", polish);
byId("analyze").addEventListener("click", () => showState("result"));
byId("restore").addEventListener("click", () => { byId("result").value = originalResult; });
byId("copy").addEventListener("click", copyResult);
byId("result").addEventListener("input", () => {
    byId("send").disabled = !byId("result").value.trim();
});
byId("send").addEventListener("click", () => {
    info("发送预览", "这是独立原型，未发送真实消息。\n\n实际使用时，将按所选方式发送你在右侧确认的文本。");
});
document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
        document.querySelectorAll("[data-mode]").forEach((item) => {
            item.setAttribute("aria-pressed", String(item === button));
        });
    });
});
for (const id of ["style", "structure"]) {
    byId(id).addEventListener("input", () => {
        byId("rule-summary").textContent = `${byId("style").value} · ${byId("structure").value}`;
    });
}
byId("switch-session").addEventListener("click", () => byId("session-modal").showModal());
byId("session-search").addEventListener("input", (event) => {
    document.querySelectorAll("[data-title]").forEach((button) => {
        button.hidden = !button.textContent.toLowerCase().includes(event.target.value.toLowerCase());
    });
});
document.querySelectorAll("[data-title]").forEach((button) => {
    button.addEventListener("click", () => {
        byId("session-title").textContent = button.dataset.title;
        byId("session-modal").close();
        showState("default");
    });
});
byId("session-details").addEventListener("click", () => {
    info("会话详情", "此处集中查看会话 ID、上下文范围、目标模型与桥接状态，避免主界面重复显示。\n当前内容为设计示例。");
});
byId("settings").addEventListener("click", () => {
    info("设置", "润色模型、桥接连接等设置仍从这里进入。\n本原型只演示入口，不修改实际配置。");
});
showState("default");
