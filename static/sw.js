const CACHE_NAME = "projectonus-v9-cache";
const OFFLINE_URL = "/static/offline.html";
const CORE_ASSETS = [
    "/static/offline.html",
    "/static/manifest.json",
    "/static/icon.svg",
    "/static/icon-192.png",
    "/static/icon-512.png",
    "/static/icon-maskable-512.png",
    "/static/apple-touch-icon.png"
];
const UPLOAD_DB_NAME = "ProjectONusPendingUploads";
const UPLOAD_STORE_NAME = "uploads";
const UPLOAD_DB_VERSION = 1;
const UPLOAD_SYNC_TAG = "projectonus-pending-uploads";

self.addEventListener("install", event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(CORE_ASSETS)).catch(() => {})
    );
    self.skipWaiting();
});

self.addEventListener("activate", event => {
    event.waitUntil(
        caches.keys().then(keys => Promise.all(
            keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
        ))
    );
    self.clients.claim();
});

self.addEventListener("fetch", event => {
    const request = event.request;
    if (request.method !== "GET") return;
    if (!request.url.startsWith(self.location.origin)) return; // let cross-origin pass through
    const url = new URL(request.url);

    // App page navigations: network first, show offline page if there's no connection.
    if (request.mode === "navigate") {
        event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
        return;
    }

    // Static assets: network first, update cache, fall back to cache when offline.
    if (url.pathname.startsWith("/static/")) {
        event.respondWith(
            fetch(request).then(response => {
                const copy = response.clone();
                caches.open(CACHE_NAME).then(cache => {
                    if (response.status === 200) cache.put(request, copy);
                });
                return response;
            }).catch(() => caches.match(request))
        );
        return;
    }
    // Everything else (e.g. stored files, API calls): pass through to the network.
});

function openUploadDb() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(UPLOAD_DB_NAME, UPLOAD_DB_VERSION);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(UPLOAD_STORE_NAME)) {
                db.createObjectStore(UPLOAD_STORE_NAME, { keyPath: "id" });
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}

async function listPendingUploads() {
    const db = await openUploadDb();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(UPLOAD_STORE_NAME, "readonly");
        const request = tx.objectStore(UPLOAD_STORE_NAME).getAll();
        request.onsuccess = () => resolve(request.result || []);
        request.onerror = () => reject(request.error);
    });
}

async function deletePendingUpload(id) {
    const db = await openUploadDb();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(UPLOAD_STORE_NAME, "readwrite");
        tx.objectStore(UPLOAD_STORE_NAME).delete(id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

function formDataFromUpload(entry) {
    const formData = new FormData();
    (entry.entries || []).forEach(item => {
        if (item.kind === "file" && item.file) {
            formData.append(item.name, item.file, item.filename || item.file.name || "ProjectONus_upload");
        } else if (item.kind === "field") {
            formData.append(item.name, item.value || "");
        }
    });
    return formData;
}

async function storePendingUpload(entry) {
    const db = await openUploadDb();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(UPLOAD_STORE_NAME, "readwrite");
        tx.objectStore(UPLOAD_STORE_NAME).put(entry);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

// Must match the page's own queue rules in base.html. This copy runs in the
// background, where nobody is watching, so an upload that can never succeed
// has to be let go rather than retried for ever.
const MAX_UPLOAD_ATTEMPTS = 5;
const UPLOAD_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

async function processPendingUploads() {
    const uploads = await listPendingUploads();
    for (const entry of uploads) {
        const age = entry.createdAt ? Date.now() - new Date(entry.createdAt).getTime() : 0;
        if (isFinite(age) && age > UPLOAD_MAX_AGE_MS) {
            await deletePendingUpload(entry.id);
            continue;
        }
        let permanent = false;
        let reason = "";
        try {
            const response = await fetch(entry.url, {
                method: entry.method || "POST",
                body: formDataFromUpload(entry),
                credentials: "include",
                redirect: "follow"
            });
            if (response.ok) {
                await deletePendingUpload(entry.id);
                continue;
            }
            // The server answered and refused it; asking again changes nothing.
            permanent = response.status >= 400 && response.status < 500;
            reason = "The site answered " + response.status + ".";
        } catch (e) {
            reason = "Could not reach the site.";
        }
        const attempts = (entry.attempts || 0) + 1;
        if (permanent || attempts >= MAX_UPLOAD_ATTEMPTS) {
            await deletePendingUpload(entry.id);
            continue;
        }
        entry.attempts = attempts;
        entry.lastError = reason;
        entry.lastTriedAt = new Date().toISOString();
        try { await storePendingUpload(entry); } catch (ignored) {}
        break;  // network trouble: leave the rest for the next round
    }
}

self.addEventListener("sync", event => {
    if (event.tag === UPLOAD_SYNC_TAG) {
        event.waitUntil(processPendingUploads());
    }
});

self.addEventListener("message", event => {
    if (event.data && event.data.type === "PROJECTONUS_PROCESS_UPLOADS") {
        const work = processPendingUploads().catch(() => {});
        if (event.waitUntil) event.waitUntil(work);
    }
});
