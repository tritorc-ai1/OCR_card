import { escapeHtml } from "./utils.js";

function nextMidnightUtcLabel() {
  const now = new Date();
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 0, 0, 0));
  const hoursLeft = Math.max(0, Math.round((next - now) / 3600000));
  return `resets in ~${hoursLeft}h (00:00 UTC)`;
}

function nextMonthLabel() {
  const now = new Date();
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  const daysLeft = Math.max(1, Math.round((next - now) / 86400000));
  return `resets in ~${daysLeft}d (1st of next month)`;
}

function providerRowHtml({ name, configured, used, limit, resetLabel }) {
  if (!configured) {
    return `
      <div class="provider-row provider-row--error">
        <div class="provider-row-head">
          <span class="name">${escapeHtml(name)}</span>
          <span class="status-pill error">Not configured</span>
        </div>
        <div class="provider-row-note">Scanning will fail until an API key is set for this provider. Check the server's .env configuration.</div>
      </div>
    `;
  }

  const safeUsed = Number(used || 0);
  const safeLimit = Number(limit || 0);
  const percent = safeLimit > 0 ? Math.min(100, Math.round((safeUsed / safeLimit) * 100)) : 0;
  const hitLimit = safeLimit > 0 && safeUsed >= safeLimit;
  const nearLimit = !hitLimit && percent >= 80;
  const barState = hitLimit ? "danger" : nearLimit ? "warning" : "";
  const statusPill = hitLimit
    ? `<span class="status-pill error">Limit reached</span>`
    : nearLimit
      ? `<span class="status-pill warning">Near limit</span>`
      : `<span class="status-pill ok">OK</span>`;

  return `
    <div class="provider-row">
      <div class="provider-row-head">
        <span class="name">${escapeHtml(name)}</span>
        ${statusPill}
      </div>
      <div class="provider-row-count">${safeUsed}${safeLimit ? ` / ${safeLimit}` : ""} requests today</div>
      ${safeLimit ? `<div class="meter-track"><div class="meter-fill ${barState}" style="width:${percent}%"></div></div>` : ""}
      ${hitLimit ? `<div class="provider-row-note error">Daily limit reached - new scans will fail until it resets. ${escapeHtml(resetLabel)}.</div>` : ""}
      ${nearLimit && !hitLimit ? `<div class="provider-row-note warning">Approaching the daily limit. ${escapeHtml(resetLabel)}.</div>` : ""}
    </div>
  `;
}

export function usagePanelHtml(usage, health) {
  if (!usage) {
    return `<div class="provider-row-note">Usage unavailable - check the connection.</div>`;
  }

  const mongo = usage.mongo || {};
  const gemini = usage.gemini || usage;
  const vision = usage.google_vision || {};
  const geminiConfigured = health ? Boolean(health.gemini_configured) : true;
  const visionConfigured = health ? Boolean(health.google_vision_configured) : true;

  if (mongo.enabled) {
    return `
      ${mongoStatusRow(mongo)}
      ${providerConfigWarnings({ geminiConfigured, visionConfigured })}
      ${mongoUsageRows(mongo)}
    `;
  }

  return `
    ${providerRowHtml({
      name: "Gemini (field sorting)",
      configured: geminiConfigured,
      used: gemini.daily_requests,
      limit: gemini.daily_request_limit,
      resetLabel: nextMidnightUtcLabel(),
    })}
    ${providerRowHtml({
      name: "Google Vision (OCR)",
      configured: visionConfigured,
      used: vision.daily_requests,
      limit: 0,
      resetLabel: nextMonthLabel(),
    })}
    ${vision.monthly_units != null ? monthlyVisionRow(vision) : ""}
    <div class="provider-row-note warning">MongoDB tracker is not enabled, so usage resets with local app data.</div>
  `;
}

function providerConfigWarnings({ geminiConfigured, visionConfigured }) {
  const rows = [];
  if (!geminiConfigured) rows.push(providerRowHtml({ name: "Gemini (field sorting)", configured: false }));
  if (!visionConfigured) rows.push(providerRowHtml({ name: "Google Vision (OCR)", configured: false }));
  return rows.join("");
}

