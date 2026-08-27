const CACHE='eth-radar-pro-v14';
const SHELL=['/','/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png','/static/apple-touch-icon.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.pathname.startsWith('/api/')){e.respondWith(fetch(e.request));return;}e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));});
self.addEventListener('push',event=>{
  let d={}; try{d=event.data?event.data.json():{};}catch(_){d={body:event.data?event.data.text():''};}
  event.waitUntil(self.registration.showNotification(d.title||'ETH Radar PRO',{body:d.body||'',icon:'/static/icon-192.png',badge:'/static/icon-192.png',data:{url:d.url||'/'}}));
});
self.addEventListener('notificationclick',event=>{event.notification.close();event.waitUntil(clients.openWindow(event.notification.data?.url||'/'));});
