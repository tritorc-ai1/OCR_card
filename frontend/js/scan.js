import { enqueueCapture } from "./queue.js";

const $ = (selector) => document.querySelector(selector);

let stream = null;
let currentSide = "front";
let frontBlob = null;
let backBlob = null;
// Accumulating multi-shot set — starts empty. There's no explicit "batch
// mode" toggle: pressing the shutter again before confirming the first shot
// is itself the signal that the user is capturing several cards in a row.
let batchItems = []; // { blob, url }
let stats = { processing: 0, done: 0, failed: 0 };
let returnRoute = "#/home";

export function wireScanScreen() {
  $("#scanCloseBtn").addEventListener("click", closeScanScreen);
  $("#scanGalleryBtn").addEventListener("click", () => $("#scanGalleryInput").click());
  $("#scanFallbackGalleryBtn").addEventListener("click", () => $("#scanGalleryInput").click());
  $("#scanGalleryInput").addEventListener("change", handleGalleryChange);
  $("#shutterBtn").addEventListener("click", handleShutter);
  $("#batchDoneBtn").addEventListener("click", finishBatch);
  $("#reviewRetakeBtn").addEventListener("click", retakePhoto);
  $("#reviewAddBackBtn").addEventListener("click", addBackSide);
  $("#reviewUsePhotoBtn").addEventListener("click", usePhoto);
  document.querySelectorAll("#sideToggle button").forEach((btn) => {
    btn.addEventListener("click", () => setSide(btn.dataset.side));
  });
  window.addEventListener("queueItemProcessed", handleQueueEvent);
  window.addEventListener("queueChanged", updateStatusStrip);
}

export async function openScanScreen() {
  const { state } = await import("./app-shell.js");
  if (!state.eventId) {
    const { showToast } = await import("./app-shell.js");
    showToast("Create or select an event first.", "error");
    return;
  }
  returnRoute = window.location.hash || "#/home";
  document.body.classList.add("scan-open");
  $("#scanScreen").classList.add("is-active");
  frontBlob = null;
  backBlob = null;
  setSide("front");
  hideReview();
  await startCamera();
  updateStatusStrip();
}

function closeScanScreen() {
  stopCamera();
  document.body.classList.remove("scan-open");
  $("#scanScreen").classList.remove("is-active");
  resetBatch();
  frontBlob = null;
  backBlob = null;
  hideReview();
  window.location.hash = returnRoute;
}

const CAMERA_ERROR_MESSAGES = {
  NotAllowedError: "Camera permission was blocked. Enable camera access for this site in your browser settings, then reopen Scan.",
  NotFoundError: "No camera was found on this device. You can still import photos from your gallery.",
  NotReadableError: "The camera is already in use by another app. Close it and try again, or import photos instead.",
  OverconstrainedError: "This device's camera doesn't support the requested settings.",
};

const CAMERA_CONSTRAINTS = [
  { video: { facingMode: { ideal: "environment" } } },
  { video: true },
];

async function startCamera() {
  stopCamera();
  $("#scanFallback").classList.remove("is-active");
  $("#scanVideo").style.display = "block";
  prepareVideoElement();

  // getUserMedia only exists in a secure context (HTTPS, or localhost/127.0.0.1
  // for local dev). Over plain http://<lan-ip> — the most common way a phone
  // ends up here during local testing — navigator.mediaDevices is undefined
  // and the camera can never start, no matter what permissions are granted.
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    showFallback("Camera needs a secure connection. Open this app at its https:// address (not a plain http:// local address) to use the camera — you can still import photos from your gallery.");
    return;
  }

  try {
    stream = await openCameraStream();
  } catch (error) {
    showFallback(CAMERA_ERROR_MESSAGES[error?.name] || `Camera could not start (${error?.name || "unknown error"}). You can still import photos from your gallery.`);
    return;
  }

  $("#scanVideo").srcObject = stream;
  if (!(await playVideo())) {
    showFallback("Camera preview couldn't start. You can still import photos from your gallery.");
  }
}

async function openCameraStream() {
  let lastError = null;
  for (const constraints of CAMERA_CONSTRAINTS) {
    try {
      return await navigator.mediaDevices.getUserMedia(constraints);
    } catch (error) {
      lastError = error;
      if (["NotAllowedError", "NotFoundError", "NotReadableError", "SecurityError"].includes(error?.name)) {
        break;
      }
    }
  }
  throw lastError || new Error("Camera unavailable");
}

function prepareVideoElement() {
  const video = $("#scanVideo");
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  video.setAttribute("muted", "");
  video.setAttribute("autoplay", "");
  video.setAttribute("playsinline", "");
  video.setAttribute("webkit-playsinline", "");
}

function hasRenderableVideoFrame(video) {
  return video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0;
}

