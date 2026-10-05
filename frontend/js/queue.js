import * as api from "./api.js";

// The offline IndexedDB capture queue has been removed: nothing is cached in the
// browser anymore. Every capture is uploaded to the server (MongoDB); if the
// upload fails, it is reported as failed instead of being persisted for later.
//
// A small in-memory list tracks items only for the current page session (so the
// process sheet's per-photo status and Retry button still work) and drains them
// ONE AT A TIME so a bulk batch doesn't fire dozens of concurrent OCR calls.
// Nothing here survives a reload — by design.

let seq = 0;
let draining = false;
let stopRequested = false; // Flag to halt processing immediately
let activeController = null; // AbortController for the request currently in flight
const items = new Map(); // id -> { id, eventId, frontBlob, backBlob, capturedAt, status }

function snapshot() {
  return [...items.values()].map((it) => ({
    id: it.id,
    eventId: it.eventId,
    capturedAt: it.capturedAt,
    status: it.status,
    attempts: 0,
    hasBack: Boolean(it.backBlob),
    thumbUrl: it.frontBlob ? URL.createObjectURL(it.frontBlob) : "",
  }));
}

function emitChange() {
  window.dispatchEvent(new CustomEvent("queueChanged", { detail: { items: snapshot() } }));
}

export async function initQueue() {
  // Nothing to restore — kept for API compatibility with app-shell.
}

export function getQueueSnapshot() {
  return snapshot();
}

export function isStopRequested() {
  return stopRequested;
}

// Returns immediately with a record whose id the caller can track; the actual
// upload happens in the background drain so callers can correlate before the
// queueItemProcessed event fires.
export async function enqueueCapture(eventId, frontBlob, backBlob) {
  const id = (seq += 1);
  const record = {
    id,
    eventId,
    frontBlob,
    backBlob: backBlob || null,
    capturedAt: new Date().toISOString(),
    status: "pending",
  };
  items.set(id, record);
  emitChange();
  drain();
  return record;
}

export async function retryQueueItem(id) {
  const record = items.get(id);
  if (!record) return;
  record.status = "pending";
  emitChange();
  drain();
}

export function removeQueueItem(id) {
  if (items.delete(id)) emitChange();
}

// Terminates the batch immediately: aborts the current upload and removes
// all pending items from the queue. Nothing gets uploaded after this is called.
export function stopProcessing() {
  stopRequested = true;
  if (activeController) activeController.abort();
  // Remove all pending items from the queue entirely (terminate)
  for (const id of items.keys()) {
    const record = items.get(id);
    if (record.status === "pending") {
      items.delete(id);
    }
  }
  emitChange();
}

async function drain() {
  if (draining || stopRequested) return;
  draining = true;
  try {
    let next = nextPending();
    while (next && !stopRequested) {
      await uploadItem(next);
      if (stopRequested) break;
      next = nextPending();
    }
  } finally {
    draining = false;
    stopRequested = false; // Reset flag so future batches can start
  }
}

function nextPending() {
  for (const record of items.values()) {
    if (record.status === "pending") return record;
  }
  return null;
}

async function uploadItem(record) {
  record.status = "uploading";
  emitChange();
  activeController = new AbortController();
  try {
    await api.uploadCard(
      record.eventId, record.frontBlob, "front.jpg", record.backBlob, "back.jpg", activeController.signal
    );
    items.delete(record.id);
    emitChange();
    window.dispatchEvent(
      new CustomEvent("queueItemProcessed", { detail: { id: record.id, success: true } })
    );
  } catch (error) {
    if (error.name === "AbortError") {
      record.status = "cancelled";
      emitChange();
      window.dispatchEvent(
        new CustomEvent("queueItemProcessed", { detail: { id: record.id, success: false, cancelled: true } })
      );
      return;
    }
    record.status = "failed";
    emitChange();
    window.dispatchEvent(
      new CustomEvent("queueItemProcessed", {
        detail: { id: record.id, success: false, error: error.message, status: error.status },
      })
    );
  } finally {
    activeController = null;
  }
}

// No persistent queue to flush; kept so existing listeners/dispatchers stay harmless.
export function processQueue() {}
