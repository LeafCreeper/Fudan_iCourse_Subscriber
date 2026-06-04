/**
 * JSON data layer — replaces sql.js with in-memory JSON + lazy loading.
 *
 * Data comes from pre-built encrypted JSON shards (built at deploy time).
 * Index (courses + lecture skeletons) loads on startup; lecture content
 * and PPT pages load on demand when the user navigates to detail views.
 */

window.ICS = window.ICS || {};

/* ── Internal state ── */
var _courses = [];
var _lectures = [];
var _lectureContent = {}; // sub_id -> {transcript, summary, summary_model}
var _pptCache = {};       // course_id -> [{sub_id, page_num, text, created_sec}]
var _catalog = null;      // [{course_id, term, title, teacher, dept}]
var _meta = {};           // key -> value (from index if present)

/* ── Init ── */
function _initFromIndex(indexData) {
  _courses = indexData.courses || [];
  _lectures = indexData.lectures || [];
  _meta = indexData.meta || {};
}

/* ── Courses ── */
function _getCourses() {
  return _courses.map(function(c) {
    return {
      course_id: c.course_id,
      title: c.title,
      teacher: c.teacher,
      summary_count: c.summary_count || 0,
      total_count: c.total_count || 0,
      last_updated: c.last_updated || null,
    };
  });
}

/* ── Lectures ── */
function _getLectures(courseId) {
  return _lectures
    .filter(function(l) { return l.course_id === courseId; })
    .map(function(l) {
      var content = _lectureContent[l.sub_id];
      return {
        sub_id: l.sub_id,
        sub_title: l.sub_title,
        date: l.date,
        summary: content ? content.summary : null,
        processed_at: l.processed_at,
        state: l.state,
        summary_model: l.summary_model,
      };
    });
}

function _getLecture(subId) {
  var skel = null;
  for (var i = 0; i < _lectures.length; i++) {
    if (_lectures[i].sub_id === subId) { skel = _lectures[i]; break; }
  }
  if (!skel) return null;
  var content = _lectureContent[subId];
  var course = null;
  for (var j = 0; j < _courses.length; j++) {
    if (_courses[j].course_id === skel.course_id) { course = _courses[j]; break; }
  }
  return {
    sub_id: skel.sub_id,
    course_id: skel.course_id,
    sub_title: skel.sub_title,
    date: skel.date,
    processed_at: skel.processed_at,
    state: skel.state,
    summary_model: skel.summary_model || (content && content.summary_model) || null,
    transcript: content ? content.transcript : null,
    summary: content ? content.summary : null,
    course_title: course ? course.title : "",
    teacher: course ? course.teacher : "",
  };
}

function _isLectureLoaded(subId) {
  return subId in _lectureContent;
}

/* ── Lazy loaders (called by app.js with a fetcher function) ── */
async function _loadLectureContent(subId, fetcher) {
  if (_lectureContent[subId]) return _lectureContent[subId];
  var data = await fetcher(subId);
  _lectureContent[subId] = data;
  return data;
}

async function _loadPptPages(courseId, fetcher) {
  if (_pptCache[courseId]) return _pptCache[courseId];
  var data = await fetcher(courseId);
  _pptCache[courseId] = data;
  return data;
}

function _getPptPages(subId) {
  // Search all cached PPT data for pages matching this sub_id
  for (var cid in _pptCache) {
    var pages = _pptCache[cid];
    var matched = pages.filter(function(p) { return p.sub_id === subId; });
    if (matched.length) return matched;
  }
  return [];
}

/* ── Search ── */
function _searchSummaries(query) {
  if (!query || !query.trim()) return [];
  var q = query.trim().toLowerCase();
  var results = [];
  for (var i = 0; i < _lectures.length && results.length < 50; i++) {
    var lec = _lectures[i];
    var content = _lectureContent[lec.sub_id];
    var summary = (content && content.summary) || "";
    var transcript = (content && content.transcript) || "";
    var subTitle = lec.sub_title || "";
    var hitField = null;
    var pptText = "";
    if (summary && summary.toLowerCase().indexOf(q) !== -1) {
      hitField = "summary";
    } else if (subTitle.toLowerCase().indexOf(q) !== -1) {
      hitField = "sub_title";
    } else if (transcript && transcript.toLowerCase().indexOf(q) !== -1) {
      hitField = "transcript";
    } else {
      // OCR / PPT text (lazy-loaded into _pptCache; matches whatever is loaded)
      var pages = _getPptPages(lec.sub_id);
      for (var p = 0; p < pages.length; p++) {
        var t = pages[p].text || "";
        if (t.toLowerCase().indexOf(q) !== -1) { hitField = "ocr"; pptText = t; break; }
      }
    }
    if (hitField) {
      var course = null;
      for (var j = 0; j < _courses.length; j++) {
        if (_courses[j].course_id === lec.course_id) { course = _courses[j]; break; }
      }
      results.push({
        sub_id: lec.sub_id,
        sub_title: subTitle,
        course_id: lec.course_id,
        course_title: course ? course.title : "",
        summary: summary,
        transcript: transcript,
        ppt_text: pptText,
        hit_field: hitField,
      });
    }
  }
  return results;
}

