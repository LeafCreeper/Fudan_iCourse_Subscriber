/**
 * Alpine.js app — all state, routing, and view logic for the iCourse frontend.
 * References ICS.crypto, ICS.github, ICS.db, ICS.render globals.
 */

/* ── Gzip helpers (Compression Streams API) ── */
async function _gunzip(compressedBytes) {
  var ds = new DecompressionStream("gzip");
  var writer = ds.writable.getWriter();
  writer.write(compressedBytes);
  writer.close();
  var chunks = [];
  var reader = ds.readable.getReader();
  while (true) {
    var r = await reader.read();
    if (r.done) break;
    chunks.push(r.value);
  }
  var total = chunks.reduce(function(s, c) { return s + c.length; }, 0);
  var result = new Uint8Array(total);
  var offset = 0;
  for (var i = 0; i < chunks.length; i++) {
    result.set(chunks[i], offset);
    offset += chunks[i].length;
  }
  return result;
}

/* ── IndexedDB cache for decrypted shards (keyed by git blob sha) ────
   Shard contents are content-addressed: a shard's git blob sha changes
   only when its bytes change, so we can keep decrypted bytes around and
   skip the network + decrypt + decompress chain on subsequent loads.
*/
var _idbName = "ics_cache_v2";
// Cache the open Promise — every shard load fires _idbGet + _idbPut, and
// without this each call opens its own connection.  Reopening costs ~10ms
// on cold caches and accumulates fast over 100+ shard reads.
var _idbPromise = null;

function _idbOpen() {
  if (_idbPromise) return _idbPromise;
  _idbPromise = new Promise(function(resolve, reject) {
    var req = indexedDB.open(_idbName, 1);
    req.onupgradeneeded = function() { req.result.createObjectStore("blobs"); };
    req.onsuccess = function() { resolve(req.result); };
    req.onerror = function() {
      // Don't cache failed opens — next call retries cleanly.
      _idbPromise = null;
      reject(req.error);
    };
  });
  return _idbPromise;
}

async function _idbGet(key) {
  var db = await _idbOpen();
  return new Promise(function(resolve) {
    var tx = db.transaction("blobs", "readonly");
    var req = tx.objectStore("blobs").get(key);
    req.onsuccess = function() { resolve(req.result || null); };
    req.onerror = function() { resolve(null); };
  });
}

async function _idbPut(key, value) {
  var db = await _idbOpen();
  return new Promise(function(resolve) {
    var tx = db.transaction("blobs", "readwrite");
    tx.objectStore("blobs").put(value, key);
    tx.oncomplete = function() { resolve(); };
    tx.onerror = function() { resolve(); };
  });
}

/* ── Credential helpers (localStorage) ── */
const _LS = "ics_";
const _loadCreds = () => { try { return JSON.parse(localStorage.getItem(_LS + "creds")); } catch { return null; } };
const _saveCreds = (c) => localStorage.setItem(_LS + "creds", JSON.stringify(c));
const _loadSettings = () => { try { return JSON.parse(localStorage.getItem(_LS + "settings")) || {}; } catch { return {}; } };
const _saveSettings = (s) => localStorage.setItem(_LS + "settings", JSON.stringify(s));
/* Starred-course IDs are per-browser (localStorage). The school side
   doesn't need to know; the user just wants their favorites pinned to
   the top of their own view. */
const _loadStarred = () => {
  try { return new Set(JSON.parse(localStorage.getItem(_LS + "starred")) || []); }
  catch { return new Set(); }
};
const _saveStarred = (set) => localStorage.setItem(
  _LS + "starred", JSON.stringify(Array.from(set))
);

