// web/public/sw.js — ccswitch Web Push service worker (minimal).
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_e) { data = {}; }
  const title = data.title || 'ccswitch';
  const options = {
    body: data.body || '',
    data: { url: data.url || '/' },
    icon: '/favicon.svg',
    tag: data.tag || 'ccswitch',
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      const navigated = await c.navigate(url).catch(() => null);
      return (navigated || c).focus();
    }
    return clients.openWindow(url);
  })());
});
