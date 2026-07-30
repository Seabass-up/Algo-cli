"use strict";

const NEON_SCHEMA_VERSION = 1;
const NEON_PROTOCOL_VERSION = 1;
const NEON_EXTENSION_VERSION = "0.0.0";
const NEON_NATIVE_HOST = "com.algo_cli.neon";
const NEON_STATE_KEY = "neon_selected_tab_state";
const NEON_WORKER_GENERATION = crypto.randomUUID();
const NEON_COMMANDS = new Set(["status", "connect", "disconnect"]);

let nativePort = null;
let pendingNative = new Map();

function emptyState(reasonCode = "none") {
  return {
    schema_version: NEON_SCHEMA_VERSION,
    state: "disconnected",
    reason_code: reasonCode,
    worker_generation: NEON_WORKER_GENERATION,
  };
}

async function readState() {
  const row = await chrome.storage.session.get(NEON_STATE_KEY);
  const state = row[NEON_STATE_KEY];
  if (!state || state.worker_generation !== NEON_WORKER_GENERATION) {
    const fresh = emptyState(state ? "service_worker_restarted" : "none");
    await chrome.storage.session.set({ [NEON_STATE_KEY]: fresh });
    return fresh;
  }
  return state;
}

async function writeState(state) {
  await chrome.storage.session.set({ [NEON_STATE_KEY]: state });
  const observed = state.state === "observed";
  await chrome.action.setBadgeText({ text: observed ? "ON" : "" });
  await chrome.action.setBadgeBackgroundColor({ color: observed ? "#2e7d32" : "#666666" });
}

async function revoke(reasonCode) {
  for (const pending of pendingNative.values()) {
    pending.reject(new Error("native_session_revoked"));
  }
  pendingNative = new Map();
  if (nativePort) {
    try { nativePort.disconnect(); } catch (_error) { /* already disconnected */ }
  }
  nativePort = null;
  await writeState(emptyState(reasonCode));
}

function observeTopDocument() {
  const protocol = location.protocol;
  let surfaceKind = "dom";
  if (protocol !== "https:" && protocol !== "http:") {
    surfaceKind = "internal";
  } else if (document.contentType === "application/pdf") {
    surfaceKind = "pdf";
  } else if (document.querySelector('input[type="password"], input[autocomplete~="webauthn" i]')) {
    surfaceKind = document.querySelector('input[autocomplete~="webauthn" i]') ? "passkey" : "secure_field";
  } else if (document.querySelector('iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], iframe[src*="turnstile" i], [class*="captcha" i], [id*="captcha" i]')) {
    surfaceKind = "captcha";
  } else if (document.querySelector("canvas") && !document.querySelector("button, input, select, textarea, a[href]")) {
    surfaceKind = "canvas";
  }
  return {
    schema_version: 1,
    origin: location.origin,
    surface_kind: surfaceKind,
    content_type: String(document.contentType || "unknown").slice(0, 128),
    secure_field_count: document.querySelectorAll('input[type="password"]').length,
    upload_control_count: document.querySelectorAll('input[type="file"]').length,
    canvas_count: document.querySelectorAll("canvas").length,
    frame_count: document.querySelectorAll("iframe, frame").length,
    shadow_host_count: Array.from(document.querySelectorAll("*")).filter((node) => node.shadowRoot).slice(0, 33).length,
  };
}

function validNativeResponse(message) {
  if (!message || Object.getPrototypeOf(message) !== Object.prototype) return false;
  if (message.schema_version !== NEON_SCHEMA_VERSION || message.protocol_version !== NEON_PROTOCOL_VERSION) return false;
  if (typeof message.request_id !== "string" || typeof message.type !== "string") return false;
  return message.type === "neon.hello_ack" || message.type === "neon.observe_ack" || message.type === "neon.denied";
}

function ensureNativePort() {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative(NEON_NATIVE_HOST);
  nativePort.onMessage.addListener((message) => {
    if (!validNativeResponse(message)) {
      revoke("native_protocol_invalid");
      return;
    }
    const pending = pendingNative.get(message.request_id);
    if (!pending) {
      revoke("native_reply_unbound");
      return;
    }
    pendingNative.delete(message.request_id);
    pending.resolve(message);
  });
  nativePort.onDisconnect.addListener(() => {
    if (chrome.runtime.lastError) void chrome.runtime.lastError.message;
    revoke("native_disconnected");
  });
  return nativePort;
}

function nativeRequest(message) {
  return new Promise((resolve, reject) => {
    const port = ensureNativePort();
    pendingNative.set(message.request_id, { resolve, reject });
    port.postMessage(message);
    setTimeout(() => {
      if (pendingNative.delete(message.request_id)) {
        reject(new Error("native_timeout"));
        revoke("native_timeout");
      }
    }, 5000);
  });
}

