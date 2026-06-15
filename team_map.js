const CACHE_NAME = "projectonus-v6-cache";
const CORE_ASSETS = ["/static/manifest.json", "/static/icon.svg"];
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
    const url = new URL(request.url);
    if (!request.url.startsWith(self.location.origin) || !url.pathname.startsWith("/static/")) {
        event.respondWith(fetch(request));
        return;
    }
    event.respondWith(
        fetch(request).then(response => {
            const copy = response.clone();
            caches.open(CACHE_NAME).then(cache => {
                if (response.status === 200) cache.put(request, copy);
            });
            return response;
        }).catch(() => caches.match(request))
    );
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

async function processPendingUploads() {
    const uploads = await listPendingUploads();
    for (const entry of uploads) {
        const response = await fetch(entry.url, {
            method: entry.method || "POST",
            body: formDataFromUpload(entry),
            credentials: "include",
            redirect: "follow"
        });
        if (!response.ok) throw new Error("Pending upload failed");
        await deletePendingUpload(entry.id);
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
