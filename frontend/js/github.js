/**
 * GitHub API client for reading/writing the encrypted database.
 *
 * Read:  raw.githubusercontent.com (binary, no base64 overhead for 57MB file)
 * Write: Git Data API (blobs → tree → commit → update ref) for large files
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

async function _getLatestCommitSha(owner, repo, branch, token) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/ref/heads/${branch}`,
    { headers: _ghHeaders(token) }
  );
  if (res.status === 404) {
    throw new Error(`Branch '${branch}' not found. Has the workflow run at least once?`);
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub API error ${res.status}: ${body}`);
  }
  const data = await res.json();
  return data.object.sha;
}

async function _fetchBlobBytes(owner, repo, blobSha, token) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/blobs/${blobSha}`,
    {
      headers: {
        Authorization: `token ${token}`,
        Accept: "application/vnd.github.raw",
      },
    }
  );
  if (!res.ok) throw new Error(`Failed to download blob ${blobSha}: ${res.status}`);
  const buffer = await res.arrayBuffer();
  return new Uint8Array(buffer);
}

async function _fetchShardManifest(owner, repo, branch, token) {
  // Walk the data branch tree to find icourse-index.enc + every shard file.
  // Returns:
  //   { commitSha, format: "sharded" | "legacy",
  //     index?: { sha, name },          // sharded only
  //     shards?: [{ name, sha, size }], // sharded only
  //     legacy?: { name, sha, compressed } }  // legacy only
  const commitSha = await _getLatestCommitSha(owner, repo, branch, token);

  const commitRes = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/commits/${commitSha}`,
    { headers: _ghHeaders(token) }
  );
  if (!commitRes.ok) throw new Error(`Failed to get commit: ${commitRes.status}`);
  const treeSha = (await commitRes.json()).tree.sha;

  const treeRes = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/trees/${treeSha}?recursive=1`,
    { headers: _ghHeaders(token) }
  );
  if (!treeRes.ok) throw new Error(`Failed to get tree: ${treeRes.status}`);
  const tree = (await treeRes.json()).tree;

  const indexEntry = tree.find((e) => e.path === "data/icourse-index.enc");
  if (indexEntry) {
    const shards = tree
      .filter((e) => e.type === "blob" && e.path.startsWith("data/shards/"))
      .map((e) => ({
        name: e.path.slice("data/shards/".length),
        sha: e.sha,
        size: e.size,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return {
      commitSha,
      format: "sharded",
      index: { sha: indexEntry.sha, name: "icourse-index.enc" },
      shards,
    };
  }

  // Legacy fallback (single-file format)
  const gz = tree.find((e) => e.path === "data/icourse.db.gz.enc");
  const raw = tree.find((e) => e.path === "data/icourse.db.enc");
  const legacy = gz || raw;
  if (!legacy) throw new Error("Database not found on data branch.");
  return {
    commitSha,
    format: "legacy",
    legacy: {
      name: legacy.path.slice("data/".length),
      sha: legacy.sha,
      compressed: !!gz,
    },
  };
}

/* ── Actions secrets API ────────────────────────────────────────────────
   Reads/writes repository-level Action secrets.  The encryption is
   libsodium ``crypto_box_seal`` against a per-repo public key — same
   scheme GitHub's REST docs show.  Requires the PAT to grant
   ``Secrets: Read and write`` (the repo-level secrets permission, NOT
   the org-level one).  Without that scope you get 403 from both endpoints.

   We rely on libsodium-wrappers being loaded as a side-effect on the
   page (it's listed in index.html before this script).  If the user
   landed on a page that didn't include it, _ensureSodium() throws.
*/

let _sodiumPromise = null;
function _ensureSodium() {
  if (!_sodiumPromise) {
    if (typeof window.sodium === "undefined" || !window.sodium.ready) {
      throw new Error(
        "libsodium-wrappers not loaded — make sure both libsodium and " +
        "libsodium-wrappers CDN <script> tags are present before js/github.js"
      );
    }
    _sodiumPromise = window.sodium.ready.then(() => window.sodium);
  }
  return _sodiumPromise;
}

async function _getRepoPublicKey(owner, repo, token) {
  // Returns { key, key_id } where key is base64-encoded libsodium public key.
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/public-key`,
    { headers: _ghHeaders(token) }
  );
  if (res.status === 403 || res.status === 404) {
    const body = await res.text();
    throw new Error(
      "无法读取仓库 Secrets 公钥。请确认你的 GitHub PAT 已开启 " +
      "Secrets: Read and write 权限（这是 fine-grained PAT 的独立条目，" +
      "不包含在 Contents 或 Actions 里）。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub API error ${res.status}: ${body}`);
  }
  return await res.json();
}

