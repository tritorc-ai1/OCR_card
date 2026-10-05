import * as api from "./api.js";
import { escapeHtml } from "./utils.js";

const $ = (selector) => document.querySelector(selector);

export function wireEventSheet() {
  $("#eventSheetCloseBtn").addEventListener("click", () => $("#eventSheet").close());
  $("#eventSheet").addEventListener("click", (event) => {
    if (event.target.id === "eventSheet") $("#eventSheet").close();
  });
  const appBarDeleteBtn = document.getElementById("appBarDeleteBtn");
  if (appBarDeleteBtn) {
    appBarDeleteBtn.addEventListener("click", async () => {
      const { state } = await import("./app-shell.js");
      if (state.eventId) openDeleteEventConfirm(state.eventId);
    });
  }
  $("#eventNewBtn").addEventListener("click", () => {
    const today = new Date().toISOString().slice(0, 10);
    $("#eventName").value = "";
    $("#eventDate").value = today;
    $("#eventLocation").value = "";
    $("#eventCreateForm").classList.add("visible");
    $("#eventList").hidden = true;
    $("#eventNewBtn").hidden = true;
    $("#eventName").focus();
  });
  $("#eventCreateCancelBtn").addEventListener("click", () => {
    $("#eventCreateForm").classList.remove("visible");
    $("#eventList").hidden = false;
    $("#eventNewBtn").hidden = false;
  });
  $("#eventCreateForm").addEventListener("submit", handleCreateEvent);
  wireDeleteEventConfirm();
}

export function openEventSheet() {
  import("./app-shell.js").then(({ state }) => {
    renderEventList(state);
    $("#eventCreateForm").classList.remove("visible");
    $("#eventList").hidden = false;
    $("#eventNewBtn").hidden = false;
    $("#eventSheet").showModal();
  });
}

function renderEventList(state) {
  const list = $("#eventList");
  if (!state.events.length) {
    list.innerHTML = `<p style="color:var(--text-dim);font-size:var(--font-sm)">No events yet. Create one below.</p>`;
    return;
  }
  list.innerHTML = state.events.map((event) => `
    <div class="event-row ${event.event_id === state.eventId ? "current" : ""}" data-event-id="${escapeHtml(event.event_id)}">
      <div class="event-row-main">
        <div class="event-row-headline">
          <div class="event-row-name">${escapeHtml(event.name)}</div>
          <button class="btn danger event-row-delete" type="button" data-delete-event="${escapeHtml(event.event_id)}" aria-label="Delete event ${escapeHtml(event.name)}" title="Delete event ${escapeHtml(event.name)}">
            <svg class="icon icon-trash-visible" aria-hidden="true"><use href="#icon-trash"/></svg>
            Delete
          </button>
        </div>
        <div class="event-row-meta">${escapeHtml(event.date)}${event.location ? ` · ${escapeHtml(event.location)}` : ""}</div>
      </div>
      <div class="event-row-end">
        <svg class="icon" aria-hidden="true"><use href="#icon-check"/></svg>
      </div>
    </div>
  `).join("");
  list.querySelectorAll("[data-event-id]").forEach((row) => {
    row.addEventListener("click", async () => {
      const { switchEvent } = await import("./app-shell.js");
      $("#eventSheet").close();
      await switchEvent(row.dataset.eventId);
    });
  });
  list.querySelectorAll("[data-delete-event]").forEach((btn) => {
    btn.addEventListener("click", (domEvent) => {
      domEvent.stopPropagation(); // don't also trigger the row's switch-event click
      openDeleteEventConfirm(btn.dataset.deleteEvent);
    });
  });
}

function wireDeleteEventConfirm() {
  const sheet = $("#deleteEventConfirmSheet");
  $("#deleteEventConfirmCancelBtn").addEventListener("click", () => sheet.close());
  sheet.addEventListener("click", (event) => {
    if (event.target === sheet) sheet.close();
  });
  $("#deleteEventConfirmBtn").addEventListener("click", async () => {
    const { state, loadEvents, refreshAll, showToast } = await import("./app-shell.js");
    if (!state.online) return showToast("Deleting an event requires a connection.", "error");
    const eventId = sheet.dataset.eventId;
    const confirmBtn = $("#deleteEventConfirmBtn");
    confirmBtn.disabled = true;
    try {
      const result = await api.deleteEvent(eventId);
      sheet.close();
      $("#eventSheet").close();
      // loadEvents() already falls back state.eventId to another event (or
      // null if none remain) when the previously-selected one is gone.
      await loadEvents();
      await refreshAll();
      showToast(`Deleted event and ${result.deleted.records || 0} records.`, "success");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      confirmBtn.disabled = false;
    }
  });
}

async function openDeleteEventConfirm(eventId) {
  const { state } = await import("./app-shell.js");
  const event = state.events.find((e) => e.event_id === eventId);
  const sheet = $("#deleteEventConfirmSheet");
  sheet.dataset.eventId = eventId;
  $("#deleteEventConfirmName").textContent = event?.name || eventId;
  $("#deleteEventConfirmBtn").disabled = false;
  sheet.showModal();
}

async function handleCreateEvent(event) {
  event.preventDefault();
  const { showToast, loadEvents, switchEvent } = await import("./app-shell.js");
  const name = $("#eventName").value.trim();
  const date = $("#eventDate").value;
  const location = $("#eventLocation").value.trim();
  if (!name || !date) return showToast("Event name and date are required.", "error");
  $("#eventCreateSaveBtn").disabled = true;
  try {
    const created = await api.createEvent({ name, date, location });
    await loadEvents();
    $("#eventSheet").close();
    await switchEvent(created.event_id);
    showToast(`Created event: ${created.name}`, "success");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    $("#eventCreateSaveBtn").disabled = false;
  }
}

export function refreshEventLabel(state) {
  const event = state.events.find((item) => item.event_id === state.eventId);
  const label = $("#appBarEvent .name");
  label.textContent = event ? event.name : "Select event";
  const appBarDeleteBtn = document.getElementById("appBarDeleteBtn");
  if (appBarDeleteBtn) appBarDeleteBtn.disabled = !event;
}