function _relativeTime(iso) {
  if (!iso) return "";
  const d = Date.now() - new Date(iso).getTime();
  const m = Math.floor(d / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  const days = Math.floor(h / 24);
  if (days < 30) return days + "d ago";
  return new Date(iso).toLocaleDateString();
}

function _highlightSnippet(text, query, radius) {
  radius = radius || 60;
  if (!text || !query) return "";
  const plain = ICS.render.plainSnippet(text, 99999);
  const idx = plain.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return plain.slice(0, 120) + "...";
  const s = Math.max(0, idx - radius);
  const e = Math.min(plain.length, idx + query.length + radius);
  let snip = (s > 0 ? "..." : "") + plain.slice(s, e) + (e < plain.length ? "..." : "");
  const re = new RegExp("(" + query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
  return snip.replace(re, "<mark>$1</mark>");
}

function _formatTimestamp(seconds) {
  var sec = Math.max(0, Math.floor(seconds || 0));
  var h = Math.floor(sec / 3600);
  var m = Math.floor((sec % 3600) / 60);
  var s = sec % 60;
  function pad(n) { return String(n).padStart(2, "0"); }
  if (h > 0) return pad(h) + ":" + pad(m) + ":" + pad(s);
  return pad(m) + ":" + pad(s);
}

/* Three-state detail view: summary → transcript → ppt → summary.
   The button label always shows the *next* state so the user can read it
   as an action ("切换到转录"). */
const _DETAIL_VIEW_CYCLE = ["summary", "transcript", "ppt"];
const _DETAIL_VIEW_LABEL = {
  summary: "摘要",
  transcript: "转录",
  ppt: "PPT 识别",
};

/* ── Sharded loading helpers ── */
async function _loadShard(owner, repo, entry, password, token) {
  // Returns { bytes, downloaded: bool } — downloaded=true means we hit
  // the network; false means IndexedDB cache hit (instant).
  var cacheKey = "shard:" + entry.sha;
  var cached = await _idbGet(cacheKey);
  if (cached) return { bytes: cached, downloaded: false };

  var encBytes = await ICS.github.fetchBlobBytes(owner, repo, entry.sha, token);
  var gzipped = await ICS.crypto.decrypt(
    encBytes, password, ICS.crypto.NEW_ITERATIONS,
  );
  if (!ICS.crypto.isGzip(gzipped)) {
    throw new Error(
      "Shard '" + entry.name + "' decrypted to non-gzip bytes — wrong key?"
    );
  }
  var dbBytes = await _gunzip(gzipped);
  await _idbPut(cacheKey, dbBytes);
  return { bytes: dbBytes, downloaded: true };
}

async function _fetchAndDecryptIndex(owner, repo, indexSha, password, token) {
  var indexEnc = await ICS.github.fetchBlobBytes(owner, repo, indexSha, token);
  var indexBytes = await ICS.crypto.decrypt(
    indexEnc, password, ICS.crypto.NEW_ITERATIONS,
  );
  if (!ICS.crypto.isJsonObj(indexBytes)) {
    throw new Error("Shard index decrypted to non-JSON bytes — wrong key?");
  }
  return JSON.parse(new TextDecoder().decode(indexBytes));
}

async function _loadFromShardManifest(manifest, owner, repo, password, token, progress) {
  // 0) Check if the full merged DB is already cached for this commit SHA.
  //    If the data branch hasn't moved, we can skip ALL shard loading.
  var mergedKey = "merged:" + manifest.commitSha;
  var cachedMerged = await _idbGet(mergedKey);
  if (cachedMerged) {
    await ICS.db.initDB(cachedMerged);
    return;
  }

  // 1) Load index: check if index SHA matches cached → reuse decrypted JSON;
  //    otherwise fetch + decrypt + cache for next time.
  var cachedIndexSha = null;
  try { cachedIndexSha = localStorage.getItem(_LS + "indexSha"); } catch (e) {}

  var index = null;
  if (manifest.index.sha === cachedIndexSha) {
    index = await _idbGet("index:v2:" + manifest.index.sha);
  }

  if (!index) {
    index = await _fetchAndDecryptIndex(
      owner, repo, manifest.index.sha, password, token,
    );
    await _idbPut("index:v2:" + manifest.index.sha, index);
    try { localStorage.setItem(_LS + "indexSha", manifest.index.sha); } catch (e) {}
  }

  // 2) Pull every shard — skip cache hits, download only changed ones.
  await ICS.db.initEmpty();
  var total = (index.shards || []).length;
  var downloaded = 0;
  for (var i = 0; i < total; i++) {
    var shardMeta = index.shards[i];
    var entry = manifest.shards.find(function (s) { return s.name === shardMeta.name; });
    if (!entry) {
      console.warn("Shard listed in index but missing from tree:", shardMeta.name);
      continue;
    }
    var result = await _loadShard(owner, repo, entry, password, token);
    if (result.downloaded) {
      downloaded++;
      if (progress) progress(downloaded, total, shardMeta.name);
    }
    await ICS.db.attachShard(result.bytes);
  }
  if (progress && downloaded === 0) progress(0, total, "");

  // 3) Cache the merged DB in IndexedDB so next load with the same commit
  //    SHA skips EVERYTHING — no index fetch, no shard iteration, no merge.
  try {
    var merged = ICS.db.exportDB();
    await _idbPut(mergedKey, merged);
  } catch (e) { /* non-critical — silently skip */ }
}

async function _loadFromLegacyBlob(manifest, owner, repo, secrets, token) {
  // Single-file fallback for users still on the pre-shard data branch.
  var encBytes = await ICS.github.fetchBlobBytes(
    owner, repo, manifest.legacy.sha, token,
  );
  var validator = manifest.legacy.compressed
    ? ICS.crypto.isGzip
    : ICS.crypto.isSqlite;
  var fallback = await ICS.crypto.decryptWithFallback(
    encBytes, secrets, validator,
  );
  var bytes = fallback.data;
  if (manifest.legacy.compressed) {
    bytes = await _gunzip(bytes);
  }
  await ICS.db.initDB(bytes);
}

/* ── Alpine app ── */
document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    view: "loading", error: null, loadingMsg: "",
    toast: null, toastType: "success",
    courses: [], lectures: [],
    currentCourse: null, currentLecture: null,
    currentPptPages: [],
    detailView: "summary",
    searchQuery: "", searchResults: [],
    searchCourseFilterQuery: "", searchSelectedCourseIds: [],
    searchCourseOpen: false,
    searchPage: 1, searchHasMore: false,
    searchDomains: { summary: true, transcript: true, ocr: true },
    commitSha: null,
    setup: { token: "", stuid: "", uispsw: "" },
    setupError: "", setupTesting: false,
    settingsForm: {}, showSecrets: {},
    exportDialogOpen: false, exportSelection: {}, exportingPdf: false,
    repoOwner: "", repoName: "", dataBranch: "data",
    _history: [],
    /* Subscriptions editor state — three-column layout:
       left (subscribed) | middle (catalog search) | right (single-run).
       The left column persists to GitHub Secret on demand; the right
       column is session-only and cleared after triggering a workflow.
       Columns are driven by SQL queries against the in-memory shard DB
       so we never hold the full 20k-row catalog in JS. */
    allCoursesTerms: [],
    subsTerms: [], subsDepts: [], deptSearchQuery: "",
    subsSearchTitle: "", subsSearchTeacher: "",
    subsTermOpen: false, subsDeptOpen: false,
    subscribedIds: [], singleRunIds: [],
    subsSelLeft: [], subsSelMiddle: [], subsSelRight: [],
    subsFiltered: [], subsFilteredTotal: 0,
    subsLimit: 200,
    _subsSubscribedCache: [], _subsSingleRunCache: [], _subsDeptCache: [],
    _subsFilterTimer: null, _deptFilterTimer: null,
    subsSaving: false, subsError: "",
    singleRunTriggering: false,
    /* Per-browser pinned-courses set, lazily synced to localStorage. */
    starred: _loadStarred(),

    async init() {
      const detected = ICS.github.detectRepo();
      const s = _loadSettings();
      this.repoOwner = s.owner || (detected?.owner ?? "");
      this.repoName = s.repo || (detected?.repo ?? "");
      this.dataBranch = s.branch || "data";
      const creds = _loadCreds();
      if (!creds) { this.view = "setup"; return; }
      await this._loadDB(creds);
    },

    async _loadDB(creds) {
      this.view = "loading"; this.error = null;
      try {
        this.loadingMsg = "Connecting to GitHub API...";
        var manifest = await ICS.github.fetchShardManifest(
          this.repoOwner, this.repoName, this.dataBranch, creds.token,
        );
        this.commitSha = manifest.commitSha;

        if (manifest.format === "sharded") {
          this.loadingMsg = "Deriving decryption key...";
          var pw = await ICS.crypto.buildPasswordV2(creds);

          this.loadingMsg = "Loading index...";
          var self = this;
          await _loadFromShardManifest(
            manifest, this.repoOwner, this.repoName, pw, creds.token,
            function (i, n, name) {
              if (i === 0) {
                self.loadingMsg = "All " + n + " shards unchanged — instant load.";
              } else {
                self.loadingMsg = "Downloading shard " + i + "/" + n
                  + " (" + name + ")...";
              }
            },
          );
        } else {
          this.loadingMsg = "Downloading + decrypting legacy database...";
          await _loadFromLegacyBlob(
            manifest, this.repoOwner, this.repoName, creds, creds.token,
          );
        }

        var sorted = this._sortCoursesByStar(ICS.db.getCourses());
        this.courses = sorted;
        this.view = "courses";
        var self = this;
        this.$nextTick(function () { self.courses = self._sortCoursesByStar(self.courses); });
      } catch (e) {
        this.error = e.message;
        this.view = "error";
      }
    },

    navigate(view, params) {
      params = params || {};
      this._history.push({ view: this.view, courseId: this.currentCourse?.course_id, lectureId: this.currentLecture?.sub_id });
      this._go(view, params);
    },
    _go(view, params) {
      params = params || {};
      this.error = null;
      if (view === "courses") {
        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
      }
      else if (view === "lectures" && params.courseId) {
        this.currentCourse = this.courses.find(x => x.course_id === params.courseId) || { course_id: params.courseId, title: "...", teacher: "" };
        this.lectures = ICS.db.getLectures(params.courseId);
      }
      else if (view === "detail" && params.subId) {
        this.currentLecture = ICS.db.getLecture(params.subId);
        this.currentPptPages = this.currentLecture
          ? ICS.db.getPptPages(this.currentLecture.sub_id)
          : [];
        this.detailView = "summary";
      }
      this.view = view;
      if (view !== "lectures") this.exportDialogOpen = false;
    },
    _sortCoursesByStar(list) {
      // Stable two-key sort: starred first (descending = pinned), then
      // by the existing last_updated DESC the SQL already produced.
      var starred = this.starred;
      return list.slice().sort(function (a, b) {
        var sa = starred.has(String(a.course_id)) ? 0 : 1;
        var sb = starred.has(String(b.course_id)) ? 0 : 1;
        if (sa !== sb) return sa - sb;
        return 0;  // preserve SQL order within each group
      });
    },
    goBack() {
      const p = this._history.pop();
      if (p) this._go(p.view, { courseId: p.courseId, subId: p.lectureId });
      else this._go("courses");
    },

    openCourse(id) { this.navigate("lectures", { courseId: id }); },
    openLecture(id) { this.navigate("detail", { subId: id }); },

    /* Prev/next within the current course's lecture list.  Lectures are
       ordered ascending by sub_id (matches the lectures view), so "prev"
       is the lecture at index-1 and "next" is at index+1. */
    _currentLectureIndex() {
      if (!this.currentLecture || !this.lectures) return -1;
      return this.lectures.findIndex(
        (l) => String(l.sub_id) === String(this.currentLecture.sub_id)
      );
    },
    prevLecture() {
      var i = this._currentLectureIndex();
      return i > 0 ? this.lectures[i - 1] : null;
    },
    nextLecture() {
      var i = this._currentLectureIndex();
      return (i >= 0 && i + 1 < this.lectures.length)
        ? this.lectures[i + 1] : null;
    },
    gotoPrevLecture() {
      var lec = this.prevLecture();
      if (lec) { this._go("detail", { subId: lec.sub_id }); this._scrollToTop(); }
    },
    gotoNextLecture() {
      var lec = this.nextLecture();
      if (lec) { this._go("detail", { subId: lec.sub_id }); this._scrollToTop(); }
    },
    _scrollToTop() {
      var self = this;
      this.$nextTick(function () {
        window.scrollTo(0, 0);
        var el = document.querySelector("main");
        if (el) el.scrollTop = 0;
      });
    },

    /* Star/pin a course.  Per-browser localStorage state; no roundtrip
       to GitHub.  Re-sorts the courses list immediately so the user
       sees the pin take effect without navigating away. */
    isStarred(courseId) {
      return this.starred.has(String(courseId));
    },
    toggleStar(courseId) {
      var cid = String(courseId);
      if (this.starred.has(cid)) this.starred.delete(cid);
      else this.starred.add(cid);
      _saveStarred(this.starred);
      // Reactive refresh — re-sort in place.
      this.courses = this._sortCoursesByStar(this.courses);
    },

    /* Three-state detail viewer.  The button shown to the user always
       advertises the *next* state so the label reads as an action. */
    cycleDetailView() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      if (idx === -1) idx = 0;
      this.detailView = _DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length];
    },
    nextDetailViewLabel() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      if (idx === -1) idx = 0;
      var next = _DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length];
      return "切换到" + _DETAIL_VIEW_LABEL[next];
    },
    formatPptTimestamp(sec) { return _formatTimestamp(sec); },

    getExportableLectures() {
      return (this.lectures || []).filter((lec) => lec.summary && lec.summary.trim());
    },
    openExportDialog() {
      const list = this.getExportableLectures();
      if (!list.length) { this._toast("No summarized lectures to export", "error"); return; }
      this.exportSelection = {};
      list.forEach((lec) => { this.exportSelection[lec.sub_id] = true; });
      this.exportDialogOpen = true;
    },
    closeExportDialog() {
      if (this.exportingPdf) return;
      this.exportDialogOpen = false;
    },
    isLectureSelected(subId) { return !!this.exportSelection[subId]; },
    toggleLectureSelection(subId, checked) { this.exportSelection[subId] = !!checked; },
    setExportAll(checked) {
      this.getExportableLectures().forEach((lec) => { this.exportSelection[lec.sub_id] = !!checked; });
    },
    isExportAllSelected() {
      const list = this.getExportableLectures();
      return list.length > 0 && list.every((lec) => this.exportSelection[lec.sub_id]);
    },
    selectedExportCount() {
      return this.getExportableLectures().filter((lec) => this.exportSelection[lec.sub_id]).length;
    },
    _getSelectedExportLectures() {
      return this.getExportableLectures().filter(
        function (lec) { return this.exportSelection[lec.sub_id]; }, this
      );
    },
    _buildSelectedMarkdown(selected) {
      var lines = [
        "# " + (this.currentCourse?.title || "课程摘要"),
        "",
      ];
      for (var i = 0; i < selected.length; i++) {
        var lec = selected[i];
        lines.push("## " + (lec.sub_title || "Untitled") + "（" + (lec.date || "") + "）");
        lines.push("");
        if (lec.summary) {
          lines.push("### 摘要");
          lines.push("");
          lines.push(lec.summary);
          lines.push("");
        }
        if (lec.transcript) {
          lines.push("### 转录文本");
          lines.push("");
          lines.push(lec.transcript);
          lines.push("");
        }
        lines.push("---");
        lines.push("");
      }
      return lines.join("\n");
    },
    _makeMarkdownFilename() {
      var title = String(this.currentCourse?.title || "课程摘要")
        .replace(/[\\/:*?"<>|]/g, "_")
        .replace(/\s+/g, "_")
        .replace(/^_+|_+$/g, "");
      if (!title) title = "课程摘要";
      return title + ".md";
    },
    async exportSelectedToPdf() {
      // Triggers .github/workflows/export.yml via workflow_dispatch.  The
      // workflow runs scripts/export_course.py (WeasyPrint) and emails the
      // PDF to RECEIVER_EMAIL — same output and same code path as a manual
      // run from the Actions UI.  We dropped the in-browser html2pdf.js
      // approach because the screenshot-based pipeline produced blank PDFs
      // unreliably; routing through Actions reuses the working tech stack.
      if (this.exportingPdf) return;
      const selected = this.getExportableLectures().filter(
        (lec) => this.exportSelection[lec.sub_id]
      );
      if (!selected.length) {
        this._toast("Please select at least one lecture", "error");
        return;
      }
      const creds = _loadCreds();
      if (!creds?.token) {
        this._toast("Not authenticated", "error");
        return;
      }
      this.exportingPdf = true;
      try {
        const subIds = selected.map((lec) => String(lec.sub_id)).join(",");
        await ICS.github.triggerExportWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          this.currentCourse.course_id, "PDF", subIds
        );
        this.exportDialogOpen = false;
        this._toast(
          "已触发后台导出，PDF 将在 1-3 分钟内发送到 RECEIVER_EMAIL",
          "success"
        );
      } catch (e) {
        this._toast(e?.message || "Export failed", "error");
      } finally {
        this.exportingPdf = false;
      }
    },

    async exportSelectedToClipboard() {
      // Client-side markdown export: concatenate transcript + summary
      // for selected lectures and copy to clipboard.  No server needed.
      var selected = this._getSelectedExportLectures();
      if (!selected.length) {
        this._toast("请至少选择一节课程", "error");
        return;
      }
      var markdown = this._buildSelectedMarkdown(selected);
      try {
        await navigator.clipboard.writeText(markdown);
        this._toast(
          "已复制 " + selected.length + " 节课的 Markdown 到剪贴板",
          "success"
        );
      } catch (e) {
        this._toast("复制失败：" + (e?.message || "unknown"), "error");
      }
    },
    exportSelectedToMarkdownFile() {
      var selected = this._getSelectedExportLectures();
      if (!selected.length) {
        this._toast("请至少选择一节课程", "error");
        return;
      }
      var markdown = this._buildSelectedMarkdown(selected);
      var blob = new Blob([markdown], { type: "text/plain;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = this._makeMarkdownFilename();
      a.click();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      this._toast("已下载 " + selected.length + " 节课的 Markdown", "success");
    },

    _searchTimeout: null,
    get searchCourseLabel() {
      if (!this.searchSelectedCourseIds.length) return "全部课程";
      return "已选" + this.searchSelectedCourseIds.length + "门";
    },
    get searchCourseOptionsFiltered() {
      var q = (this.searchCourseFilterQuery || "").trim().toLowerCase();
      var list = this.courses || [];
      if (!q) return list;
      return list.filter(function (c) {
        var title = String(c?.title || "").toLowerCase();
        var teacher = String(c?.teacher || "").toLowerCase();
        var cid = String(c?.course_id || "").toLowerCase();
        return title.indexOf(q) !== -1 || teacher.indexOf(q) !== -1 || cid.indexOf(q) !== -1;
      });
    },
    toggleSearchCourse(courseId, checked) {
      var cid = String(courseId);
      var s = new Set(this.searchSelectedCourseIds.map(String));
      if (checked) s.add(cid); else s.delete(cid);
      this.searchSelectedCourseIds = Array.from(s);
      this.doSearch();
    },
    doSearch() {
      clearTimeout(this._searchTimeout);
      this._searchTimeout = setTimeout(() => {
        this._loadSearchResults(1);
      }, 300);
    },
    _loadSearchResults(page) {
      if (!this.searchQuery.trim()) {
        this.searchResults = [];
        this.searchHasMore = false;
        this.searchPage = 1;
        return;
      }
      var result = ICS.db.searchSummaries(
        this.searchQuery, this.searchSelectedCourseIds,
        page, 50, this.searchDomains,
      );
      if (page <= 1) {
        this.searchResults = result.results;
      } else {
        [].push.apply(this.searchResults, result.results);
      }
      this.searchPage = result.page;
      this.searchHasMore = result.hasMore;
    },
    loadMore() {
      this._loadSearchResults(this.searchPage + 1);
    },
    toggleSearchDomain(domain) {
      this.searchDomains = Object.assign({}, this.searchDomains);
      this.searchDomains[domain] = !this.searchDomains[domain];
      this.doSearch();
    },

    async refresh() {
      const c = _loadCreds();
      if (c) { await this._loadDB(c); this._toast("Refreshed", "success"); }
    },

    async testAndSave() {
      this.setupTesting = true; this.setupError = "";
      try {
        var manifest = await ICS.github.fetchShardManifest(
          this.repoOwner, this.repoName, this.dataBranch, this.setup.token,
        );
        if (manifest.format === "sharded") {
          // Probe the index decryption to validate creds before we save.
          var pw = await ICS.crypto.buildPasswordV2(this.setup);
          var indexEnc = await ICS.github.fetchBlobBytes(
            this.repoOwner, this.repoName, manifest.index.sha, this.setup.token,
          );
          var indexPt = await ICS.crypto.decrypt(
            indexEnc, pw, ICS.crypto.NEW_ITERATIONS,
          );
          if (!ICS.crypto.isJsonObj(indexPt)) {
            throw new Error("凭据验证失败：索引解密结果不像 JSON。");
          }
        } else {
          var encBytes = await ICS.github.fetchBlobBytes(
            this.repoOwner, this.repoName, manifest.legacy.sha, this.setup.token,
          );
          var legacyValidator = manifest.legacy.compressed
            ? ICS.crypto.isGzip
            : ICS.crypto.isSqlite;
          await ICS.crypto.decryptWithFallback(
            encBytes, this.setup, legacyValidator,
          );
        }
        _saveCreds({ ...this.setup });
        _saveSettings({ owner: this.repoOwner, repo: this.repoName, branch: this.dataBranch });
        this.commitSha = manifest.commitSha;
        await this._loadDB({ ...this.setup });
      } catch (e) { this.setupError = e.message; }
      finally { this.setupTesting = false; }
    },

    openSettings() {
      this.settingsForm = { ...(_loadCreds() || {}) };
      this.showSecrets = {};
      this.navigate("settings");
    },
    async saveSettingsAndReload() {
      _saveCreds({ ...this.settingsForm });
      _saveSettings({ owner: this.repoOwner, repo: this.repoName, branch: this.dataBranch });
      this._toast("Saved. Reloading...", "success");
      const c = _loadCreds();
      if (c) await this._loadDB(c);
    },
    clearAllData() {
      if (!confirm("Clear all saved credentials?")) return;
      localStorage.removeItem(_LS + "creds");
      localStorage.removeItem(_LS + "settings");
      // Close the cached connection before deleting — Chrome/Firefox block
      // deleteDatabase indefinitely while any handle is still open.
      if (_idbPromise) {
        _idbPromise.then(function(db) { try { db.close(); } catch (e) {} });
        _idbPromise = null;
      }
      indexedDB.deleteDatabase(_idbName);
      this.view = "setup";
      this.setup = { token: "", stuid: "", uispsw: "" };
    },

    // ── Subscriptions editor (three-column) ──────────────────────────
    openSubscriptions() {
      try {
        this._go("subscriptions");
      } catch (e) {
        console.error("_go failed:", e);
        return;
      }

      this.allCoursesTerms = ICS.db.getAllCoursesTerms();
      this.subsTerms = [];
      this.subsDepts = [];
      this.deptSearchQuery = "";
      this.subsSearchTitle = "";
      this.subsSearchTeacher = "";
      this.subsTermOpen = false;
      this.subsDeptOpen = false;
      this.subsFiltered = [];
      this.subsFilteredTotal = 0;
      this.singleRunIds = [];
      this.subsSelLeft = [];
      this.subsSelMiddle = [];
      this.subsSelRight = [];
      this._subsSubscribedCache = [];
      this._subsSingleRunCache = [];
      this._subsDeptCache = [];
      this.subsError = "";

      // Load subscription (localStorage → meta table fallback)
      this.subscribedIds = [];
      try {
        var cached = JSON.parse(
          localStorage.getItem(_LS + "lastSubscribed") || "null"
        );
        if (Array.isArray(cached)) this.subscribedIds = cached.map(String);
      } catch {}
      if (!this.subscribedIds.length) {
        try { var metaRaw = ICS.db.getMeta("course_ids"); } catch (e) { metaRaw = null; }
        if (metaRaw) {
          this.subscribedIds = metaRaw.split(",")
            .map(function (s) { return s.trim(); })
            .filter(Boolean);
          try {
            localStorage.setItem(
              _LS + "lastSubscribed",
              JSON.stringify(this.subscribedIds),
            );
          } catch {}
        }
      }
      this._refreshSubscribedCache();
      this._refreshSingleRunCache();
      this._refreshDeptCache();
      this.rebuildSubsFiltered();
    },
    // ── Cache refreshers (called explicitly when underlying IDs change) ─
    _refreshSubscribedCache() {
      this._subsSubscribedCache = ICS.db.getCoursesByIds(this.subscribedIds);
    },
    _refreshSingleRunCache() {
      this._subsSingleRunCache = ICS.db.getCoursesByIds(this.singleRunIds);
    },
    _refreshDeptCache() {
      this._subsDeptCache = ICS.db.getAllCoursesDepts(
        this.subsTerms, this.deptSearchQuery,
      );
    },
    // ── Term badge color (cyclic palette for the search results) ─────
    _TERM_COLORS: [
      "bg-blue-100 text-blue-700",
      "bg-emerald-100 text-emerald-700",
      "bg-purple-100 text-purple-700",
      "bg-amber-100 text-amber-700",
      "bg-rose-100 text-rose-700",
      "bg-cyan-100 text-cyan-700",
      "bg-orange-100 text-orange-700",
    ],
    termBadgeClass(term) {
      var idx = 0;
      for (var i = 0; i < term.length; i++) idx = (idx * 31 + term.charCodeAt(i)) | 0;
      return this._TERM_COLORS[Math.abs(idx) % this._TERM_COLORS.length];
    },
    // ── Column data (plain arrays kept in sync by cache refreshers) ─
    get subscribedCourses() { return this._subsSubscribedCache; },
    get singleRunCourses() { return this._subsSingleRunCache; },
    // ── Dropdown labels ─────────────────────────────────────────────
    get subsTermLabel() {
      if (!this.subsTerms.length) return '全部学期';
      return this.subsTerms.length + '个学期';
    },
    get subsDeptLabel() {
      if (!this.subsDepts.length) return '全部院系';
      return this.subsDepts.length + '个院系';
    },
    get subsDeptFiltered() { return this._subsDeptCache; },
    onDeptSearchInput() {
      // Debounced — typing in the dept search box requeries distinct depts
      // from sqlite filtered by term + substring.
      var self = this;
      clearTimeout(this._deptFilterTimer);
      this._deptFilterTimer = setTimeout(function () {
        self._refreshDeptCache();
      }, 150);
    },
    // ── Toggle multi-select ─────────────────────────────────────────
    toggleSubsTerm(term, checked) {
      var s = new Set(this.subsTerms);
      if (checked) s.add(term); else s.delete(term);
      this.subsTerms = Array.from(s);
      // Term change invalidates both the dept dropdown (term-narrowed)
      // and the middle column (different rows match).
      this._refreshDeptCache();
      this.rebuildSubsFiltered();
    },
    toggleSubsDept(dept, checked) {
      var s = new Set(this.subsDepts);
      if (checked) s.add(dept); else s.delete(dept);
      this.subsDepts = Array.from(s);
      this.rebuildSubsFiltered();
    },
    // ── Filter middle column (debounced SQL query) ───────────────────
    rebuildSubsFiltered() {
      var self = this;
      clearTimeout(this._subsFilterTimer);
      this._subsFilterTimer = setTimeout(function () {
        var filters = {
          terms: self.subsTerms,
          depts: self.subsDepts,
          title: self.subsSearchTitle,
          teacher: self.subsSearchTeacher,
        };
        self.subsFiltered = ICS.db.searchAllCourses(filters, self.subsLimit);
        self.subsFilteredTotal = ICS.db.countAllCourses(filters);
      }, 150);
    },
    // ── Per-column multi-select ──────────────────────────────────────
    _toggleSel(arr, id, checked) {
      var cid = String(id);
      var s = new Set(arr.map(String));
      if (checked) s.add(cid); else s.delete(cid);
      return Array.from(s);
    },
    toggleSelLeft(id, checked) {
      this.subsSelLeft = this._toggleSel(this.subsSelLeft, id, checked);
    },
    toggleSelMiddle(id, checked) {
      this.subsSelMiddle = this._toggleSel(this.subsSelMiddle, id, checked);
    },
    toggleSelRight(id, checked) {
      this.subsSelRight = this._toggleSel(this.subsSelRight, id, checked);
    },
    // ── Triangle-arrow operations ────────────────────────────────────
    moveToSubscribed() {
      var target = new Set(this.subscribedIds.map(String));
      var selected = this.subsSelMiddle;
      for (var i = 0; i < selected.length; i++) target.add(selected[i]);
      this.subscribedIds = Array.from(target);
      this.subsSelMiddle = [];
      this._refreshSubscribedCache();
    },
    moveFromSubscribed() {
      var target = new Set(this.subscribedIds.map(String));
      var selected = this.subsSelLeft;
      for (var i = 0; i < selected.length; i++) target.delete(selected[i]);
      this.subscribedIds = Array.from(target);
      this.subsSelLeft = [];
      this._refreshSubscribedCache();
    },
    moveToSingleRun() {
      var target = new Set(this.singleRunIds.map(String));
      var selected = this.subsSelMiddle;
      for (var i = 0; i < selected.length; i++) target.add(selected[i]);
      this.singleRunIds = Array.from(target);
      this.subsSelMiddle = [];
      this._refreshSingleRunCache();
    },
    moveFromSingleRun() {
      var target = new Set(this.singleRunIds.map(String));
      var selected = this.subsSelRight;
      for (var i = 0; i < selected.length; i++) target.delete(selected[i]);
      this.singleRunIds = Array.from(target);
      this.subsSelRight = [];
      this._refreshSingleRunCache();
    },
    // ── Save left column to GitHub Secret ────────────────────────────
    async saveSubscriptions() {
      if (this.subsSaving) return;
      var creds = _loadCreds();
      if (!creds?.token) {
        this.subsError = "未登录或 PAT 缺失。";
        return;
      }
      if (!this.repoOwner || !this.repoName) {
        this.subsError = "Repo owner/name 未设置，请到 Settings 配置。";
        return;
      }
      this.subsSaving = true;
      this.subsError = "";
      try {
        var written = await ICS.github.setCourseIdsSecret(
          this.repoOwner, this.repoName, creds.token, this.subscribedIds,
        );
        this._toast(
          "已保存 " + written.split(",").filter(Boolean).length + " 门课到 COURSE_IDS secret",
          "success",
        );
        try {
          localStorage.setItem(
            _LS + "lastSubscribed",
            JSON.stringify(this.subscribedIds),
          );
        } catch {}
      } catch (e) {
        this.subsError = e?.message || "保存失败";
      } finally {
        this.subsSaving = false;
      }
    },
    // ── Single-run (right column) ────────────────────────────────────
    async runSingleRunWorkflow() {
      if (this.singleRunTriggering) return;
      if (!this.singleRunIds.length) {
        this._toast("单次运行列表为空", "error");
        return;
      }
      var creds = _loadCreds();
      if (!creds?.token) {
        this.subsError = "未登录或 PAT 缺失。";
        return;
      }
      this.singleRunTriggering = true;
      this.subsError = "";
      try {
        // Fire single_run.yml directly with course_ids as input. This
        // keeps the persisted COURSE_IDS secret (used by daily check)
        // untouched, and uses the dedicated single-run workflow rather
        // than overloading the scheduled check workflow.
        await ICS.github.triggerSingleRunWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          this.singleRunIds,
        );
        this._toast(
          "已触发单次运行，处理 " + this.singleRunIds.length + " 门课。请到 Actions 查看进度",
          "success",
        );
        // Clear the basket
        this.singleRunIds = [];
        this.subsSelRight = [];
        this._refreshSingleRunCache();
      } catch (e) {
        this.subsError = e?.message || "触发失败";
      } finally {
        this.singleRunTriggering = false;
      }
    },

    _toast(msg, type) {
      this.toast = msg; this.toastType = type || "success";
      setTimeout(() => { this.toast = null; }, 3000);
    },

    // Template helpers
    renderMd(s) { return ICS.render.renderMarkdown(s); },
    activateKaTeX(el) { ICS.render.activateKaTeX(el); },
    snippet(s, n) { return ICS.render.plainSnippet(s, n); },
    highlight(text, q) { return _highlightSnippet(text, q); },
    relTime(s) { return _relativeTime(s); },
  }));
});
