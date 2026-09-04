"use strict";

const statusNode = document.getElementById("status");
const connectButton = document.getElementById("connect");
const disconnectButton = document.getElementById("disconnect");

function render(response) {
  const state = response && typeof response.state === "string" ? response.state : "unavailable";
  const reason = response && typeof response.reason_code === "string" ? response.reason_code : "none";
  statusNode.textContent = reason === "none" ? state : `${state} (${reason})`;
  connectButton.disabled = state === "observed";
  disconnectButton.disabled = state !== "observed";
}

async function request(command) {
  try {
    const response = await chrome.runtime.sendMessage({
      schema_version: 1,
      type: "neon.popup",
      command,
    });
    render(response);
  } catch (_error) {
    render({ state: "unavailable", reason_code: "service_worker_unavailable" });
  }
}

connectButton.addEventListener("click", () => request("connect"));
disconnectButton.addEventListener("click", () => request("disconnect"));
request("status");