async function _putRepoSecret(owner, repo, token, secretName,
                              encryptedB64, keyId) {
  // PUT /repos/{owner}/{repo}/actions/secrets/{secret_name}
  // 201 = created, 204 = updated.  Anything else is fatal.
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/${encodeURIComponent(secretName)}`,
    {
      method: "PUT",
      headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({
        encrypted_value: encryptedB64,
        key_id: keyId,
      }),
    }
  );
  if (res.status === 201 || res.status === 204) return;
  const body = await res.text();
  if (res.status === 403) {
    throw new Error(
      "无权限写入 Secret。请确认 PAT 的 Secrets 权限设为 Read and write。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _getCourseIdsSecretInfo(owner, repo, token) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/COURSE_IDS`,
    { headers: _ghHeaders(token) },
  );
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`无法检查 COURSE_IDS Secret：${res.status}`);
  return await res.json();
}

async function _setCourseIdsSecret(owner, repo, token, courseIds) {
  // Convenience: encrypts the comma-joined COURSE_IDS list against the
  // repo's public key and writes it as the ``COURSE_IDS`` secret in one
  // round-trip.  Returns the canonical list that was written so the UI
  // can confirm the saved state.
  const sodium = await _ensureSodium();
  const pub = await _getRepoPublicKey(owner, repo, token);
  const value = (Array.isArray(courseIds) ? courseIds : [])
    .map(String)
    .map((s) => s.trim())
    .filter(Boolean)
    .join(",");
  // sealed box: anyone with the public key can encrypt; only the matching
  // private key (held by GitHub Actions runner) can decrypt.
  const cipher = sodium.crypto_box_seal(
    sodium.from_string(value),
    sodium.from_base64(pub.key, sodium.base64_variants.ORIGINAL),
  );
  const cipherB64 = sodium.to_base64(
    cipher, sodium.base64_variants.ORIGINAL,
  );
  await _putRepoSecret(owner, repo, token, "COURSE_IDS", cipherB64, pub.key_id);
  return value;
}

async function _triggerSingleRunWorkflow(owner, repo, ref, token, courseIds, useOfficial) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/single_run.yml/dispatches`;
  const ids = (Array.isArray(courseIds) ? courseIds : [])
    .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  if (!ids) throw new Error("单次运行列表为空");
  const inputs = { course_ids: ids };
  if (useOfficial) inputs.use_official_transcript = "true";
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: ref || "main",
      inputs: inputs,
    }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发 single_run workflow。请确认 PAT 已开启 Actions: Read and write " +
      `权限，且 single_run.yml 存在于 ref '${ref || "main"}'。服务端返回：${res.status} ${body}`
    );
  }
  if (res.status === 422) {
    throw new Error(
      "触发失败 (422)：通常是 inputs 不匹配 workflow 定义，或 single_run.yml " +
      `不存在于指定分支 '${ref || "main"}'。服务端返回：${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _triggerDeleteWorkflow(owner, repo, ref, token, courseIds, subIds) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/delete_course.yml/dispatches`;
  const ids = (Array.isArray(courseIds) ? courseIds : [])
    .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  if (!ids) throw new Error("删除列表为空");
  var inputs = { course_ids: ids };
  if (subIds && subIds.length) {
    inputs.sub_ids = (Array.isArray(subIds) ? subIds : [])
      .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  }
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: ref || "main",
      inputs: inputs,
    }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发删除 workflow。请确认你的 GitHub PAT 已开启 " +
      "Actions: Read and write 权限。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _triggerExportWorkflow(
  owner, repo, ref, token, courseId, exportType, subIds
) {
  // Fires the existing .github/workflows/export.yml workflow_dispatch.
  // The workflow runs scripts/export_course.py (WeasyPrint) and emails
  // the resulting PDF to RECEIVER_EMAIL — same output the user gets when
  // triggering the workflow manually from the Actions UI.
  //
  // Requires the PAT to grant Actions: Write (in addition to Contents:RW).
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/export.yml/dispatches`;
  const payload = {
    ref,
    inputs: {
      course_id: String(courseId),
      export_type: exportType || "PDF",
      sub_ids: subIds || "",
    },
  };
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 204) return; // success — workflow_dispatch returns 204 No Content
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发导出 workflow。请确认你的 GitHub PAT 已开启 " +
      "Actions: Read and write 权限（Contents 权限不足以触发 workflow）。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  if (res.status === 422) {
    throw new Error(
      "触发失败 (422)：通常是 inputs 不匹配 workflow 定义，或 export.yml " +
      `不存在于指定分支 '${ref}'。服务端返回：${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

window.ICS.github = {
  detectRepo: _detectRepo,
  getLatestCommitSha: _getLatestCommitSha,
  fetchBlobBytes: _fetchBlobBytes,
  fetchShardManifest: _fetchShardManifest,
  triggerExportWorkflow: _triggerExportWorkflow,
  triggerDeleteWorkflow: _triggerDeleteWorkflow,
  triggerSingleRunWorkflow: _triggerSingleRunWorkflow,
  getRepoPublicKey: _getRepoPublicKey,
  putRepoSecret: _putRepoSecret,
  setCourseIdsSecret: _setCourseIdsSecret,
  getCourseIdsSecretInfo: _getCourseIdsSecretInfo,
};
