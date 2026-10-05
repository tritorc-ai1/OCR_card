export async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const error = new Error(data.detail || `Request failed: ${response.status}`);
    error.status = response.status;
    // Dispatch auth:required event on 401 (except for auth endpoints themselves)
    if (response.status === 401 && url !== "/auth/login" && url !== "/auth/me") {
      window.dispatchEvent(new CustomEvent("auth:required"));
    }
    throw error;
  }
  return data;
}

export function getHealth() {
  return fetchJson("/health");
}

export function listEvents() {
  return fetchJson("/events");
}

export function createEvent(payload) {
  return fetchJson("/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function listRecords(eventId) {
  return fetchJson(`/events/${eventId}/cards`);
}

export function uploadCard(eventId, frontBlob, frontName, backBlob, backName, signal) {
  const form = new FormData();
  form.append("front", frontBlob, frontName || "front.jpg");
  if (backBlob) form.append("back", backBlob, backName || "back.jpg");
  return fetchJson(`/events/${eventId}/cards`, { method: "POST", body: form, signal });
}

export function patchRecord(eventId, cardId, values) {
  return fetchJson(`/events/${eventId}/cards/${cardId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export function resetCards(eventId) {
  return fetchJson(`/events/${eventId}/cards`, { method: "DELETE" });
}

export function deleteEvent(eventId) {
  return fetchJson(`/events/${eventId}`, { method: "DELETE" });
}

export function getUsage() {
  return fetchJson("/llm-usage");
}

export function downloadUrl(eventId, options = {}) {
  const params = new URLSearchParams();
  if (options.skipDuplicates) params.set("skip_duplicates", "true");
  if (options.columns && options.columns.length) params.set("columns", options.columns.join(","));
  if (options.categories && options.categories.length) params.set("category", options.categories.join(","));
  if (options.search) params.set("search", options.search);
  const query = params.toString();
  return `/events/${eventId}/download${query ? `?${query}` : ""}`;
}

export function imageUrl(eventId, filename) {
  return `/events/${eventId}/images/${encodeURIComponent(filename)}`;
}

// --- Auth endpoints -----
export function getMe() {
  return fetchJson("/auth/me");
}

export function login(email, password) {
  return fetchJson("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
}

export function logout() {
  return fetchJson("/auth/logout", { method: "POST" });
}

export function changePassword(currentPassword, newPassword) {
  return fetchJson("/auth/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

// --- Admin endpoints -----
export function adminListUsers() {
  return fetchJson("/admin/users");
}

export function adminCreateUser(email, password, role = "user") {
  return fetchJson("/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, role }),
  });
}

export function adminPatchUser(email, payload) {
  return fetchJson(`/admin/users/${encodeURIComponent(email)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function adminStats(days = 30) {
  return fetchJson(`/admin/stats?days=${Math.min(Math.max(1, days), 365)}`);
}

export function myStats(days = 30) {
  return fetchJson(`/me/stats?days=${Math.min(Math.max(1, days), 365)}`);
}
