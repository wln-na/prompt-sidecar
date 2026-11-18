# Model Selection Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the typed bridge model-selection contract so persisted configuration is validated against runtime capabilities and every send adapter receives a verified request object.

**Architecture:** Keep Sidecar's global polishing model separate from bridge target-model selection. Bridge adapters declare a typed capability; configuration stores only `bridge_default` or `explicit`; the send route reloads the bound bridge, validates the persisted choice, constructs `BridgeSendRequest`, and returns `effective_model`. Claude receives a dynamic override through `--model`; Codex queue remains fixed to the current thread model.

**Tech Stack:** Python 3.13, `TypedDict`, existing `unittest` suite, vanilla JavaScript frontend.

---

### Task 1: Complete typed bridge contracts

**Files:**
- Modify: `bridges/contracts.py`
- Modify: `bridges/__init__.py`
- Test: `tests/test_bridge_contracts.py`

**Steps:**
1. Add `BridgeSendRequest` with source, session ID, message, send mode, and validated model ID.
2. Change `SessionBridge.send` to accept the request object.
3. Export the new type.
4. Update the dummy bridge contract test and assert the request shape can carry an empty model ID for bridge default.
5. Run the focused contract tests.

### Task 2: Route verified requests through every adapter

**Files:**
- Modify: `server.py`
- Test: `tests/test_server.py`

**Steps:**
1. Import `BridgeSendRequest`.
2. Update each built-in bridge adapter to read its fields from the request object.
3. Keep Claude's validated model ID as the only dynamic CLI override.
4. Keep Codex rejecting a non-empty model ID and reporting `current-thread`.
5. Construct the request only after `resolve_bridge` and `model_plan_for_send` revalidate the persisted configuration.
6. Update adapter tests for the request signature and add coverage for fixed and dynamic effective models.
7. Run the focused model and bridge tests.

### Task 3: Verify API and frontend contract

**Files:**
- Modify: `tests/test_server.py`
- Modify: `README.md` only if the final public contract wording is stale

**Steps:**
1. Assert config responses retain `model_selection`.
2. Assert health responses expose typed model capabilities.
3. Assert invalid explicit selections fail for fixed and unsupported bridges.
4. Assert the send response contains `effective_model`.
5. Run the full unittest suite and record the existing zstd fixture limitation if the environment lacks `compression.zstd` or `zstandard`.

---
