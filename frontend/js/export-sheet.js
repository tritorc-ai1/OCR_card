import * as api from "./api.js";
import { escapeHtml } from "./utils.js";

const $ = (selector) => document.querySelector(selector);

// Keep in sync with backend/config.py's EXCEL_COLUMNS/EXCEL_HEADERS — this is the
// full set of fields a custom export can include, in the same order they'll
// appear as spreadsheet columns.
const EXPORT_FIELDS = [
  ["date", "Date"],
  ["name", "Name"],
  ["designation", "Designation"],
  ["business", "Business"],
  ["address", "Address"],
  ["city", "City"],
  ["state", "State"],
  ["country", "Country"],
  ["zip_code", "Zip Code"],
  ["website", "Website"],
  ["category", "Category"],
  ["social_media", "Social Media"],
  ["notes", "Notes"],
  ["email1", "Email1"],
  ["email2", "Email2"],
  ["contact1", "Contact1"],
  ["contact2", "Contact2"],
  ["contact3", "Contact3"],
  ["card", "Card image"],
];

export function wireExportOptionsSheet() {
  const grid = $("#exportFieldGrid");
  grid.innerHTML = EXPORT_FIELDS.map(([key, label]) => `
    <label class="export-field-row">
      <input type="checkbox" value="${key}" checked>
      <span>${label}</span>
    </label>
  `).join("");

  $("#exportOptionsCloseBtn").addEventListener("click", closeExportOptions);
  $("#exportOptionsCancelBtn").addEventListener("click", closeExportOptions);
  $("#exportOptionsSheet").addEventListener("click", (event) => {
    if (event.target.id === "exportOptionsSheet") closeExportOptions();
  });
  $("#exportSelectAllBtn").addEventListener("click", () => {
    grid.querySelectorAll("input[type=checkbox]").forEach((cb) => { cb.checked = true; });
  });
  $("#exportSelectNoneBtn").addEventListener("click", () => {
    grid.querySelectorAll("input[type=checkbox]").forEach((cb) => { cb.checked = false; });
  });

  const categoryGrid = $("#exportCategoryGrid");
  $("#exportCategoryAllBtn").addEventListener("click", () => {
    categoryGrid.querySelectorAll("input[type=checkbox]").forEach((cb) => { cb.checked = true; });
  });
  $("#exportCategoryNoneBtn").addEventListener("click", () => {
    categoryGrid.querySelectorAll("input[type=checkbox]").forEach((cb) => { cb.checked = false; });
  });

  $("#exportOptionsDownloadBtn").addEventListener("click", async () => {
    const { state, showToast } = await import("./app-shell.js");
    if (!state.online) return showToast("Export requires a connection.", "error");
    if (!state.eventId) return showToast("Select an event first.", "error");
    const columns = Array.from(grid.querySelectorAll("input[type=checkbox]:checked")).map((cb) => cb.value);
    if (!columns.length) return showToast("Choose at least one field to export.", "error");
    const skipDuplicates = $("#exportSkipDuplicates").checked;
    const search = $("#exportSearch").value.trim();

    // Omit the category filter entirely when every known category is
    // checked (the common case) so a plain export doesn't grow a needless
    // query string; only send it when the user actually narrowed the set.
    const categoryBoxes = Array.from(categoryGrid.querySelectorAll("input[type=checkbox]"));
    const checkedCategories = categoryBoxes.filter((cb) => cb.checked).map((cb) => cb.value);
    if (categoryBoxes.length && !checkedCategories.length) {
      return showToast("Choose at least one category, or select All.", "error");
    }
    const categories = categoryBoxes.length && checkedCategories.length < categoryBoxes.length ? checkedCategories : null;

    window.open(api.downloadUrl(state.eventId, { skipDuplicates, columns, categories, search }), "_blank");
    closeExportOptions();
  });
}

export async function openExportOptions() {
  const { state } = await import("./app-shell.js");
  renderCategoryOptions(state.records || []);
  $("#exportOptionsSheet").showModal();
}

// Categories are drawn from what's actually in this event's records (not a
// hardcoded master list) — so the checklist only ever shows options that
// will actually filter something, with a count so "Manufacturing (4)" tells
// you what you're about to get before you download it.
function renderCategoryOptions(records) {
  const wrap = $("#exportCategoryWrap");
  const grid = $("#exportCategoryGrid");
  const counts = new Map();
  records.forEach((r) => {
    const label = (r.category || "").trim();
    if (!label) return;
    counts.set(label, (counts.get(label) || 0) + 1);
  });
  const categories = [...counts.keys()].sort((a, b) => a.localeCompare(b));
  if (!categories.length) {
    wrap.hidden = true;
    grid.innerHTML = "";
    return;
  }
  wrap.hidden = false;
  grid.innerHTML = categories.map((label) => `
    <label class="export-field-row">
      <input type="checkbox" value="${escapeHtml(label)}" checked>
      <span>${escapeHtml(label)} (${counts.get(label)})</span>
    </label>
  `).join("");
}

function closeExportOptions() {
  $("#exportOptionsSheet").close();
}
