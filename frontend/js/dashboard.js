import * as api from "./api.js";
import { escapeHtml, isDuplicate } from "./utils.js";
import { getQueueSnapshot, stopProcessing } from "./queue.js";
import { usagePanelHtml } from "./usage-panel.js";

const $ = (selector) => document.querySelector(selector);

// How long a finished/failed batch keeps showing in the pill before it
// clears back to hidden (the queue itself deletes a succeeded item instantly).
const DONE_DISPLAY_MS = 4500;

// Recently-completed queue items, kept around just long enough for the pill
// to say "✓ N processed" (the queue itself deletes an item the instant it
// succeeds, so without this the pill would jump straight to hidden).
let recentlyDone = []; // { id }
let clearDoneTimer = null;

export function renderDashboard() {
  // Static wiring handled in wire functions; kept for symmetry with other screens.
}

export function wireDashboard() {
  // "Scan a card" + "Upload photos instead" show on every breakpoint —
  // mobile matches desktop exactly instead of routing Scan through a
  // separate bottom-nav icon.
  const scanBtn = $("#dashScanBtn");
  scanBtn.addEventListener("click", async () => {
    const { openScanScreen } = await import("./scan.js");
    openScanScreen();
  });
  $("#dashUploadBtn").addEventListener("click", () => $("#dashUploadInput").click());
  $("#dashUploadInput").addEventListener("change", async () => {
    const files = Array.from($("#dashUploadInput").files || []);
    if (!files.length) return;
    const { importGalleryFiles } = await import("./scan.js");
    await importGalleryFiles(files);
    $("#dashUploadInput").value = "";
  });
  $("#processingPillStopBtn")?.addEventListener("click", async () => {
    stopProcessing();
    const { showToast } = await import("./app-shell.js");
    showToast("Terminated queue processing.", "info");
  });
  // Registered once here (not inside render) so a completed card updates the
  // pill no matter which screen is active when it finishes.
  window.addEventListener("queueItemProcessed", handleQueueItemProcessed);
}

export async function refreshDashboard(state) {
  renderHero(state);
  renderProcessingPill(state);
  renderUsage(state);
}

async function handleQueueItemProcessed(event) {
  const { id, success } = event.detail || {};
  if (!success || id == null) return;
  if (recentlyDone.some((entry) => entry.id === id)) return; // already tracked
  recentlyDone.push({ id });
  window.clearTimeout(clearDoneTimer);
  clearDoneTimer = window.setTimeout(() => {
    recentlyDone = [];
    import("./app-shell.js").then(({ state }) => renderProcessingPill(state));
  }, DONE_DISPLAY_MS);
  const { state } = await import("./app-shell.js");
  renderProcessingPill(state);
}

function renderHero(state) {
  const hero = $("#heroCard");
  if (state.loading) {
    hero.innerHTML = `
      <div class="skeleton-line" style="width:60%;height:20px"></div>
      <div class="skeleton-line" style="width:40%;margin-top:8px"></div>
      <div class="hero-stats">
        <div class="skeleton-block" style="height:56px"></div>
        <div class="skeleton-block" style="height:56px"></div>
        <div class="skeleton-block" style="height:56px"></div>
      </div>
    `;
    return;
  }
  const event = state.events.find((item) => item.event_id === state.eventId);
  if (!state.events.length) {
    hero.innerHTML = `
      <div class="hero-card-name">Create your first event</div>
      <div class="hero-card-sub">Set up an event to start scanning cards.</div>
      <button id="heroCreateEventBtn" class="btn primary block" type="button" style="margin-top:var(--space-3)">+ New event</button>
    `;
    $("#heroCreateEventBtn").addEventListener("click", async () => {
      const { openEventSheet } = await import("./events.js");
      openEventSheet();
      $("#eventNewBtn").click();
    });
    return;
  }
  const total = state.records.length;
  const today = new Date().toISOString().slice(0, 10);
  const todayCount = state.records.filter((record) => record.date === today).length;
  const dupCount = state.records.filter(isDuplicate).length;
  hero.innerHTML = `
    <div class="hero-card-top">
      <div class="hero-card-name">${escapeHtml(event?.name || "Event")}</div>
      <button id="heroSwitchEventBtn" class="btn text-link" type="button">Switch <svg class="icon" aria-hidden="true" style="width:14px;height:14px"><use href="#icon-chevron-down"/></svg></button>
    </div>
    <div class="hero-card-sub">${escapeHtml(event?.date || "")}${event?.location ? ` · ${escapeHtml(event.location)}` : ""}</div>
    <div class="hero-stats">
      <div class="hero-stat"><div class="num mono">${total}</div><div class="label">Cards</div></div>
      <div class="hero-stat"><div class="num mono">${todayCount}</div><div class="label">Today</div></div>
      <div class="hero-stat"><div class="num mono ${dupCount > 0 ? "amber" : ""}">${dupCount}</div><div class="label">Duplicates</div></div>
    </div>
  `;
  $("#heroSwitchEventBtn").addEventListener("click", async () => {
    const { openEventSheet } = await import("./events.js");
    openEventSheet();
  });
}