function waitForVideoFrame(video, timeoutMs = 3500) {
  if (hasRenderableVideoFrame(video)) return Promise.resolve(true);

  return new Promise((resolve) => {
    let done = false;
    let intervalId = null;
    let timeoutId = null;
    const events = ["loadedmetadata", "loadeddata", "canplay", "playing", "resize"];

    const finish = (value) => {
      if (done) return;
      done = true;
      events.forEach((eventName) => video.removeEventListener(eventName, check));
      if (intervalId) window.clearInterval(intervalId);
      if (timeoutId) window.clearTimeout(timeoutId);
      resolve(value);
    };

    const check = () => {
      if (hasRenderableVideoFrame(video)) finish(true);
    };

    events.forEach((eventName) => video.addEventListener(eventName, check));
    intervalId = window.setInterval(check, 100);
    timeoutId = window.setTimeout(() => finish(hasRenderableVideoFrame(video)), timeoutMs);

    if ("requestVideoFrameCallback" in video) {
      video.requestVideoFrameCallback(() => finish(hasRenderableVideoFrame(video)));
    }
  });
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

// A muted, playsinline, autoplay video is allowed to start without any user
// gesture in every modern engine, but right after assigning srcObject the
// stream can still be negotiating — play() occasionally rejects or resolves
// before a frame is actually decoded. That's a transient race, not a real
// autoplay-policy block, so retry silently with backoff instead of stopping
// the camera on a "tap to start" gate.
const PLAY_RETRY_ATTEMPTS = 6;

async function playVideo() {
  const video = $("#scanVideo");
  prepareVideoElement();
  for (let attempt = 0; attempt < PLAY_RETRY_ATTEMPTS; attempt++) {
    const isLastAttempt = attempt === PLAY_RETRY_ATTEMPTS - 1;
    try {
      await video.play();
      if (await waitForVideoFrame(video, isLastAttempt ? 3500 : 400)) return true;
    } catch (error) {
      // fall through to retry below
    }
    if (!isLastAttempt) await sleep(150 * (attempt + 1));
  }
  return false;
}

function showFallback(message) {
  $("#scanFallbackMsg").textContent = message || "Camera access was denied or is unavailable. You can still import photos from your gallery.";
  $("#scanFallback").classList.add("is-active");
  $("#scanVideo").style.display = "none";
}

function stopCamera() {
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }
  const video = $("#scanVideo");
  if (video) {
    video.pause();
    video.srcObject = null;
  }
}

function setSide(side) {
  currentSide = side;
  document.querySelectorAll("#sideToggle button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.side === side);
  });
}

function resetBatch() {
  batchItems.forEach((item) => URL.revokeObjectURL(item.url));
  batchItems = [];
  renderBatchTray();
}

function addToBatch(blob) {
  batchItems.push({ blob, url: URL.createObjectURL(blob) });
  renderBatchTray();
}

async function handleShutter() {
  const blob = await captureFrame();
  if (!blob) {
    const { showToast } = await import("./app-shell.js");
    showToast("Camera isn't ready yet — wait a moment and try again.", "error");
    return;
  }

  // Already accumulating a multi-shot set — keep adding silently to the
  // tray instead of showing a per-shot review (arity-adaptive: once the
  // user is clearly capturing several cards in a row, stay out of their way).
  if (batchItems.length > 0) {
    addToBatch(blob);
    return;
  }

  // A front capture is already awaiting confirmation and the shutter was
  // pressed again — that's the multi-card signal. Fold the pending shot
  // into a new accumulating set along with this one, instead of silently
  // overwriting it.
  if (currentSide === "front" && frontBlob && $("#scanReviewSheet").classList.contains("visible")) {
    const previous = frontBlob;
    frontBlob = null;
    hideReview();
    addToBatch(previous);
    addToBatch(blob);
    return;
  }

  if (currentSide === "front") {
    frontBlob = blob;
  } else {
    backBlob = blob;
  }
  showReviewFrame(blob);
}

// #scanVideo is `object-fit: cover`, so the displayed video is scaled up and
// cropped to fill its box — the guide frame's on-screen position doesn't map
// 1:1 to native video pixels. This inverts that mapping so the captured photo
// is exactly the card-sized rectangle the user saw on screen, not the whole
// (wider) camera view.
function computeGuideSourceRect(video) {
  const guide = $("#scanGuideFrame");
  const videoRect = video.getBoundingClientRect();
  if (!guide || !videoRect.width || !videoRect.height || !video.videoWidth || !video.videoHeight) {
    return null;
  }
  const guideRect = guide.getBoundingClientRect();
  const scale = Math.max(videoRect.width / video.videoWidth, videoRect.height / video.videoHeight);
  const offsetX = (video.videoWidth * scale - videoRect.width) / 2;
  const offsetY = (video.videoHeight * scale - videoRect.height) / 2;

  let sx = (guideRect.left - videoRect.left + offsetX) / scale;
  let sy = (guideRect.top - videoRect.top + offsetY) / scale;
  let sw = guideRect.width / scale;
  let sh = guideRect.height / scale;

  // Defensive clamp against rounding/layout edge cases — never read outside
  // the actual video frame.
  sx = Math.max(0, Math.min(sx, video.videoWidth - 1));
  sy = Math.max(0, Math.min(sy, video.videoHeight - 1));
  sw = Math.max(1, Math.min(sw, video.videoWidth - sx));
  sh = Math.max(1, Math.min(sh, video.videoHeight - sy));
  return { sx, sy, sw, sh };
}

