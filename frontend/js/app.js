/**
 * Alpine.js app — all state, routing, and view logic for the iCourse frontend.
 * References ICS.crypto, ICS.github, ICS.db, ICS.render globals.
 *
 * V3: Data loaded from co-located encrypted JSON shards (built at deploy time).
 * No more GitHub API tree walk for data; only GitHub API for Secrets + workflow triggers.
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

/* ── Credential helpers (localStorage) ── */
const _LS = "ics_";
const _loadCreds = () => { try { return JSON.parse(localStorage.getItem(_LS + "creds")); } catch { return null; } };
const _saveCreds = (c) => localStorage.setItem(_LS + "creds", JSON.stringify(c));
const _loadSettings = () => { try { return JSON.parse(localStorage.getItem(_LS + "settings")) || {}; } catch { return {}; } };
const _saveSettings = (s) => localStorage.setItem(_LS + "settings", JSON.stringify(s));
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

const _DETAIL_VIEW_CYCLE = ["summary", "transcript", "ppt"];
const _DETAIL_VIEW_LABEL = {
  summary: "摘要",
  transcript: "转录",
  ppt: "PPT 识别",
};

/* ── Data path (relative to index.html) ── */
var _DATA_BASE = "data/";

/* ── V3 master key (held in memory after login) ── */
var _masterKey = null;

async function _v3Fetch(path, signal) {
  var res = await fetch(_DATA_BASE + path, signal ? { signal: signal } : undefined);
  if (!res.ok) throw new Error("Failed to fetch " + path + ": " + res.status);
  return new Uint8Array(await res.arrayBuffer());
}

async function _v3Decrypt(path, signal) {
  var enc = await _v3Fetch(path, signal);
  var compressed = await ICS.crypto.hkdfDecrypt(enc, _masterKey);
  var decompressed = await _gunzip(compressed);
  return JSON.parse(new TextDecoder().decode(decompressed));
}

/* ── Scheduler-routed lazy loaders ──
 * All on-demand shard loads funnel through ICS.scheduler so they share one
 * bounded, focus-aware pipe. focus(courseId) aborts other-course requests in
 * flight and dedicates the pipe to the opened course; loads dedupe by key. */
var _PRIO = { DETAIL: 100, PPT: 90, FOCUS_PRELOAD: 10, BG_PRELOAD: 1 };

/* Scheduler group for the catalog shard — a non-course group so focusing it
 * aborts in-flight course loads and dedicates the pipe to the catalog. */
var _CATALOG_GROUP = "__catalog__";

function _loadCatalogShard(priority) {
  return ICS.scheduler.enqueue({
    key: "catalog",
    group: _CATALOG_GROUP,
    priority: priority,
    run: function (signal) { return _v3Decrypt("catalog.enc", signal); },
  });
}

function _loadLecture(subId, courseId, priority) {
  return ICS.db.loadLectureContent(subId, function (id) {
    return ICS.scheduler.enqueue({
      key: "lecture:" + id,
      group: courseId,
      priority: priority,
      run: function (signal) { return _v3Decrypt("lectures/" + id + ".enc", signal); },
    });
  });
}

