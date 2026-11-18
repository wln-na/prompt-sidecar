# AgentXRay Integration Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve Prompt Sidecar standalone behavior while adding an explicit AgentXRay integration mode that consumes a locked handoff and delegates confirmed sending back to AgentXRay.

**Architecture:** Keep the existing bridge registry and standalone `/api/send` path intact. Add an integration context validated by `handoff_id`, target fingerprint, snapshot version, and expiry. In integration mode, `/api/next-step` and `/api/polish` may use the supplied handoff context, while `/api/send` calls AgentXRay’s local handoff send endpoint and never chooses a different session.

**Tech Stack:** Python 3, `http.server`, existing bridge contracts, vanilla JavaScript UI, unittest.

---

### Task 1: Add integration handoff validation and state

**Files:** `server.py`, `bridges/contracts.py`, tests.

Normalize standalone/integration mode, validate required handoff fields, preserve object-shaped `session_refs`, enforce expiry and target identity, and return stable integration errors without storing credentials.

### Task 2: Add AgentXRay proxy send path

**Files:** `server.py`, `app.js`, tests.

Add configurable local AgentXRay endpoint and authenticated/request-scoped handoff send call. Keep direct bridge sending for standalone mode. Add idempotency key and forward delivery/lifecycle results.

### Task 3: Add integration UI entry and error states

**Files:** `index.html`, `app.js`, `styles.css`, tests.

Show locked Attention Inbox target, handoff expiry, evidence, and an explicit “发送到当前会话” state. Disable target/session switching in integration mode and preserve the optimized draft after send failure or stale-context rejection.

### Task 4: Verify compatibility

Run Sidecar unittest suites and targeted UI tests. Confirm standalone send behavior remains unchanged. Do not commit, push, or publish.
