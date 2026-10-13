// Service worker för SL Live Map.
// Medvetet konservativ: HTML och /api/ rörs aldrig (network only) så att
// versionsbannern och realtidsdata fungerar exakt som utan service worker.
// Statiska resurser (JS/CSS/bilder/geojson) får stale-while-revalidate.
const CACHE = 'sl-map-static-v1';
const PRECACHE = ['/leaflet.js', '/leaflet.css'];
const STATIC_RE = /\.(js|css|png|jpg|webp|svg|ico|geojson)$/;

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;        // karttiles m.m. → webbläsarens default
  if (url.pathname.startsWith('/api/')) return;      // realtidsdata cache:as aldrig här
  if (!STATIC_RE.test(url.pathname)) return;         // HTML → network only
  if (url.pathname === '/site-config.js') return;    // driftkonfig: alltid färsk, som HTML

  e.respondWith(
    caches.open(CACHE).then(async (c) => {
      const hit = await c.match(e.request);
      const net = fetch(e.request)
        .then((resp) => {
          if (resp.ok) c.put(e.request, resp.clone());
          return resp;
        })
        .catch(() => hit);
      return hit || net;
    })
  );
});
