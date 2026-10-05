export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

export function formatPhoneForDisplay(value, countryCode) {
  if (!value) return "";
  const digits = String(value).replace(/\D/g, "");
  if (!digits) return String(value).trim();
  let codeDigits = String(countryCode || "").replace(/\D/g, "");
  if (!codeDigits && String(value).trim().startsWith("+")) {
    // Same dial-code set as backend/extraction/field_resolver.py's COUNTRY_HINTS
    // (kept in sync manually), longest-first so no shorter code shadows a
    // longer one that's actually a prefix of it (e.g. "971" before "97").
    const knownCodes = [
      "880", "886", "852", "351", "353", "358", "234", "254", "962", "963", "964", "965", "966", "967", "968", "971", "972", "973", "974", "977",
      "20", "27", "30", "31", "32", "33", "34", "39", "41", "43", "44", "45", "46", "47", "48", "49",
      "51", "52", "54", "55", "56", "57", "60", "61", "62", "63", "64", "65", "66",
      "81", "82", "84", "86", "90", "91", "92", "94", "98",
      "1", "7",
    ].sort((a, b) => b.length - a.length);
    codeDigits = knownCodes.find((code) => digits.startsWith(code)) || "";
  }
  let national = digits;
  if (codeDigits && national.startsWith(codeDigits)) national = national.slice(codeDigits.length);
  national = national.replace(/^0+/, "") || digits;
  return codeDigits ? `(+${codeDigits}) ${national}` : national;
}

export function debounce(fn, wait) {
  let timer;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

export function confidenceLevel(record) {
  const label = String(record.confidence_score || "").toLowerCase();
  if (label === "high") return "high";
  if (label === "medium") return "medium";
  return "low";
}

export function isDuplicate(record) {
  return String(record.duplicate_flag || "No").toLowerCase() !== "no";
}

export function normalizeWebsiteUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const candidate = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  try {
    const url = new URL(candidate);
    return url.href;
  } catch {
    return null;
  }
}

// Single definition of "needs your attention" shared by Home's callout card,
// the Records filter, and the review-queue mode — a record qualifies for any
// of those surfaces if and only if this returns true.
//
// Confidence-based flagging is disabled for now: the underlying
// confidence_score has been too noisy/unreliable in practice (see
// backend/main.py's record_from_llm_fields), so it was flooding this with false
// positives. Only real duplicates are surfaced until that's sorted out.
export function needsReview(record) {
  return isDuplicate(record);
}

const AVATAR_TINTS = [
  { bg: "#e7f7f5", fg: "#0b5c55" }, // teal
  { bg: "#fdecd9", fg: "#b1560c" }, // amber
  { bg: "#ece9fb", fg: "#5b47c9" }, // purple
];

export function avatarTint(name) {
  const str = String(name || "?");
  let hash = 0;
  for (let i = 0; i < str.length; i += 1) hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
  return AVATAR_TINTS[hash % AVATAR_TINTS.length];
}

export function initials(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return parts.slice(0, 2).map((part) => part[0].toUpperCase()).join("");
}

// No user account stores a display name (see backend/storage/users.py's
// _PUBLIC_FIELDS — just email/role/active/timestamps), so this derives a
// readable one from the email's local-part: "omkar@tritorc.com" -> "Omkar",
// "michael.ab@micony.com" -> "Michael Ab". Shared by the Admin users table
// and the Settings/Account panel so both show the same name for the same
// account.
export function displayNameFromEmail(email) {
  const local = String(email || "").split("@")[0];
  return local
    .split(/[._-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ") || email;
}
