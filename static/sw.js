// Substituted server-side (see app/main.py's service_worker() route) with a
// hash of every static file's mtime+size, so the cache is invalidated
// automatically whenever ANY app-shell asset changes — no more manually
// bumping a version string by hand and shipping a stale cache to real users.
const CACHE_VERSION = "cardscan-__CACHE_VERSION__";
// Substituted server-side too (see app/main.py's service_worker() route):
// the bundled JS/CSS filenames are content-hashed by Vite and change on every
// build, so this list is generated from the build's manifest.json rather than
// hardcoded — a hardcoded list here would 404 on install() the moment the
// bundle output filenames change, breaking cache.addAll() entirely.
const PRECACHE_URLS = __PRECACHE_URLS__;

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
