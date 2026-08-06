import * as api from "./api.js";
import { showToast } from "./app-shell.js";
import { escapeHtml, displayNameFromEmail } from "./utils.js";
import { usagePanelHtml } from "./usage-panel.js";

// This account seeds/owns the deployment (see app/main.py's seed_test_user
// call) and must always keep at least one working admin login — deactivating
// it is blocked here (and mirrored server-side for self-deactivation) so an
// admin can't accidentally lock the team out.
const PROTECTED_ADMIN_EMAIL = "a.i@tritorc.com";

export function wireAdminScreen() {
  // Delegated event handlers for dynamic elements
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (btn) {
      const action = btn.dataset.action;
      const email = btn.dataset.email;
      if (action === "deactivate") {
        await handleUserAction(email, { active: false });
      } else if (action === "reactivate") {
        await handleUserAction(email, { active: true });
      } else if (action === "reset-password") {
        const newPw = prompt("Enter temporary password:");
        if (newPw) {
          await handleUserAction(email, { new_password: newPw });
        }
      } else if (action === "toggle-create-user") {
        const form = document.getElementById("adminCreateUserForm");
        if (form) {
          form.classList.toggle("visible");
          if (form.classList.contains("visible")) form.elements.email?.focus();
        }
      } else if (action === "view-activity") {
        showUserActivity(email);
      }
    }
  });

  document.addEventListener("submit", async (e) => {
    if (e.target.id === "adminCreateUserForm") {
      e.preventDefault();
      const email = e.target.elements.email?.value;
      const password = e.target.elements.password?.value;
      const role = e.target.elements.role?.value || "user";
      if (!email || !password) return showToast("All fields required", "error");
      try {
        await api.adminCreateUser(email, password, role);
        showToast(`${role === "admin" ? "Admin" : "User"} ${email} created`, "success");
        e.target.reset();
        await refreshAdminScreen(window.state || {});
      } catch (err) {
        showToast("Create failed: " + err.message, "error");
      }
    }
  });

  document.getElementById("userActivityCloseBtn")?.addEventListener("click", () => {
    document.getElementById("userActivitySheet")?.close();
  });

  // Role select is a <select>, not a button — it fires "change", not "click".
  document.addEventListener("change", async (e) => {
    const select = e.target.closest("select[data-action='change-role']");
    if (!select) return;
    const email = select.dataset.email;
    const role = select.value;
    if (!confirm(`Change ${email}'s role to "${role.replace("_", " ")}"?`)) {
      await refreshAdminScreen(window.state || {}); // revert the <select> to its real value
      return;
    }
    await handleUserAction(email, { role });
  });
}

async function handleUserAction(email, payload) {
  try {
    await api.adminPatchUser(email, payload);
    showToast("User updated", "success");
    await refreshAdminScreen(window.state || {});
  } catch (err) {
    showToast("Update failed: " + err.message, "error");
  }
}

function relativeTime(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return mins <= 1 ? "just now" : `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr${hrs === 1 ? "" : "s"} ago`;
  const days = Math.round(hrs / 24);
  if (days < 14) return `${days} day${days === 1 ? "" : "s"} ago`;
  const weeks = Math.round(days / 7);
  return `${weeks} week${weeks === 1 ? "" : "s"} ago`;
}