function _loadPpt(courseId, priority) {
  return ICS.db.loadPptPages(courseId, function (cid) {
    return ICS.scheduler.enqueue({
      key: "ppt:" + cid,
      group: courseId,
      priority: priority,
      run: function (signal) { return _v3Decrypt("ppt/" + cid + ".enc", signal); },
    });
  });
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
    commitSha: null,
    setup: { token: "", stuid: "", uispsw: "" },
    setupError: "", setupTesting: false,
    settingsForm: {}, showSecrets: {},
    exportDialogOpen: false, exportSelection: {}, exportingPdf: false,
    repoOwner: "", repoName: "",
    _navToken: 0,
    _applyingHash: false,
    _selfHashWrite: false,
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
    starred: _loadStarred(),
    _catalogLoaded: false,
    catalogLoading: false,
    loadProgress: { coursesPending: 0, lecturesPending: 0 },
    _loadProgressTimer: null,

    async init() {
      const detected = ICS.github.detectRepo();
      const s = _loadSettings();
      this.repoOwner = s.owner || (detected?.owner ?? "");
      this.repoName = s.repo || (detected?.repo ?? "");
      // Browser back/forward and manual hash edits re-navigate. Skip the
      // events caused by our own programmatic hash writes (in _go).
      window.addEventListener("hashchange", () => {
        if (this._selfHashWrite) { this._selfHashWrite = false; return; }
        if (this._applyingHash) return;
        if (ICS.routing.ROUTABLE_VIEWS.indexOf(this.view) === -1 && this.view !== "loading") return;
        this._applyHash();
      });
      const creds = _loadCreds();
      if (!creds) { this.view = "setup"; return; }
      await this._loadDB(creds);
    },

    async _loadDB(creds) {
      this.view = "loading"; this.error = null;
      ICS.scheduler.reset();
      try {
        // 1) Fetch meta.json (plaintext)
        this.loadingMsg = "Loading metadata...";
        var metaRes = await fetch(_DATA_BASE + "meta.json");
        if (!metaRes.ok) throw new Error("Failed to load meta.json: " + metaRes.status);
        var meta = await metaRes.json();

        // 2) Derive master key (one-time PBKDF2)
        this.loadingMsg = "Deriving decryption key...";
        var pw = await ICS.crypto.buildPasswordV3(creds);
        _masterKey = await ICS.crypto.deriveV3MasterKey(
          pw, meta.pbkdf2_salt, meta.iterations
        );

        // 3) Decrypt index
        this.loadingMsg = "Decrypting index...";
        var indexData = await _v3Decrypt("index.enc");
        ICS.db.initFromIndex(indexData);

        // 4) Show courses (or restore the position encoded in the URL hash)
        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
        var self = this;
        var route = ICS.routing.parseHash(location.hash);
        if (route.view && route.view !== "courses") {
          this._applyHash();
        } else {
          this.view = "courses";
        }
        this.$nextTick(function() { self.courses = self._sortCoursesByStar(self.courses); });

        // 5) Background preload (non-blocking)
        this._preloadInBackground();
        this._trackLoadProgress();
      } catch (e) {
        this.error = e.message;
        this.view = "error";
      }
    },

    navigate(view, params) {
      // Browser history is the single source of truth now: _go writes the
      // hash, which pushes a history entry. No separate in-app stack.
      this._go(view, params || {});
    },
    async _go(view, params) {
      params = params || {};
      this.error = null;
      var navToken = ++this._navToken;
      if (view === "courses") {
        ICS.scheduler.blur();
        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
      }
      else if (view === "lectures" && params.courseId) {
        ICS.scheduler.focus(params.courseId);
        this._focusPreloadCourse(params.courseId);
        this.currentCourse = this.courses.find(x => x.course_id === params.courseId) || { course_id: params.courseId, title: "...", teacher: "" };
        this.lectures = ICS.db.getLectures(params.courseId);
      }
      else if (view === "detail" && params.subId) {
        // Ensure the course context (currentCourse + lectures) matches this
        // lecture, so prev/next nav works even when arriving via deep link or
        // a refresh straight into detail.
        var detSkel = ICS.db.getLecture(params.subId);
        if (detSkel) {
          var detCid = detSkel.course_id;
          if (!this.currentCourse || this.currentCourse.course_id !== detCid) {
            this.currentCourse = this.courses.find(x => x.course_id === detCid)
              || { course_id: detCid, title: "...", teacher: "" };
            this.lectures = ICS.db.getLectures(detCid);
          }
        }
        // Lazy-load lecture content if not yet loaded
        if (!ICS.db.isLectureLoaded(params.subId)) {
          var skel = detSkel;
          if (skel) ICS.scheduler.focus(skel.course_id);
          try {
            await _loadLecture(params.subId, skel ? skel.course_id : null, _PRIO.DETAIL);
          } catch (e) {
            console.warn("Failed to load lecture content:", e);
          }
        }
        // Bail if the user navigated elsewhere while we were loading.
        if (navToken !== this._navToken) return;
        this.currentLecture = ICS.db.getLecture(params.subId);
        // Load PPT pages for the course (lazy)
        if (this.currentLecture) {
          var courseId = this.currentLecture.course_id;
          try {
            await _loadPpt(courseId, _PRIO.PPT);
          } catch (e) {
            // PPT might not exist for all courses
          }
          if (navToken !== this._navToken) return;
          this.currentPptPages = ICS.db.getPptPages(params.subId);
        } else {
          this.currentPptPages = [];
        }
        this.detailView = "summary";
      }
      else if (view === "subscriptions") {
        // Dedicate the pipe to the catalog: focusing this non-course group
        // aborts any in-flight course loads so the catalog fetches first.
        ICS.scheduler.focus(_CATALOG_GROUP);
      }
      else if (view === "search") {
        // The search filter doubles as a focus selector: when courses are
        // picked, dedicate the pipe to them (abort other-course fetches) so
        // their summaries/transcripts become searchable first. No selection
        // ("全部课程") means search everything, so keep preloading all.
        this._applySearchFocus();
      }
      else {
        // settings — release the focused course
        ICS.scheduler.blur();
      }
      this.view = view;
      if (view !== "lectures") this.exportDialogOpen = false;
      // Reflect the new position in the URL hash (browser history entry),
      // unless this navigation was itself triggered by a hash change.
      if (!this._applyingHash) {
        var courseId = (view === "lectures") ? params.courseId
          : (this.currentCourse ? this.currentCourse.course_id : null);
        var subId = (view === "detail") ? params.subId : null;
        var h = ICS.routing.hashFor(view, courseId, subId);
        if (h && location.hash !== h) { this._selfHashWrite = true; location.hash = h; }
      }
    },
    _sortCoursesByStar(list) {
      var starred = this.starred;
      return list.slice().sort(function (a, b) {
        var sa = starred.has(String(a.course_id)) ? 0 : 1;
        var sb = starred.has(String(b.course_id)) ? 0 : 1;
        if (sa !== sb) return sa - sb;
        return 0;
      });
    },
    goBack() {
      // Defer to the browser. If there's no prior entry (e.g. opened via a
      // deep link), fall back to the course list.
      if (window.history.length > 1) window.history.back();
      else this.navigate("courses");
    },
    // Apply the current URL hash as a navigation (driven by hashchange and
    // by initial load). Sets _applyingHash so _go doesn't re-write the hash.
    _applyHash() {
      var route = ICS.routing.parseHash(location.hash);
      this._applyingHash = true;
      try {
        if (route.view === "subscriptions") {
          // Subscriptions needs its catalog-loading side effects.
          this.openSubscriptions();
        } else if (route.view === "lectures") {
          // Unknown course (data changed) → fall back to the list.
          var known = ICS.db.getCourses().some((c) => String(c.course_id) === String(route.courseId));
          if (known) { this._go("lectures", { courseId: route.courseId }); }
          else { this._go("courses"); this._selfHashWrite = true; location.hash = "#/courses"; }
        } else if (route.view === "detail") {
          var skel = ICS.db.getLecture(route.subId);
          if (!skel) {
            this._go("courses"); this._selfHashWrite = true; location.hash = "#/courses";
          } else {
            var cid = route.courseId || skel.course_id;
            this._go("detail", { subId: route.subId, courseId: cid });
            if (!route.courseId) {
              var newHash = ICS.routing.hashFor("detail", cid, route.subId);
              if (newHash && location.hash !== newHash) {
                this._selfHashWrite = true;
                history.replaceState(null, "", newHash);
              }
            }
          }
        } else {
          this._go(route.view || "courses", {});
        }
      } finally {
        this._applyingHash = false;
      }
    },

    openCourse(id) { this.navigate("lectures", { courseId: id }); },
    openLecture(id) { this.navigate("detail", { subId: id }); },

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
      return (i >= 0 && i < this.lectures.length - 1) ? this.lectures[i + 1] : null;
    },
    goPrevLecture() { var l = this.prevLecture(); if (l) this._go("detail", { subId: l.sub_id }); },
    goNextLecture() { var l = this.nextLecture(); if (l) this._go("detail", { subId: l.sub_id }); },
    gotoPrevLecture() { this.goPrevLecture(); },
    gotoNextLecture() { this.goNextLecture(); },

    cycleDetailView() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      this.detailView = _DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length];
    },
    detailViewNext() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      return _DETAIL_VIEW_LABEL[_DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length]];
    },

    toggleStar(courseId) {
      var cid = String(courseId);
      if (this.starred.has(cid)) this.starred.delete(cid);
      else this.starred.add(cid);
      _saveStarred(this.starred);
      this.courses = this._sortCoursesByStar(this.courses);
    },
    isStarred(courseId) { return this.starred.has(String(courseId)); },

    // Export markdown to clipboard
    async exportMarkdown() {
      var selected = this.lectures.filter((l) => this.exportSelection[l.sub_id]);
      if (!selected.length) { this._toast("请先选择课次", "error"); return; }
      var lines = [];
      for (var i = 0; i < selected.length; i++) {
        var l = selected[i];
        // Load content if needed
        if (!ICS.db.isLectureLoaded(l.sub_id)) {
          try {
            await _loadLecture(l.sub_id, l.course_id || (this.currentCourse && this.currentCourse.course_id), _PRIO.DETAIL);
          } catch (e) { continue; }
        }
        var full = ICS.db.getLecture(l.sub_id);
        lines.push("## " + (l.sub_title || l.sub_id));
        if (full && full.summary) {
          lines.push("");
          lines.push(full.summary);
          lines.push("");
        }
        lines.push("---");
        lines.push("");
      }
      try {
        await navigator.clipboard.writeText(lines.join("\n"));
        this._toast("已复制 " + selected.length + " 节课的 Markdown 到剪贴板", "success");
      } catch (e) {
        this._toast("复制失败：" + (e?.message || "unknown"), "error");
      }
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
        var title = String((c && c.title) || "").toLowerCase();
        var teacher = String((c && c.teacher) || "").toLowerCase();
        var cid = String((c && c.course_id) || "").toLowerCase();
        return title.indexOf(q) !== -1 || teacher.indexOf(q) !== -1 || cid.indexOf(q) !== -1;
      });
    },
    toggleSearchCourse(courseId, checked) {
      var cid = String(courseId);
      var s = new Set(this.searchSelectedCourseIds.map(String));
      if (checked) s.add(cid); else s.delete(cid);
      this.searchSelectedCourseIds = Array.from(s);
      // Treat the search filter as focus: dedicate the download pipe to the
      // selected courses (or release it when the selection is cleared) so
      // their shards load ahead of the background sweep.
      this._applySearchFocus();
      this.doSearch();
    },
    // Drive the scheduler from the search course filter. Selected courses →
    // focus that set (abort other-course fetches); empty selection → blur so
    // the global preload of every course resumes. Only meaningful while the
    // search view is active; navigating away re-focuses via _go.
    _applySearchFocus() {
      if (this.searchSelectedCourseIds.length) {
        ICS.scheduler.focus(this.searchSelectedCourseIds.map(String));
      } else {
        ICS.scheduler.blur();
      }
    },
    async doSearch() {
      clearTimeout(this._searchTimeout);
      var self = this;
      this._searchTimeout = setTimeout(function() {
        if (!self.searchQuery.trim()) { self.searchResults = []; return; }
        self.searchResults = ICS.db.searchSummaries(self.searchQuery, self.searchSelectedCourseIds);
      }, 300);
    },

    async refresh() {
      const c = _loadCreds();
      if (c) { await this._loadDB(c); this._toast("Refreshed", "success"); }
    },

    async testAndSave() {
      this.setupTesting = true; this.setupError = "";
      try {
        if (!this.setup.stuid || !this.setup.uispsw) {
          throw new Error("请输入学号和密码");
        }
        // Validate credentials by trying to decrypt index.enc
        var metaRes = await fetch(_DATA_BASE + "meta.json");
        if (!metaRes.ok) throw new Error("无法加载 meta.json");
        var meta = await metaRes.json();
        var pw = await ICS.crypto.buildPasswordV3(this.setup);
        var mk = await ICS.crypto.deriveV3MasterKey(pw, meta.pbkdf2_salt, meta.iterations);
        var indexEnc = await _v3Fetch("index.enc");
        await ICS.crypto.hkdfDecrypt(indexEnc, mk);
        // If decryption succeeds without throwing, creds are valid
        _saveCreds({ ...this.setup });
        _saveSettings({ owner: this.repoOwner, repo: this.repoName });
        await this._loadDB({ ...this.setup });
      } catch (e) {
        this.setupError = "凭据验证失败: " + (e.message || "解密失败");
      } finally {
        this.setupTesting = false;
      }
    },

    openSettings() {
      this.settingsForm = { ...(_loadCreds() || {}) };
      this.showSecrets = {};
      this.navigate("settings");
    },
    async saveSettingsAndReload() {
      _saveCreds({ ...this.settingsForm });
      _saveSettings({ owner: this.repoOwner, repo: this.repoName });
      this._toast("Saved. Reloading...", "success");
      const c = _loadCreds();
      if (c) await this._loadDB(c);
    },
    clearAllData() {
      if (!confirm("Clear all saved credentials?")) return;
      ICS.scheduler.reset();
      localStorage.removeItem(_LS + "creds");
      localStorage.removeItem(_LS + "settings");
      _masterKey = null;
      this.view = "setup";
      this.setup = { token: "", stuid: "", uispsw: "" };
    },

    // ── Subscriptions editor ──────────────────────────────────────────
    async openSubscriptions() {
      try {
        this._go("subscriptions");
      } catch (e) {
        console.error("_go failed:", e);
        return;
      }

      // Load catalog on demand (focused above so it preempts course loads)
      if (!this._catalogLoaded) {
        this.catalogLoading = true;
        try {
          var catalogData = await _loadCatalogShard(_PRIO.DETAIL);
          ICS.db.loadCatalog(catalogData);
          this._catalogLoaded = true;
        } catch (e) {
          console.warn("Failed to load catalog:", e);
        } finally {
          this.catalogLoading = false;
        }
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

      this.subscribedIds = [];
      try {
        var cached = JSON.parse(localStorage.getItem(_LS + "lastSubscribed") || "null");
        if (Array.isArray(cached)) this.subscribedIds = cached.map(String);
      } catch {}
      if (!this.subscribedIds.length) {
        var metaRaw = ICS.db.getMeta("course_ids");
        if (metaRaw) {
          this.subscribedIds = metaRaw.split(",").map(function(s) { return s.trim(); }).filter(Boolean);
          try { localStorage.setItem(_LS + "lastSubscribed", JSON.stringify(this.subscribedIds)); } catch {}
        }
      }
      this._refreshSubscribedCache();
      this._refreshSingleRunCache();
      this._refreshDeptCache();
      this.rebuildSubsFiltered();
    },
    _refreshSubscribedCache() {
      this._subsSubscribedCache = ICS.db.getCoursesByIds(this.subscribedIds);
    },
    _refreshSingleRunCache() {
      this._subsSingleRunCache = ICS.db.getCoursesByIds(this.singleRunIds);
    },
    _refreshDeptCache() {
      this._subsDeptCache = ICS.db.getAllCoursesDepts(this.subsTerms, this.deptSearchQuery);
    },
    _TERM_COLORS: [
      "bg-blue-100 text-blue-700", "bg-emerald-100 text-emerald-700",
      "bg-purple-100 text-purple-700", "bg-amber-100 text-amber-700",
      "bg-rose-100 text-rose-700", "bg-cyan-100 text-cyan-700",
      "bg-orange-100 text-orange-700",
    ],
    termBadgeClass(term) {
      var idx = 0;
      for (var i = 0; i < term.length; i++) idx = (idx * 31 + term.charCodeAt(i)) | 0;
      return this._TERM_COLORS[Math.abs(idx) % this._TERM_COLORS.length];
    },
    get subscribedCourses() { return this._subsSubscribedCache; },
    get singleRunCourses() { return this._subsSingleRunCache; },
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
      var self = this;
      clearTimeout(this._deptFilterTimer);
      this._deptFilterTimer = setTimeout(function() { self._refreshDeptCache(); }, 150);
    },
    toggleSubsTerm(term, checked) {
      var s = new Set(this.subsTerms);
      if (checked) s.add(term); else s.delete(term);
      this.subsTerms = Array.from(s);
      this._refreshDeptCache();
      this.rebuildSubsFiltered();
    },
    toggleSubsDept(dept, checked) {
      var s = new Set(this.subsDepts);
      if (checked) s.add(dept); else s.delete(dept);
      this.subsDepts = Array.from(s);
      this.rebuildSubsFiltered();
    },
    rebuildSubsFiltered() {
      var self = this;
      clearTimeout(this._subsFilterTimer);
      this._subsFilterTimer = setTimeout(function() {
        var filters = {
          terms: self.subsTerms, depts: self.subsDepts,
          title: self.subsSearchTitle, teacher: self.subsSearchTeacher,
        };
        self.subsFiltered = ICS.db.searchAllCourses(filters, self.subsLimit);
        self.subsFilteredTotal = ICS.db.countAllCourses(filters);
      }, 150);
    },
    _toggleSel(arr, id, checked) {
      var cid = String(id);
      var s = new Set(arr.map(String));
      if (checked) s.add(cid); else s.delete(cid);
      return Array.from(s);
    },
    toggleSelLeft(id, checked) { this.subsSelLeft = this._toggleSel(this.subsSelLeft, id, checked); },
    toggleSelMiddle(id, checked) { this.subsSelMiddle = this._toggleSel(this.subsSelMiddle, id, checked); },
    toggleSelRight(id, checked) { this.subsSelRight = this._toggleSel(this.subsSelRight, id, checked); },
    moveToSubscribed() {
      var target = new Set(this.subscribedIds.map(String));
      for (var i = 0; i < this.subsSelMiddle.length; i++) target.add(this.subsSelMiddle[i]);
      this.subscribedIds = Array.from(target);
      this.subsSelMiddle = [];
      this._refreshSubscribedCache();
    },
    moveFromSubscribed() {
      var target = new Set(this.subscribedIds.map(String));
      for (var i = 0; i < this.subsSelLeft.length; i++) target.delete(this.subsSelLeft[i]);
      this.subscribedIds = Array.from(target);
      this.subsSelLeft = [];
      this._refreshSubscribedCache();
    },
    moveToSingleRun() {
      var target = new Set(this.singleRunIds.map(String));
      for (var i = 0; i < this.subsSelMiddle.length; i++) target.add(this.subsSelMiddle[i]);
      this.singleRunIds = Array.from(target);
      this.subsSelMiddle = [];
      this._refreshSingleRunCache();
    },
    moveFromSingleRun() {
      var target = new Set(this.singleRunIds.map(String));
      for (var i = 0; i < this.subsSelRight.length; i++) target.delete(this.subsSelRight[i]);
      this.singleRunIds = Array.from(target);
      this.subsSelRight = [];
      this._refreshSingleRunCache();
    },
    async saveSubscriptions() {
      if (this.subsSaving) return;
      var creds = _loadCreds();
      if (!creds?.token) { this.subsError = "未登录或 PAT 缺失。"; return; }
      if (!this.repoOwner || !this.repoName) { this.subsError = "Repo owner/name 未设置。"; return; }
      this.subsSaving = true; this.subsError = "";
      try {
        var written = await ICS.github.setCourseIdsSecret(
          this.repoOwner, this.repoName, creds.token, this.subscribedIds,
        );
        this._toast("已保存 " + written.split(",").filter(Boolean).length + " 门课到 COURSE_IDS secret", "success");
        try { localStorage.setItem(_LS + "lastSubscribed", JSON.stringify(this.subscribedIds)); } catch {}
      } catch (e) { this.subsError = e?.message || "保存失败"; }
      finally { this.subsSaving = false; }
    },
    async runSingleRunWorkflow() {
      if (this.singleRunTriggering) return;
      if (!this.singleRunIds.length) { this._toast("单次运行列表为空", "error"); return; }
      var creds = _loadCreds();
      if (!creds?.token) { this.subsError = "未登录或 PAT 缺失。"; return; }
      this.singleRunTriggering = true; this.subsError = "";
      try {
        await ICS.github.triggerSingleRunWorkflow(
          this.repoOwner, this.repoName, "main", creds.token, this.singleRunIds,
        );
        this._toast("已触发单次运行，处理 " + this.singleRunIds.length + " 门课。", "success");
        this.singleRunIds = []; this.subsSelRight = [];
        this._refreshSingleRunCache();
      } catch (e) { this.subsError = e?.message || "触发失败"; }
      finally { this.singleRunTriggering = false; }
    },

    // Bump every lecture of one course to focus-preload priority so the
    // opened course finishes ahead of the global background sweep.
    _focusPreloadCourse(courseId) {
      var self = this;
      var lecs = ICS.db.getLectures(courseId);
      for (var i = 0; i < lecs.length; i++) {
        var subId = lecs[i].sub_id;
        if (!ICS.db.isLectureLoaded(subId)) {
          (function (sid) {
            _loadLecture(sid, courseId, _PRIO.FOCUS_PRELOAD)
              .then(function () { self._patchLectureInList(sid); })
              .catch(function () {});
          })(subId);
        }
      }
      _loadPpt(courseId, _PRIO.FOCUS_PRELOAD).catch(function () {});
    },
    // Once a lecture's encrypted shard has loaded, patch its entry in the
    // currently-displayed list so the summary preview renders live (no need
    // to leave and re-enter the course). Matches by sub_id, so late loads
    // from a course the user already navigated away from are harmless no-ops.
    _patchLectureInList(subId) {
      if (!this.lectures || !this.lectures.length) return;
      var full = ICS.db.getLecture(subId);
      if (!full) return;
      for (var i = 0; i < this.lectures.length; i++) {
        if (String(this.lectures[i].sub_id) === String(subId)) {
          this.lectures[i].summary = full.summary;
          break;
        }
      }
    },

    // Poll db load progress for the global bar until everything is loaded.
    // Cheap (a single pass over the lecture skeletons) and self-stopping.
    _trackLoadProgress() {
      var self = this;
      clearInterval(this._loadProgressTimer);
      var update = function () {
        self.loadProgress = ICS.db.getLoadProgress();
        if (self.loadProgress.lecturesPending === 0) {
          clearInterval(self._loadProgressTimer);
          self._loadProgressTimer = null;
        }
      };
      update();
      this._loadProgressTimer = setInterval(update, 500);
    },

    async _preloadInBackground() {
      var courses = ICS.db.getCourses();
      // Concurrency = max lectures in any single course, so one focused course
      // can have all its shards in flight at once (still funneled through the
      // scheduler's focus/abort logic). Falls back to 1 for an empty catalog.
      var maxPerCourse = 1;
      var perCourse = [];
      for (var i = 0; i < courses.length; i++) {
        var lecs = ICS.db.getLectures(courses[i].course_id);
        perCourse.push(lecs);
        if (lecs.length > maxPerCourse) maxPerCourse = lecs.length;
      }
      ICS.scheduler.setConcurrency(maxPerCourse);
      // Enqueue every shard at low priority; the scheduler bounds concurrency
      // and lets focus() jump an opened course ahead of (and pause) the rest.
      for (var i = 0; i < courses.length; i++) {
        var cid = courses[i].course_id;
        var lecs = perCourse[i];
        for (var j = 0; j < lecs.length; j++) {
          if (!ICS.db.isLectureLoaded(lecs[j].sub_id)) {
            _loadLecture(lecs[j].sub_id, cid, _PRIO.BG_PRELOAD).catch(function () {});
          }
        }
        _loadPpt(cid, _PRIO.BG_PRELOAD).catch(function () {});
      }
      // Catalog (lowest priority; same group/key as the focused load so the
      // two dedupe — opening Subscriptions promotes this very task).
      if (!this._catalogLoaded) {
        _loadCatalogShard(_PRIO.BG_PRELOAD)
          .then((data) => { ICS.db.loadCatalog(data); this._catalogLoaded = true; })
          .catch(function () {});
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

    nextDetailViewLabel() {
      var idx = _DETAIL_VIEW_CYCLE.indexOf(this.detailView);
      return _DETAIL_VIEW_LABEL[_DETAIL_VIEW_CYCLE[(idx + 1) % _DETAIL_VIEW_CYCLE.length]];
    },
    getExportableLectures() {
      return this.lectures.filter(function(l) { return l.state === 'ready'; });
    },
    isExportAllSelected() {
      var exportable = this.getExportableLectures();
      if (!exportable.length) return false;
      var sel = this.exportSelection;
      return exportable.every(function(l) { return sel[l.sub_id]; });
    },
    selectedExportCount() {
      var sel = this.exportSelection;
      return this.getExportableLectures().filter(function(l) { return sel[l.sub_id]; }).length;
    },
    setExportAll(checked) {
      var exportable = this.getExportableLectures();
      for (var i = 0; i < exportable.length; i++) {
        this.exportSelection[exportable[i].sub_id] = checked;
      }
    },
    formatPptTimestamp(sec) { return _formatTimestamp(sec); },
    openExportDialog() {
      this.exportSelection = {};
      this.exportDialogOpen = true;
    },
    closeExportDialog() { this.exportDialogOpen = false; },
    isLectureSelected(subId) { return !!this.exportSelection[subId]; },
    toggleLectureSelection(subId) {
      this.exportSelection[subId] = !this.exportSelection[subId];
    },
    async exportSelectedToClipboard() { await this.exportMarkdown(); },
    async exportSelectedToPdf() {
      // PDF export via workflow trigger
      var creds = _loadCreds();
      if (!creds?.token) { this._toast("需要 PAT 来触发导出", "error"); return; }
      var selected = this.lectures.filter((l) => this.exportSelection[l.sub_id]);
      if (!selected.length) { this._toast("请先选择课次", "error"); return; }
      this.exportingPdf = true;
      try {
        var subIds = selected.map(l => l.sub_id).join(",");
        var courseId = this.currentCourse?.course_id || "";
        // Trigger export workflow
        var url = "https://api.github.com/repos/" + this.repoOwner + "/" + this.repoName + "/actions/workflows/export.yml/dispatches";
        var res = await fetch(url, {
          method: "POST",
          headers: { Authorization: "token " + creds.token, Accept: "application/vnd.github+json", "Content-Type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: { course_id: courseId, export_type: "PDF", sub_ids: subIds } }),
        });
        if (res.status === 204) this._toast("PDF 导出已触发，请查看邮箱", "success");
        else throw new Error("触发失败: " + res.status);
      } catch (e) { this._toast(e.message, "error"); }
      finally { this.exportingPdf = false; }
    },
  }));
});
