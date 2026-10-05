import * as api from "./api.js";
import { escapeHtml, displayNameFromEmail } from "./utils.js";
import { usagePanelHtml } from "./usage-panel.js";

const $ = (selector) => document.querySelector(selector);

let deferredInstallPrompt = null;

export function wireMoreScreen() {
  // Account section
  const changePasswordBtn = $("#changePasswordBtn");
  if (changePasswordBtn) {
    changePasswordBtn.addEventListener("click", () => {
      const sheet = $("#changePasswordSheet");
      if (sheet) sheet.showModal?.();
    });
  }

  const moreResetBtn = $("#moreResetBtn");
  if (moreResetBtn) {
    moreResetBtn.addEventListener("click", async () => {
      const { state, showToast } = await import("./app-shell.js");
      if (!state.eventId) return showToast("Select an event first.", "error");
      if (!confirm("Reset all card data for this event? This cannot be undone.")) return;
      try {
        await api.resetCards(state.eventId);
        showToast("Event data reset.", "success");
        state.records = [];
        const { handleRouteChange } = await import("./app-shell.js");
        handleRouteChange();
      } catch (err) {
        showToast("Reset failed: " + err.message, "error");
      }
    });
  }

  // Delete event button
  const deleteBtn = document.getElementById("appBarDeleteBtn");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      const { state, showToast } = await import("./app-shell.js");
      if (!state.eventId) return showToast("Select an event first.", "error");
      if (!confirm("Delete this event permanently? This cannot be undone.")) return;
      try {
        await api.deleteEvent(state.eventId);
        showToast("Event deleted.", "success");
        state.events = state.events.filter((e) => e.event_id !== state.eventId);
        state.eventId = state.events[0]?.event_id || null;
        const { loadEvents, handleRouteChange } = await import("./app-shell.js");
        await loadEvents();
        handleRouteChange();
      } catch (err) {
        showToast("Delete failed: " + err.message, "error");
      }
    });
  }

  $("#moreExportBtn").addEventListener("click", async () => {
    const { state, showToast } = await import("./app-shell.js");
    if (!state.eventId) return showToast("Select an event first.", "error");
    if (!state.online) return showToast("Export requires a connection.", "error");
    window.open(api.downloadUrl(state.eventId), "_blank");
  });

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    $("#installAppBtn").hidden = false;
  });
  window.addEventListener("appinstalled", () => {
    $("#installAppBtn").hidden = true;
    deferredInstallPrompt = null;
  });
  $("#installAppBtn").addEventListener("click", async () => {
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    $("#installAppBtn").hidden = true;
  });

}

export async function refreshMoreScreen(state) {
  renderAccount(state);
  renderUsage(state);
  await renderHealth(state);
}

function renderAccount(state) {
  if (!state.user) return;
  const nameEl = $("#accountName");
  if (nameEl) nameEl.textContent = displayNameFromEmail(state.user.email);
  const emailEl = $("#accountEmail");
  if (emailEl) emailEl.textContent = state.user.email;
}

function renderUsage(state) {
  const el = $("#moreUsageMeters");
  el.innerHTML = usagePanelHtml(state.usage, state.health);
}

async function renderHealth(state) {
  const el = $("#healthList");
  try {
    const data = state.health || (await api.getHealth());
    const usageMongo = state.usage?.mongo;
    const mongo = usageMongo || data.mongo_usage || {};
    const mongoStatus = mongo.enabled
      ? (
          mongo.available
            ? "live"
            : mongo.checked === false && !usageMongo
              ? "configured - checked during usage"
              : (mongo.blocking_scans ? "unavailable - blocking scans" : "fallback - local counters")
        )
      : "disabled";
    el.innerHTML = `
      <div>Gemini: ${data.gemini_configured ? `configured (${data.gemini_key_count} key${data.gemini_key_count === 1 ? "" : "s"})` : "not configured"}</div>
      <div>Google Vision: ${data.google_vision_configured ? "configured" : "not configured"}</div>
      <div>MongoDB tracker: ${escapeHtml(mongoStatus)}</div>
      <div>Mode: ${escapeHtml(data.processing_mode || "")}</div>
    `;
  } catch {
    el.textContent = "Health check unavailable.";
  }
}