function captureFrame() {
  return new Promise((resolve) => {
    const video = $("#scanVideo");
    if (!hasRenderableVideoFrame(video)) return resolve(null);
    const canvas = $("#scanCanvas");
    const crop = computeGuideSourceRect(video);
    const context = canvas.getContext("2d");
    if (crop) {
      canvas.width = Math.round(crop.sw);
      canvas.height = Math.round(crop.sh);
      context.drawImage(video, crop.sx, crop.sy, crop.sw, crop.sh, 0, 0, canvas.width, canvas.height);
    } else {
      // Guide frame not measurable (e.g. hidden layout) — fall back to the
      // full frame rather than losing the capture.
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
    }
    canvas.toBlob((blob) => resolve(blob), "image/jpeg", 0.9);
  });
}

function renderBatchTray() {
  const tray = $("#batchTray");
  tray.innerHTML = batchItems.map((item, index) => `
    <div class="batch-thumb" data-index="${index}">
      <img src="${item.url}" alt="">
      <button type="button" aria-label="Remove">✕</button>
    </div>
  `).join("");
  tray.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      const index = Number(event.currentTarget.parentElement.dataset.index);
      URL.revokeObjectURL(batchItems[index].url);
      batchItems.splice(index, 1);
      renderBatchTray();
    });
  });
  $("#batchDoneBtn").textContent = `Done (${batchItems.length})`;
  $("#batchDoneBtn").classList.toggle("visible", batchItems.length > 0);
  $("#batchTray").classList.toggle("visible", batchItems.length > 0);
  // Gallery import provides its own multi-photo path; hide it while an
  // in-camera multi-shot set is accumulating to avoid two competing ways in.
  $("#scanGalleryBtn").classList.toggle("is-hidden", batchItems.length > 0);
}

async function finishBatch() {
  if (!batchItems.length) return;
  // Hand the captured blobs to the preview/confirm sheet, then clear the tray.
  const blobs = batchItems.map((item) => item.blob);
  resetBatch();
  const { openProcessSheet } = await import("./process-sheet.js");
  openProcessSheet(blobs.map((blob) => ({ blob })));
}

function showReviewFrame(blob) {
  const url = URL.createObjectURL(blob);
  $("#scanFrozenFrame").src = url;
  $("#scanFrozenFrame").style.display = "block";
  $("#scanReviewSheet").classList.add("visible");
  $("#reviewAddBackBtn").hidden = currentSide === "back";
}

function hideReview() {
  $("#scanReviewSheet").classList.remove("visible");
  $("#scanFrozenFrame").style.display = "none";
}

function retakePhoto() {
  if (currentSide === "front") frontBlob = null; else backBlob = null;
  hideReview();
}

function addBackSide() {
  hideReview();
  setSide("back");
}

async function usePhoto() {
  const { state, showToast } = await import("./app-shell.js");
  hideReview();
  if (currentSide === "back") {
    setSide("front");
  }
  const front = frontBlob;
  const back = backBlob;
  frontBlob = null;
  backBlob = null;
  setSide("front");
  if (!front) return;
  stats.processing += 1;
  updateStatusStrip();
  showToast("Processing…", "info");
  await enqueueAndTrack(state.eventId, front, back);
}

async function enqueueAndTrack(eventId, front, back) {
  await enqueueCapture(eventId, front, back);
}

function handleQueueEvent(event) {
  // Only the single-capture path feeds this aggregate strip now; gallery/batch
  // uploads report their own per-photo status in the process sheet. Update the
  // counters only while we're actually tracking a single capture, but always
  // refresh records so newly processed cards appear regardless of the source.
  if (stats.processing > 0) {
    stats.processing -= 1;
    if (event.detail?.success) stats.done += 1; else stats.failed += 1;
    updateStatusStrip();
  }
  import("./app-shell.js").then(({ refreshAll }) => refreshAll());
}

function updateStatusStrip() {
  const strip = $("#scanStatusStrip");
  const parts = [];
  if (stats.processing) parts.push(`⏳ ${stats.processing} processing`);
  if (stats.done) parts.push(`✓ ${stats.done} done`);
  if (stats.failed) parts.push(`⚠ ${stats.failed} failed`);
  if (!parts.length) {
    strip.classList.remove("visible");
    return;
  }
  strip.textContent = parts.join(" · ");
  strip.classList.add("visible");
}

async function handleGalleryChange() {
  const files = Array.from($("#scanGalleryInput").files || []);
  $("#scanGalleryInput").value = "";
  await importGalleryFiles(files);
}

export async function importGalleryFiles(files) {
  const imageFiles = files.filter((file) => file.type.startsWith("image/"));
  if (!imageFiles.length) return;
  const { state, showToast } = await import("./app-shell.js");
  if (!state.eventId) return showToast("Create or select an event first.", "error");
  const { openProcessSheet } = await import("./process-sheet.js");
  openProcessSheet(imageFiles.map((file) => ({ blob: file, name: file.name })));
}
