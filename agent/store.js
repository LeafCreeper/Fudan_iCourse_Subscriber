const storeName = "sessions";
const configKey = "ics_agent_config";
const activeKey = "ics_agent_active";
const settingsVersion = 2;
let opened;

function open() {
  opened ??= new Promise((resolve, reject) => {
    const request = indexedDB.open("ics_agent", 1);
    request.onupgradeneeded = () => request.result.createObjectStore(storeName, { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return opened;
}

async function access(mode, operation) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const request = operation(tx.objectStore(storeName));
    tx.oncomplete = () => resolve(request.result);
    tx.onabort = () => reject(tx.error);
  });
}

export const sessions = {
  list: () => access("readonly", store => store.getAll()),
  save: session => access("readwrite", store => store.put(session)),
  remove: id => access("readwrite", store => store.delete(id)),
  get active() { return localStorage.getItem(activeKey); },
  set active(value) { localStorage.setItem(activeKey, value); },
};

export function loadConfig(defaultConfig) {
  const stored = JSON.parse(localStorage.getItem(configKey) ?? "{}");
  // Earlier versions also saved the default as a literal value.
  if (!stored.settingsVersion && stored.contextChars === 750000) delete stored.contextChars;
  return { ...defaultConfig, ...stored };
}

export function saveConfig(config) {
  const copy = { ...config, settingsVersion };
  if (!copy.rememberKey) delete copy.apiKey;
  localStorage.setItem(configKey, JSON.stringify(copy));
}