async function connectSelectedTab() {
  await revoke("reconnecting");
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length !== 1 || !Number.isInteger(tabs[0].id) || !Number.isInteger(tabs[0].windowId)) {
    throw new Error("active_tab_missing");
  }
  const tab = tabs[0];
  if (tab.incognito) throw new Error("incognito_denied");
  const gestureId = crypto.randomUUID();
  const helloId = crypto.randomUUID();
  const hello = await nativeRequest({
    schema_version: NEON_SCHEMA_VERSION,
    protocol_version: NEON_PROTOCOL_VERSION,
    type: "neon.hello",
    request_id: helloId,
    extension_version: NEON_EXTENSION_VERSION,
    worker_generation: NEON_WORKER_GENERATION,
    user_gesture_id: gestureId,
    window_id: tab.windowId,
    tab_id: tab.id,
    incognito: false,
  });
  if (hello.type !== "neon.hello_ack") throw new Error("native_hello_denied");

  const injected = await chrome.scripting.executeScript({
    target: { tabId: tab.id, frameIds: [0] },
    world: "ISOLATED",
    func: observeTopDocument,
  });
  if (injected.length !== 1 || !injected[0].documentId || injected[0].frameId !== 0) {
    throw new Error("document_binding_missing");
  }
  const observation = injected[0].result;
  if (!observation || observation.schema_version !== NEON_SCHEMA_VERSION) {
    throw new Error("observation_invalid");
  }
  const requestId = crypto.randomUUID();
  const response = await nativeRequest({
    schema_version: NEON_SCHEMA_VERSION,
    protocol_version: NEON_PROTOCOL_VERSION,
    type: "neon.observe",
    request_id: requestId,
    session_id: hello.session_id,
    extension_version: NEON_EXTENSION_VERSION,
    worker_generation: NEON_WORKER_GENERATION,
    user_gesture_id: gestureId,
    window_id: tab.windowId,
    tab_id: tab.id,
    frame_id: 0,
    document_id: injected[0].documentId,
    origin: observation.origin,
    surface_kind: observation.surface_kind,
    content_type: observation.content_type,
    secure_field_count: observation.secure_field_count,
    upload_control_count: observation.upload_control_count,
    canvas_count: observation.canvas_count,
    frame_count: observation.frame_count,
    shadow_host_count: observation.shadow_host_count,
    incognito: false,
  });
  if (response.type !== "neon.observe_ack" || response.mode !== "observe_only") {
    throw new Error("observation_denied");
  }
  const state = {
    schema_version: NEON_SCHEMA_VERSION,
    state: "observed",
    reason_code: "none",
    worker_generation: NEON_WORKER_GENERATION,
    window_id: tab.windowId,
    tab_id: tab.id,
    document_id: injected[0].documentId,
    session_id: hello.session_id,
    binding_id: response.binding_id,
    profile_id: response.profile_id,
    origin_digest: response.origin_digest,
    snapshot_id: response.snapshot_id,
    fencing_token: response.fencing_token,
    surface_kind: observation.surface_kind,
  };
  await writeState(state);
  return state;
}

chrome.runtime.onInstalled.addListener(() => revoke("installed"));
chrome.runtime.onStartup.addListener(() => revoke("browser_restarted"));
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== "loading") return;
  readState().then((state) => {
    if (state.state === "observed" && state.tab_id === tabId) return revoke("navigation_revoked");
    return undefined;
  });
});
chrome.tabs.onRemoved.addListener((tabId) => {
  readState().then((state) => {
    if (state.state === "observed" && state.tab_id === tabId) return revoke("tab_closed");
    return undefined;
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const expectedUrl = chrome.runtime.getURL("neon_popup.html");
  if (sender.id !== chrome.runtime.id || sender.url !== expectedUrl) return false;
  if (!message || Object.getPrototypeOf(message) !== Object.prototype) return false;
  if (message.schema_version !== NEON_SCHEMA_VERSION || message.type !== "neon.popup" || !NEON_COMMANDS.has(message.command)) return false;
  (async () => {
    if (message.command === "status") return readState();
    if (message.command === "disconnect") {
      await revoke("user_disconnected");
      return readState();
    }
    try {
      return await connectSelectedTab();
    } catch (error) {
      const reason = error instanceof Error && /^[a-z][a-z0-9_]{0,63}$/.test(error.message)
        ? error.message
        : "connect_failed";
      await revoke(reason);
      return readState();
    }
  })().then(sendResponse);
  return true;
});

readState().then(writeState);