function formatDayLabel(isoDay) {
  const date = new Date(`${isoDay}T00:00:00`);
  if (Number.isNaN(date.getTime())) return isoDay;
  const today = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  if (isoDay === today) return "Today";
  if (isoDay === yesterday) return "Yesterday";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

let lastStatsResp = null;

function showUserActivity(email) {
  const sheet = document.getElementById("userActivitySheet");
  const body = document.getElementById("userActivityBody");
  const title = document.getElementById("userActivityTitle");
  if (!sheet || !body) return;

  const stat = (lastStatsResp?.totals || []).find((row) => row.email === email);
  const byEvent = (lastStatsResp?.by_event || []).filter((row) => row.email === email)
    .sort((a, b) => b.count - a.count);
  const byDay = (lastStatsResp?.daily || []).filter((row) => row.email === email)
    .sort((a, b) => b.day.localeCompare(a.day));
  const today = new Date().toISOString().slice(0, 10);
  const todayCount = byDay.filter((row) => row.day === today).reduce((sum, row) => sum + row.count, 0);

  if (title) title.textContent = `${displayNameFromEmail(email)}'s activity`;

  const eventRows = byEvent.length
    ? byEvent.map((row) => `
        <div class="admin-activity-row">
          <div class="who">
            <div class="name">${escapeHtml(row.event_name || "Untitled event")}</div>
          </div>
          <div class="delta">${row.count}</div>
        </div>`).join("")
    : `<div class="admin-card-sub" style="margin:0">No scans recorded for this user yet.</div>`;

  const dailyRows = byDay.length
    ? byDay.slice(0, 14).map((row) => `
        <div class="admin-activity-row">
          <div class="who">
            <div class="name">${escapeHtml(formatDayLabel(row.day))}</div>
          </div>
          <div class="delta">${row.count}</div>
        </div>`).join("")
    : `<div class="admin-card-sub" style="margin:0">No daily activity in this window.</div>`;

  body.innerHTML = `
    <div class="admin-stat-row" style="grid-template-columns:1fr 1fr 1fr;margin-bottom:var(--space-4)">
      <div class="admin-stat"><div class="num">${stat?.total || 0}</div><div class="label">Total scans</div></div>
      <div class="admin-stat good"><div class="num">${todayCount}</div><div class="label">Today</div></div>
      <div class="admin-stat${stat?.errors ? "" : " good"}"><div class="num">${stat?.errors || 0}</div><div class="label">Errors</div></div>
    </div>
    <div class="admin-card-sub" style="margin-bottom:var(--space-3)">Last scan: ${relativeTime(stat?.last_scan_at)}</div>
    <div class="section-label" style="margin-bottom:12px">Daily activity</div>
    ${dailyRows}
    <div class="section-label" style="margin:var(--space-4) 0 12px">By event</div>
    ${eventRows}
  `;

  sheet.showModal?.();
}

function adminSkeletonHtml() {
  return `
    <div class="admin-grid">
      <div class="admin-sidebar">
        <div class="admin-card">
          <div class="skeleton-line" style="width:50%;height:18px;margin-bottom:10px"></div>
          <div class="admin-stat-row">
            <div class="skeleton-block" style="height:48px"></div>
            <div class="skeleton-block" style="height:48px"></div>
          </div>
        </div>
        <div class="admin-card">
          <div class="skeleton-line" style="width:100%;height:20px;margin-bottom:6px"></div>
          <div class="skeleton-line" style="width:100%;height:20px;margin-bottom:6px"></div>
          <div class="skeleton-line" style="width:100%;height:20px"></div>
        </div>
      </div>
      <div class="admin-main">
        <div class="skeleton-line" style="width:30%;height:24px;margin-bottom:16px"></div>
        <div class="skeleton-block" style="height:200px"></div>
      </div>
    </div>
  `;
}

export async function refreshAdminScreen(state) {
  window.state = state; // for delegated handlers
  const container = document.getElementById("adminContent");
  if (!container) return;

  // Revisiting #/admin re-fetches (adminStats took ~2.5s in the perf audit)
  // — without this, the previous view's users/stats stay on screen looking
  // current while fresh data loads.
  container.innerHTML = adminSkeletonHtml();

  try {
    const [statsResp, usersResp] = await Promise.all([
      api.adminStats(30),
      api.adminListUsers(),
    ]);
    lastStatsResp = statsResp;

    const users = usersResp.users || [];
    const totals = statsResp.totals || [];
    const statsByEmail = new Map(totals.map((row) => [row.email, row]));
    const today = new Date().toISOString().slice(0, 10);
    const todayByEmail = new Map();
    (statsResp.daily || []).forEach((row) => {
      if (row.day === today) todayByEmail.set(row.email, row.count);
    });
    const scansToday = [...todayByEmail.values()].reduce((sum, n) => sum + n, 0);
    const activeCount = users.filter((u) => u.active).length;

    let health = state.health;
    if (!health) {
      try { health = await api.getHealth(); } catch { health = {}; }
    }
    let usage = state.usage;
    if (usage === undefined) {
      try { usage = await api.getUsage(); } catch { usage = null; }
    }

    // Activity rows: every user who has ever scanned, busiest first.
    const activityRows = totals
      .map((row) => `
        <div class="admin-activity-row">
          <div class="who">
            <div class="name">${escapeHtml(displayNameFromEmail(row.email))}</div>
            <div class="meta">${row.total} card${row.total === 1 ? "" : "s"} total</div>
          </div>
          <div class="delta">+${todayByEmail.get(row.email) || 0}</div>
        </div>`)
      .join("") || `<div class="admin-card-sub" style="margin:0">No scans recorded yet.</div>`;

    const untracked = statsResp.untracked_records > 0
      ? `<div class="admin-card-sub" style="margin:10px 0 0">${statsResp.untracked_records} records predate per-user tracking.</div>`
      : "";

    const viewerIsSuperAdmin = state.user?.role === "super_admin";
    const roleLabels = { super_admin: "Super Admin", admin: "Admin", user: "User" };
    const userRows = users.map((user) => {
      const isProtected = user.email === PROTECTED_ADMIN_EMAIL;
      const stat = statsByEmail.get(user.email);
      const rowAction = isProtected
        ? `<span class="admin-row-action" style="cursor:default;color:var(--text-dim)" title="This account is permanent and can't be deactivated">Protected</span>`
        : user.active
          ? `<button class="admin-row-action danger" data-action="deactivate" data-email="${escapeHtml(user.email)}" type="button">Deactivate</button>`
          : `<button class="admin-row-action" data-action="reactivate" data-email="${escapeHtml(user.email)}" type="button">Reactivate</button>`;
      // Only a super admin can change roles, and never the protected seed
      // account's — everyone else sees the same read-only chip as today.
      const roleCell = viewerIsSuperAdmin && !isProtected
        ? `<select class="role-select" data-action="change-role" data-email="${escapeHtml(user.email)}">
            ${["user", "admin", "super_admin"].map((r) => `<option value="${r}" ${user.role === r ? "selected" : ""}>${roleLabels[r]}</option>`).join("")}
          </select>`
        : `<span class="role-chip ${user.role === "super_admin" ? "super-admin" : user.role === "admin" ? "admin" : "user"}">${roleLabels[user.role] || "User"}</span>`;
      return `
        <tr>
          <td class="user-name">${escapeHtml(displayNameFromEmail(user.email))}</td>
          <td class="user-email">${escapeHtml(user.email)}</td>
          <td>${roleCell}</td>
          <td><span class="status-chip ${user.active ? "active" : "deactivated"}">${user.active ? "Active" : "Deactivated"}</span></td>
          <td style="color:var(--text-dim)">${relativeTime(stat?.last_scan_at || user.password_changed_at || user.created_at)}</td>
          <td>
            <button class="admin-row-action" data-action="view-activity" data-email="${escapeHtml(user.email)}" type="button">Activity</button>
            <button class="admin-row-action" data-action="reset-password" data-email="${escapeHtml(user.email)}" type="button">Reset PW</button>
            ${rowAction}
          </td>
        </tr>`;
    }).join("");

    container.innerHTML = `
      <div class="admin-grid">
        <div class="admin-sidebar">
          <div class="admin-card">
            <div class="admin-card-title">Admin panel</div>
            <div class="admin-card-sub">User accounts &amp; system health</div>
            <div class="admin-stat-row">
              <div class="admin-stat"><div class="num">${users.length}</div><div class="label">Users</div></div>
              <div class="admin-stat good"><div class="num">${activeCount}</div><div class="label">Active</div></div>
            </div>
          </div>

          <div class="admin-card">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
              <div class="section-label" style="margin:0">Scan activity by user</div>
              <span style="font-size:12px;font-weight:700;color:var(--teal-dark)">${scansToday} today</span>
            </div>
            ${activityRows}
            ${untracked}
          </div>

          <div class="admin-card">
            <div class="section-label" style="margin-bottom:10px">System health</div>
            ${usagePanelHtml(usage, health)}
          </div>
        </div>

        <div class="admin-main">
          <div class="admin-title-row">
            <div class="heading">
              <h2>Users</h2>
              <span class="count">${users.length} account${users.length === 1 ? "" : "s"}</span>
            </div>
            <button class="btn primary" data-action="toggle-create-user" type="button" style="min-height:40px;border-radius:8px;font-size:14px">
              <svg class="icon" aria-hidden="true" style="width:15px;height:15px"><use href="#icon-plus"/></svg>
              New user
            </button>
          </div>

          <div class="admin-table-wrap">
            <form id="adminCreateUserForm">
              <input type="email" name="email" placeholder="user@tritorc.com" required>
              <input type="password" name="password" placeholder="Temporary password" required>
              <select name="role" style="min-height:40px;border-radius:8px;border:1px solid var(--line-strong);padding:0 10px">
                <option value="user" selected>User</option>
                <option value="admin">Admin</option>
              </select>
              <button type="submit" class="btn primary" style="min-height:40px;border-radius:8px;font-size:14px">Create</button>
            </form>
            <table class="admin-table">
              <thead>
                <tr>
                  <th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last active</th><th></th>
                </tr>
              </thead>
              <tbody>${userRows}</tbody>
            </table>
          </div>
        </div>
      </div>`;
  } catch (err) {
    container.innerHTML = `<p style="color:var(--danger);padding:var(--space-3)">${escapeHtml(err.message)}</p>`;
  }
}
