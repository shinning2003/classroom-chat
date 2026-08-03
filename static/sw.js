/* Classroom Chat service worker — app-shell cache + push notifications */
const CACHE = 'classroom-v1';
const SHELL = [
  '/',
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
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
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return; // never cache API
  // network-first, cache fallback
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return r;
      })
      .catch(() => caches.match(e.request))
  );
});

self.addEventListener('push', (e) => {
  let data = { title: '💬 Classroom Chat', body: 'New message in the room', tag: 'room', url: '/' };
  if (e.data) {
    try { data = Object.assign(data, e.data.json()); } catch (_) { data.body = e.data.text(); }
  }
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      // App is open and focused — the 2s poll already shows the message, skip the toast
      if (clients.some((c) => c.focused)) return;
      return self.registration.showNotification(data.title, {
        body: data.body,
        icon: '/static/icons/icon-192.png',
        badge: '/static/icons/icon-192.png',
        tag: data.tag || 'room',
        data: { url: data.url || '/' }
      });
    })
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window' }).then((clients) => {
      for (const c of clients) {
        if ('focus' in c) {
          c.focus();
          if ('navigate' in c) c.navigate(url);
          return;
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
