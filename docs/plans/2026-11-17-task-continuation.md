# Task Continuation Implementation Plan

> **For Codex:** Execute this plan in the current worktree while preserving unrelated changes.

**Goal:** Add a user-confirmed “next step / continue task” flow that analyzes the bound session, optional AgentXRay handoff packet, and recent evidence, then produces an editable task prompt without sending automatically.

**Architecture:** Add a server-side `/api/next-step` endpoint. The endpoint validates the optional handoff packet, loads the explicit bridge context, asks the local model for a strict JSON recommendation, normalizes the result, detects missing context and session reference conflicts, and deterministically renders the final task prompt. The existing `/api/send` route remains the only delivery path. The frontend adds a continuation entry point, recommendation preview, optional packet input, editable prompt output, copy action, and existing bridge send controls.

**Tech Stack:** Python 3.13+, `unittest`, existing local OpenAI-compatible Responses client, vanilla JavaScript/CSS/HTML.

---

### Task 1: Establish the continuation contract and baseline fix

**Files:**
- Modify: `server.py`
- Modify: `tests/test_server.py`

**Steps:**
1. Define the handoff packet fields, continuation recommendation shape, allowed statuses, and deterministic prompt sections.
2. Make `send_workbuddy_local_prompt` return `effective_model` so the current model-selection baseline passes.
3. Add packet validation, conflict checks, recommendation normalization, prompt rendering, and model prompt construction helpers.
4. Add focused tests for packet validation, context insufficiency, completed tasks, pending work, user decisions, out-of-scope suggestions, conflict detection, and final prompt sections.
5. Run the focused unit tests and fix failures before adding the route.

### Task 2: Add the next-step API

**Files:**
- Modify: `server.py`
- Modify: `tests/test_server.py`

**Steps:**
1. Add `GET`-independent `POST /api/next-step` request handling with explicit source/session binding and optional supplied context.
2. Return a stable JSON contract containing status, missing information, objective, completed evidence, pending work, blockers, primary recommendation, up to two alternatives, conflicts, and the rendered prompt.
3. Short-circuit with a clear insufficiency response when no usable session or packet context exists.
4. Add handler tests by patching model calls and bridge context loading; verify the endpoint never sends to a bridge.
5. Run all server tests.

### Task 3: Add continuation UI and preserve send boundaries

**Files:**
- Modify: `index.html`
- Modify: `app.js`
- Modify: `styles.css`
- Modify: `tests/test_server.py`

**Steps:**
1. Add a “下一步 / 继续任务” entry point and an optional AgentXRay handoff packet textarea under a collapsible debug section.
2. Render recommendation status, objective, completed evidence, pending work, blockers, conflict/missing notices, and primary/alternative recommendation tabs.
3. Put the selected rendered task prompt into the existing result textarea as editable content.
4. Keep copy and existing bridge send controls active only after the user reviews the editable prompt; show target session and selected send mode before delivery.
5. Add frontend contract tests for the new controls and API payload, then run the full test command and syntax checks.

### Task 4: Update user-facing documentation

**Files:**
- Modify: `README.md`

**Steps:**
1. Document the continuation flow, stable handoff packet shape, conflict behavior, and explicit user confirmation boundary.
2. Document the `/api/next-step` response at a high level without exposing credentials or claiming automatic task management.
3. Re-run the complete project validation command and record any environment-limited checks.
