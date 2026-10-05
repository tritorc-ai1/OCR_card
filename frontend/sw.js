// Substituted server-side (see backend/main.py's service_worker() route) with a
// hash of every static file's mtime+size, so the cache is invalidated
// automatically whenever ANY app-shell asset changes — no more manually
// bumping a version string by hand and shipping a stale cache to real users.
const CACHE_VERSION = "cardscan-__CACHE_VERSION__";
const PRECACHE_URLS = [
  "/",
  "/manifest.webmanifest",
  "/static/css/tokens.css",
  "/static/css/components.css",
  "/static/css/screens.css",
  "/static/js/app-shell.js",
  "/static/js/api.js",
  "/static/js/utils.js",
  "/static/js/scan.js",
  "/static/js/queue.js",
  "/static/js/process-sheet.js",
  "/static/js/records.js",
  "/static/js/dashboard.js",
  "/static/js/events.js",
  "/static/js/more.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-192-maskable.png",
  "/static/icons/icon-512-maskable.png",
  "/static/fonts/ibm-plex-sans-400.woff2",
  "/static/fonts/ibm-plex-sans-500.woff2",
  "/static/fonts/ibm-plex-sans-600.woff2",
  "/static/fonts/ibm-plex-mono-500.woff2",
];

// Only the immutable app-shell assets (JS/CSS/fonts/icons) are cached, so the
// PWA stays installable. NOTHING data-related is cached in the browser anymore:
// events, cards, usage, health, images and downloads all go straight to the
// network every time, so the UI never shows stale data.
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  // Always refresh the app shell from the network when online so changes to
  // index.html and the top-level UI are not stuck behind an old cached shell.
  if (request.mode === "navigate" || url.pathname === "/" || url.pathname === "/index.html") {
    event.respondWith(networkFirst(request));
    return;
  }

  // Cache-first ONLY for static app-shell assets. Everything else (all data
  // endpoints, images, downloads) is left to the network with no SW caching.
  if (PRECACHE_URLS.includes(url.pathname) || url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
  }
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw new Error("Offline and no cached response available");
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone());
  return response;
}