function mongoStatusRow(mongo) {
  if (mongo.available) {
    return `
      <div class="provider-row">
        <div class="provider-row-head">
          <span class="name">MongoDB 24/7 tracker</span>
          <span class="status-pill ok">Live</span>
        </div>
        <div class="provider-row-note">Persistent counters are stored in MongoDB and survive app restarts, redeploys, and free-tier sleep.</div>
      </div>
    `;
  }

  const blocking = Boolean(mongo.blocking_scans);
  const status = blocking ? "Blocking scans" : "Fallback";
  const message = blocking
    ? "MongoDB is configured but unreachable, so new scans are blocked to keep limits accurate."
    : (mongo.fallback_message || "MongoDB is configured but unreachable. Scans can continue with local counters, but the persistent tracker is not updating.");
  const detail = mongo.error_summary ? ` ${mongo.error_summary}` : "";
  return `
    <div class="provider-row provider-row--error">
      <div class="provider-row-head">
        <span class="name">MongoDB 24/7 tracker</span>
        <span class="status-pill error">${escapeHtml(status)}</span>
      </div>
      <div class="provider-row-note error">${escapeHtml(message + detail)}</div>
    </div>
  `;
}

function mongoUsageRows(mongo) {
  if (!mongo.available) return "";
  const rows = [];
  if (mongo.google_vision) {
    rows.push(mongoCreditRow({
      name: "Google Vision monthly cap",
      unit: "OCR units",
      data: mongo.google_vision,
      resetLabel: nextMonthLabel(),
    }));
  }
  if (mongo.gemini) {
    rows.push(mongoCreditRow({
      name: "Gemini daily cap",
      unit: "requests",
      data: mongo.gemini,
      resetLabel: nextMidnightUtcLabel(),
    }));
  }
  return rows.join("");
}

function mongoCreditRow({ name, unit, data, resetLabel }) {
  const used = Number(data.used || 0);
  const limit = Number(data.limit || 0);
  const tokens = Number(data.tokens_estimated || 0);
  const percent = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const hitLimit = Boolean(data.hit_limit);
  const nearLimit = !hitLimit && percent >= 80;
  const barState = hitLimit ? "danger" : nearLimit ? "warning" : "";
  const statusPill = hitLimit
    ? `<span class="status-pill error">Limit reached</span>`
    : nearLimit
      ? `<span class="status-pill warning">Near limit</span>`
      : `<span class="status-pill ok">OK</span>`;
  const tokenText = tokens ? `, ${tokens} estimated tokens` : "";
  return `
    <div class="provider-row">
      <div class="provider-row-head">
        <span class="name">${escapeHtml(name)}</span>
        ${statusPill}
      </div>
      <div class="provider-row-count">${used} / ${limit} ${escapeHtml(unit)}${tokenText} - ${escapeHtml(resetLabel)}</div>
      ${limit ? `<div class="meter-track"><div class="meter-fill ${barState}" style="width:${percent}%"></div></div>` : ""}
      ${hitLimit ? `<div class="provider-row-note error">Limit reached - new scans are blocked until it resets. ${escapeHtml(resetLabel)}.</div>` : ""}
      ${nearLimit ? `<div class="provider-row-note warning">Approaching the limit. ${escapeHtml(resetLabel)}.</div>` : ""}
    </div>
  `;
}

function monthlyVisionRow(vision) {
  const used = Number(vision.monthly_units || 0);
  const limit = Number(vision.free_units_monthly || 0);
  const percent = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const hitLimit = limit > 0 && used >= limit;
  const barState = hitLimit ? "danger" : percent >= 80 ? "warning" : "";
  return `
    <div class="provider-row">
      <div class="provider-row-head">
        <span class="name">Vision free tier (this month)</span>
        ${hitLimit ? `<span class="status-pill warning">Billing active</span>` : `<span class="status-pill ok">Within free tier</span>`}
      </div>
      <div class="provider-row-count">${used}${limit ? ` / ${limit}` : ""} OCR units - ${nextMonthLabel()}</div>
      ${limit ? `<div class="meter-track"><div class="meter-fill ${barState}" style="width:${percent}%"></div></div>` : ""}
      ${hitLimit ? `<div class="provider-row-note warning">Free monthly OCR units used up - additional scans are now billed.</div>` : ""}
    </div>
  `;
}