/* ── Catalog (all_courses for subscription editor) ── */
function _loadCatalog(data) {
  _catalog = data;
}

function _getAllCourses(term) {
  if (!_catalog) return [];
  if (term) return _catalog.filter(function(c) { return c.term === term; });
  return _catalog;
}

function _getAllCoursesTerms() {
  if (!_catalog) return [];
  var seen = {};
  var terms = [];
  for (var i = 0; i < _catalog.length; i++) {
    var t = _catalog[i].term;
    if (t && !seen[t]) { seen[t] = true; terms.push(t); }
  }
  return terms.sort().reverse();
}

function _searchAllCourses(filters, limit) {
  if (!_catalog) return [];
  limit = limit || 200;
  var results = _catalog;
  if (filters.terms && filters.terms.length) {
    var ts = {};
    for (var i = 0; i < filters.terms.length; i++) ts[filters.terms[i]] = true;
    results = results.filter(function(c) { return ts[c.term]; });
  }
  if (filters.depts && filters.depts.length) {
    var ds = {};
    for (var i = 0; i < filters.depts.length; i++) ds[filters.depts[i]] = true;
    results = results.filter(function(c) { return ds[c.dept]; });
  }
  if (filters.title && filters.title.trim()) {
    var tq = filters.title.trim().toLowerCase();
    results = results.filter(function(c) { return c.title && c.title.toLowerCase().indexOf(tq) !== -1; });
  }
  if (filters.teacher && filters.teacher.trim()) {
    var teq = filters.teacher.trim().toLowerCase();
    results = results.filter(function(c) { return c.teacher && c.teacher.toLowerCase().indexOf(teq) !== -1; });
  }
  return results.slice(0, limit);
}

function _countAllCourses(filters) {
  return _searchAllCourses(filters, 999999).length;
}

function _getCoursesByIds(ids) {
  if (!ids || !ids.length) return [];
  var idSet = {};
  for (var i = 0; i < ids.length; i++) idSet[String(ids[i])] = true;
  var out = [];
  var found = {};
  if (_catalog) {
    for (var j = 0; j < _catalog.length; j++) {
      var c = _catalog[j];
      var cid = String(c.course_id);
      if (idSet[cid] && !found[cid]) {
        found[cid] = true;
        out.push(c);
      }
    }
  }
  // Synthesize placeholders for missing IDs
  for (var k = 0; k < ids.length; k++) {
    var idStr = String(ids[k]);
    if (!found[idStr]) {
      out.push({ course_id: idStr, term: "", title: "", teacher: "", dept: "" });
      found[idStr] = true;
    }
  }
  return out;
}

function _getAllCoursesDepts(termFilter, search) {
  if (!_catalog) return [];
  var filtered = _catalog;
  if (termFilter && termFilter.length) {
    var ts = {};
    for (var i = 0; i < termFilter.length; i++) ts[termFilter[i]] = true;
    filtered = filtered.filter(function(c) { return ts[c.term]; });
  }
  var seen = {};
  var depts = [];
  for (var j = 0; j < filtered.length; j++) {
    var d = filtered[j].dept;
    if (d && !seen[d]) {
      if (!search || !search.trim() || d.toLowerCase().indexOf(search.trim().toLowerCase()) !== -1) {
        seen[d] = true;
        depts.push(d);
      }
    }
  }
  return depts.sort();
}

function _getSubscribedCourseIds() {
  return _courses.map(function(c) { return c.course_id; });
}

function _getMeta(key) {
  return _meta[key] || null;
}

window.ICS.db = {
  initFromIndex: _initFromIndex,
  getCourses: _getCourses,
  getLectures: _getLectures,
  getLecture: _getLecture,
  isLectureLoaded: _isLectureLoaded,
  loadLectureContent: _loadLectureContent,
  loadPptPages: _loadPptPages,
  getPptPages: _getPptPages,
  searchSummaries: _searchSummaries,
  loadCatalog: _loadCatalog,
  getAllCourses: _getAllCourses,
  getAllCoursesTerms: _getAllCoursesTerms,
  searchAllCourses: _searchAllCourses,
  countAllCourses: _countAllCourses,
  getCoursesByIds: _getCoursesByIds,
  getAllCoursesDepts: _getAllCoursesDepts,
  getSubscribedCourseIds: _getSubscribedCourseIds,
  getMeta: _getMeta,
};
