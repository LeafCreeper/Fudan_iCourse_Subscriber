/**
 * GitHub API client — reduced to repo detection, Secrets API, and workflow triggers.
 * (V3: Data fetching is now handled directly via same-origin fetch in app.js)
 */

window.ICS = window.ICS || {};

const _GH_API = "https://api.github.com";

function _ghHeaders(token) {
  return {
    Authorization: `token ${token}`,
    Accept: "application/vnd.github+json",
  };
}

function _detectRepo() {
  const host = location.hostname;
  const path = location.pathname;
  if (host.endsWith(".github.io")) {
    const owner = host.replace(".github.io", "");
    const repo = path.split("/").filter(Boolean)[0];
    if (owner && repo) return { owner, repo };
  }
  return null;
}

/* ── Actions secrets API (requires libsodium for sealed-box encryption) ── */

let _sodiumPromise = null;
function _ensureSodium() {
  if (!_sodiumPromise) {
    if (typeof window.sodium === "undefined" || !window.sodium.ready) {
      throw new Error(
        "libsodium-wrappers not loaded — make sure CDN scripts are present"
      );
    }
    _sodiumPromise = window.sodium.ready.then(() => window.sodium);
  }
  return _sodiumPromise;
}

async function _getRepoPublicKey(owner, repo, token) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/public-key`,
    { headers: _ghHeaders(token) }
  );
  if (!res.ok) {
    const body = await res.text();
    throw new Error(
      "无法读取仓库 Secrets 公钥。请确认 PAT 已开启 Secrets: Read and write 权限。" +
      `(${res.status}: ${body})`
    );
  }
  return await res.json();
}

async function _putRepoSecret(owner, repo, token, secretName, encryptedB64, keyId) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/${encodeURIComponent(secretName)}`,
    {
      method: "PUT",
      headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({ encrypted_value: encryptedB64, key_id: keyId }),
    }
  );
  if (res.status === 201 || res.status === 204) return;
  const body = await res.text();
  throw new Error(`写入 Secret 失败: ${res.status} ${body}`);
}

async function _setCourseIdsSecret(owner, repo, token, courseIds) {
  const sodium = await _ensureSodium();
  const pub = await _getRepoPublicKey(owner, repo, token);
  const value = (Array.isArray(courseIds) ? courseIds : [])
    .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  const cipher = sodium.crypto_box_seal(
    sodium.from_string(value),
    sodium.from_base64(pub.key, sodium.base64_variants.ORIGINAL),
  );
  const cipherB64 = sodium.to_base64(cipher, sodium.base64_variants.ORIGINAL);
  await _putRepoSecret(owner, repo, token, "COURSE_IDS", cipherB64, pub.key_id);
  return value;
}

async function _triggerSingleRunWorkflow(owner, repo, ref, token, courseIds) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/single_run.yml/dispatches`;
  const ids = (Array.isArray(courseIds) ? courseIds : [])
    .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  if (!ids) throw new Error("单次运行列表为空");
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: ref || "main", inputs: { course_ids: ids } }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  throw new Error(`触发 single_run 失败: ${res.status} ${body}`);
}

async function _triggerCheckWorkflow(owner, repo, ref, token) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/check.yml/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: ref || "main" }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  throw new Error(`触发 check 失败: ${res.status} ${body}`);
}

window.ICS.github = {
  detectRepo: _detectRepo,
  setCourseIdsSecret: _setCourseIdsSecret,
  triggerSingleRunWorkflow: _triggerSingleRunWorkflow,
  triggerCheckWorkflow: _triggerCheckWorkflow,
};