// Compact, auto-hiding live indicator — replaces the old always-visible
// "Processing" panel. Only takes up space on Home while something is
// actually happening; per-photo detail still lives in the process sheet.
function renderProcessingPill(state) {
  const pill = $("#processingPill");
  const text = $("#processingPillText");
  if (!pill || !text) return;

  const queue = getQueueSnapshot();
  const inFlight = queue.filter((it) => it.status === "pending" || it.status === "uploading").length;
  const failed = queue.filter((it) => it.status === "failed").length;
  const cancelled = queue.filter((it) => it.status === "cancelled").length;
  const stopBtn = $("#processingPillStopBtn");

  if (inFlight > 0) {
    text.innerHTML = `Processing ${inFlight} card${inFlight === 1 ? "" : "s"}&hellip;`;
    pill.classList.add("visible");
    if (stopBtn) stopBtn.hidden = false;
  } else if (failed > 0 || cancelled > 0) {
    const parts = [];
    if (failed) parts.push(`${failed} failed`);
    if (cancelled) parts.push(`${cancelled} stopped`);
    text.innerHTML = `${escapeHtml(parts.join(", "))} <span class="dim">&middot; retry from Records</span>`;
    pill.classList.add("visible");
    if (stopBtn) stopBtn.hidden = true;
  } else if (recentlyDone.length > 0) {
    text.textContent = `✓ ${recentlyDone.length} card${recentlyDone.length === 1 ? "" : "s"} processed`;
    pill.classList.add("visible");
    if (stopBtn) stopBtn.hidden = true;
  } else {
    pill.classList.remove("visible");
    if (stopBtn) stopBtn.hidden = true;
  }
}

// Admins see the shared API-usage/credit panel (it's operational data about
// the whole deployment); everyone else sees their own scan activity instead
// — API quotas aren't actionable for a non-admin and were just noise here.
function renderUsage(state) {
  const isAdmin = state.user?.role === "admin";
  const usageCard = $("#usageCard");
  const activityCard = $("#myActivityCard");
  if (usageCard) usageCard.hidden = !isAdmin;
  if (activityCard) activityCard.hidden = isAdmin;

  if (isAdmin) {
    $("#usageMeters").innerHTML = usagePanelHtml(state.usage, state.health);
    return;
  }
  renderMyActivity(state);
}

async function renderMyActivity(state) {
  const el = $("#myActivityContent");
  if (!el) return;
  try {
    const stats = await api.myStats(14);
    const recentDays = (stats.daily || []).slice(-7).reverse();
    const dayRows = recentDays.length
      ? recentDays.map((row) => `
          <div class="admin-activity-row">
            <div class="who"><div class="name">${escapeHtml(row.day)}</div></div>
            <div class="delta">${row.count}</div>
          </div>`).join("")
      : `<div class="provider-row-note">No scans recorded yet.</div>`;
    el.innerHTML = `
      <div class="admin-stat-row" style="grid-template-columns:1fr 1fr 1fr;margin-bottom:var(--space-3)">
        <div class="admin-stat"><div class="num">${stats.total || 0}</div><div class="label">Total</div></div>
        <div class="admin-stat good"><div class="num">${stats.today || 0}</div><div class="label">Today</div></div>
        <div class="admin-stat${stats.errors ? "" : " good"}"><div class="num">${stats.errors || 0}</div><div class="label">Errors</div></div>
      </div>
      ${dayRows}
    `;
  } catch {
    el.innerHTML = `<div class="provider-row-note">Activity unavailable — check the connection.</div>`;
  }
}
