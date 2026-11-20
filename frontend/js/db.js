/**
 * sql.js wrapper — load lecture data from sharded encrypted shards.
 *
 * In the sharded layout (current), each shard is a self-contained sqlite
 * file holding only the courses + lectures + ppt_pages rows for the courses
 * it owns. The page reassembles them in-memory by copying every shard's
 * rows into a single working SQL.Database. This avoids ATTACH'ing across
 * sql.js DB instances — sql.js doesn't support cross-file ATTACH cleanly,
 * and the row-count is small enough that copying is instantaneous.
 */

window.ICS = window.ICS || {};

// Semester terms that predate the recording system — never shown anywhere.
const _INVALID_TERMS_GLOB = ["*_19_*"];
const _INVALID_TERMS_EXACT = ["25"];

const _SQL_CDN = "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.12.0";
let _db = null;
let _SQL = null;

function _schemaSql() {
  // schema.js loads before this file via index.html and registers the SQL
  // on window.ICS.schema so backend (src/schema.py) and frontend share one
  // source of truth.  Throw early if it's missing — silent NULL would
  // produce "no such table" later, which is a worse failure mode.
  var s = window.ICS && window.ICS.schema && window.ICS.schema.SCHEMA_SQL;
  if (!s) throw new Error("ICS.schema.SCHEMA_SQL missing — load js/schema.js first");
  return s;
}

async function _ensureSqlJs() {
  if (_SQL) return _SQL;
  _SQL = await window.initSqlJs({
    locateFile: (file) => `${_SQL_CDN}/${file}`,
  });
  return _SQL;
}

async function _initFromBytes(dbBytes) {
  // Legacy path — accepts a single monolithic sqlite file.
  // Kept so older deployments (pre-shard data branch) still load.
  const SQL = await _ensureSqlJs();
  _db = dbBytes ? new SQL.Database(dbBytes) : new SQL.Database();
  if (!dbBytes) _db.exec(_schemaSql());
  // Ensure new tables exist when loading a cached DB from an older version
  _db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)");
}

async function _initEmpty() {
  const SQL = await _ensureSqlJs();
  _db = new SQL.Database();
  _db.exec(_schemaSql());
  _db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)");
}

function _exportDB() {
  // Returns the full merged DB as Uint8Array (for caching in IndexedDB).
  if (!_db) throw new Error("Database not initialized");
  return _db.export();
}

function _copyRows(src, dst, table) {
  const result = src.exec(`SELECT * FROM ${table}`);
  if (!result.length || !result[0].values.length) return;
  // Shard may have extra columns that the frontend's schema no longer
  // defines (e.g. summary_format_version).  Only copy columns that
  // actually exist in the destination table.
  var dstCols = {};
  try {
    var info = dst.exec("PRAGMA table_info(" + table + ")");
    if (info.length && info[0].values) {
      for (var i = 0; i < info[0].values.length; i++) {
        dstCols[info[0].values[i][1]] = true;
      }
    }
  } catch (e) { /* fall through — use all source columns */ }
  const srcCols = result[0].columns;
  var cols = srcCols;
  var colIndexes = null;
  if (Object.keys(dstCols).length) {
    cols = [];
    colIndexes = [];
    for (var j = 0; j < srcCols.length; j++) {
      if (dstCols[srcCols[j]]) {
        cols.push(srcCols[j]);
        colIndexes.push(j);
      }
    }
  }
  if (!cols.length) return;
  const placeholders = cols.map(function () { return "?"; }).join(",");
  const stmt = dst.prepare(
    "INSERT OR IGNORE INTO " + table + " (" + cols.join(",") + ") VALUES (" + placeholders + ")"
  );
  for (var k = 0; k < result[0].values.length; k++) {
    var row = result[0].values[k];
    if (colIndexes) {
      var filtered = [];
      for (var m = 0; m < colIndexes.length; m++) {
        filtered.push(row[colIndexes[m]]);
      }
      stmt.bind(filtered);
    } else {
      stmt.bind(row);
    }
    stmt.step();
    stmt.reset();
  }
  stmt.free();
}

async function _attachShard(shardBytes) {
  if (!_db) throw new Error("Database not initialized");
  const SQL = await _ensureSqlJs();
  const shard = new SQL.Database(shardBytes);
  // Check which tables exist in the shard — the ``meta`` table is new
  // (added 2026-05) and old shards don't have it.
  var shardTables = {};
  try {
    var tableRows = shard.exec("SELECT name FROM sqlite_master WHERE type='table'");
    if (tableRows.length && tableRows[0].values) {
      for (var i = 0; i < tableRows[0].values.length; i++) {
        shardTables[tableRows[0].values[i][0]] = true;
      }
    }
  } catch (e) { /* old sql.js version — fall through */ }
  try {
    _copyRows(shard, _db, "courses");
    _copyRows(shard, _db, "lectures");
    _copyRows(shard, _db, "ppt_pages");
    _copyRows(shard, _db, "all_courses");
    if (shardTables["meta"]) _copyRows(shard, _db, "meta");
  } finally {
    shard.close();
  }
}

function _deriveState(row) {
  // "no_video" is a soft error stage the backend records when the lecture
  // has no playable video yet (it retries a few runs in case the recording
  // appears later).  Render it as a gray informational badge, not a red
  // failure.
  if (row.error_stage === "no_video") return "novideo";
  if (row.error_stage) return "failed";
  if (row.summary && row.processed_at) return "ready";
  // Processed, no summary, no error = a lecture the backend permanently
  // skipped (no audio stream / empty transcript) and marked done.
  // Distinct from "waiting" (enqueued, not yet run) so these don't show
  // as perpetually pending in the UI.
  if (row.processed_at) return "skipped";
  if (row.transcript) return "processing";
  return "waiting";
}

function _queryAll(sql, params, db = _db) {
  if (!db) return [];
  const stmt = db.prepare(sql);
  if (params) stmt.bind(params);
  const results = [];
  while (stmt.step()) results.push(stmt.getAsObject());
  stmt.free();
  return results;
}

function _getCourses(db = _db) {
  return _queryAll(`
    SELECT c.course_id AS course_id, c.title AS title, c.teacher AS teacher,
           COUNT(CASE WHEN l.summary IS NOT NULL THEN 1 END) AS summary_count,
           COUNT(l.sub_id) AS total_count,
           MAX(l.processed_at) AS last_updated
    FROM courses c
    LEFT JOIN lectures l ON c.course_id = l.course_id
    GROUP BY c.course_id
    ORDER BY last_updated DESC NULLS LAST, c.course_id
  `, [], db);
}

// Sort key parsed straight from sub_title — we deliberately do NOT trust the
// `date` column. `date` is itself just substr(sub_title,1,10) on the backend
// and is inconsistently back-filled: unprocessed lectures often ship with an
// empty `date`, and `ORDER BY date` then floats that whole block ahead of the
// populated rows (the cause of the two-run, out-of-chronological-order list).
// Parsing sub_title here gives a reliable chronological order regardless.
//   "2026-03-09第6-8节" -> { dateNum: 20260309, period: 6 }
function _lectureOrderKey(subTitle) {
  var s = String(subTitle || "");
  var dm = s.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  var dateNum = dm
    ? parseInt(dm[1], 10) * 10000 + parseInt(dm[2], 10) * 100 + parseInt(dm[3], 10)
    : null;
  var pm = s.match(/第\s*(\d+)/);  // first period number, e.g. 第11-12节 -> 11
  var period = pm ? parseInt(pm[1], 10) : 0;
  return { dateNum: dateNum, period: period, raw: s };
}

function _getLectures(courseId) {
  const rows = _queryAll(`
    SELECT sub_id, sub_title, date, summary, processed_at,
           error_stage, error_msg, summary_model, transcript
    FROM lectures WHERE course_id = ?
  `, [courseId]);
  return _sortLectureRows(rows).map((r) => {
    r.state = _deriveState(r);
    delete r.transcript;
    return r;
  });
}

function _sortLectureRows(rows) {
  // Chronological ascending (earliest first); 第N-M节 breaks intra-day ties so
  // a morning session sorts before an afternoon one. Lectures with no parseable
  // date in their sub_title sort last (their position is genuinely unknown).
  return rows.sort(function (a, b) {
    var ka = _lectureOrderKey(a.sub_title), kb = _lectureOrderKey(b.sub_title);
    if (ka.dateNum === null || kb.dateNum === null) {
      if (ka.dateNum === null && kb.dateNum === null)
        return _orderValues(ka.raw, kb.raw) || _orderValues(a.sub_id, b.sub_id);
      return ka.dateNum === null ? 1 : -1;
    }
    if (ka.dateNum !== kb.dateNum) return ka.dateNum - kb.dateNum;
    if (ka.period !== kb.period) return ka.period - kb.period;
    return _orderValues(ka.raw, kb.raw) || _orderValues(a.sub_id, b.sub_id);
  });
}

function _getLecture(subId, db = _db) {
  const rows = _queryAll(`
    SELECT l.*, c.title AS course_title, c.teacher
    FROM lectures l JOIN courses c ON l.course_id = c.course_id
    WHERE l.sub_id = ?
  `, [subId], db);
  if (!rows.length) return null;
  rows[0].state = _deriveState(rows[0]);
  return rows[0];
}

function _getPptPages(subId, db = _db) {
  // Only return done pages with non-empty text — keeps the PPT viewer
  // free of pending placeholders and dropped pages.
  return _queryAll(`
    SELECT page_num, created_sec, text
    FROM ppt_pages
    WHERE sub_id = ? AND ocr_status = 'done'
      AND text IS NOT NULL AND text != ''
    ORDER BY created_sec ASC, page_num ASC
  `, [subId], db);
}

function _searchSummaries(query, courseIds, page, pageSize, domains) {
  if (!query?.trim()) return { results: [], page: 1, hasMore: false };
  page = page || 1;
  pageSize = pageSize || 50;
  const offset = (page - 1) * pageSize;
  const q = query;

  // Domain flags — default all enabled
  const d = domains || {};
  const matchSummary = d.summary !== false;
  const matchTranscript = d.transcript !== false;
  const matchOcr = d.ocr !== false;

  // Build WHERE parts per active domain
  var textParts = [];
  var textParams = [];
  function addText(cond) { textParts.push(cond); textParams.push(q); }
  if (matchSummary)    addText("l.summary    LIKE '%' || ? || '%'");
  if (matchTranscript) addText("l.transcript LIKE '%' || ? || '%'");
  if (matchOcr)        addText("EXISTS(SELECT 1 FROM ppt_pages pp3 WHERE pp3.sub_id = l.sub_id AND pp3.ocr_status = 'done' AND pp3.text LIKE '%' || ? || '%')");

  if (!textParts.length) return { results: [], page: 1, hasMore: false };

  // Build hit_field CASE for active domains
  var caseParts = [];
  var caseParams = [];
  function addCase(when, then) { caseParts.push("WHEN " + when + " THEN '" + then + "'"); caseParams.push(q); }
  if (matchSummary)    addCase("l.summary    LIKE '%' || ? || '%'", "summary");
  if (matchTranscript) addCase("l.transcript LIKE '%' || ? || '%'", "transcript");
  if (matchOcr)        addCase("EXISTS(SELECT 1 FROM ppt_pages pp2 WHERE pp2.sub_id = l.sub_id AND pp2.ocr_status = 'done' AND pp2.text LIKE '%' || ? || '%')", "ocr");

  // ppt_text subquery (for OCR snippet, only when OCR domain active)
  var pptSql = matchOcr
    ? "(SELECT pp.text FROM ppt_pages pp WHERE pp.sub_id = l.sub_id AND pp.ocr_status = 'done' AND pp.text LIKE '%' || ? || '%' LIMIT 1)"
    : "NULL";
  var pptParams = matchOcr ? [q] : [];

  // Full params: ppt_text + CASE + WHERE
  var params = pptParams.concat(caseParams, textParams);

  // WHERE clause with optional course filter
  var whereClauses = ["(" + textParts.join("\n           OR ") + ")"];
  if (courseIds && courseIds.length) {
    var placeholders = courseIds.map(function () { return "?"; }).join(",");
    whereClauses.push("l.course_id IN (" + placeholders + ")");
    courseIds.forEach(function (id) { params.push(String(id)); });
  }

  var caseSql = caseParts.length
    ? "CASE\n             " + caseParts.join("\n             ") + "\n             ELSE 'other'\n           END"
    : "'other'";

  // Fetch pageSize+1 rows to detect whether a next page exists
  var rows = _queryAll(`
    SELECT l.sub_id, l.sub_title, l.summary, l.transcript,
           ${pptSql} AS ppt_text,
           l.course_id, c.title AS course_title,
           ${caseSql} AS hit_field
    FROM lectures l JOIN courses c ON l.course_id = c.course_id
    WHERE ` + whereClauses.join(" AND ") + `
    ORDER BY l.processed_at DESC, l.sub_id DESC LIMIT ? OFFSET ?
  `, params.concat([pageSize + 1, offset]));

  var hasMore = rows.length > pageSize;
  if (hasMore) rows.pop();
  return { results: rows, page: page, hasMore: hasMore };
}

function _invalidTermExclusion() {
  // Single source of truth for excluding pre-recording-system terms. Used by
  // both the catalog WHERE builder and the term dropdown so the two never
  // diverge (they previously used different mechanisms — SQL GLOB here vs a
  // JS substring test in the dropdown — which could disagree).
  var clauses = [];
  var params = [];
  for (var gi = 0; gi < _INVALID_TERMS_GLOB.length; gi++) {
    clauses.push("term NOT GLOB ?");
    params.push(_INVALID_TERMS_GLOB[gi]);
  }
  for (var ei = 0; ei < _INVALID_TERMS_EXACT.length; ei++) {
    clauses.push("term != ?");
    params.push(_INVALID_TERMS_EXACT[ei]);
  }
  return { clauses: clauses, params: params };
}

function _getAllCoursesTerms() {
  var ex = _invalidTermExclusion();
  var where = ex.clauses.length ? "WHERE " + ex.clauses.join(" AND ") : "";
  return _queryAll(
    "SELECT DISTINCT term FROM all_courses " + where + " ORDER BY term DESC",
    ex.params,
  ).map(function (r) { return r.term; });
}

function _buildCatalogWhere(filters) {
  // Shared WHERE/params builder for paged search + count + dept distinct.
  // Filters: { terms: string[], depts: string[], title: string, teacher: string }
  var ex = _invalidTermExclusion();
  var clauses = ex.clauses.slice();
  var params = ex.params.slice();
  if (filters.terms && filters.terms.length) {
    clauses.push("term IN (" + filters.terms.map(function () { return "?"; }).join(",") + ")");
    for (var i = 0; i < filters.terms.length; i++) params.push(filters.terms[i]);
  }
  if (filters.depts && filters.depts.length) {
    clauses.push("dept IN (" + filters.depts.map(function () { return "?"; }).join(",") + ")");
    for (var j = 0; j < filters.depts.length; j++) params.push(filters.depts[j]);
  }
  if (filters.title && filters.title.trim()) {
    clauses.push("title LIKE ?");
    params.push("%" + filters.title.trim() + "%");
  }
  if (filters.teacher && filters.teacher.trim()) {
    clauses.push("teacher LIKE ?");
    params.push("%" + filters.teacher.trim() + "%");
  }
  return {
    where: clauses.length ? "WHERE " + clauses.join(" AND ") : "",
    params: params,
  };
}

function _searchAllCourses(filters, limit) {
  // Paged catalog search — used by the subscriptions editor middle column.
  // Pushes all filtering into sqlite so the JS heap never holds the full
  // 20k-row catalog.
  var w = _buildCatalogWhere(filters || {});
  var sql = "SELECT course_id, term, title, teacher, dept FROM all_courses "
          + w.where + " ORDER BY term DESC, title LIMIT ?";
  var p = w.params.slice();
  p.push(limit || 200);
  return _queryAll(sql, p);
}

function _countAllCourses(filters) {
  var w = _buildCatalogWhere(filters || {});
  var rows = _queryAll("SELECT COUNT(*) AS n FROM all_courses " + w.where, w.params);
  return rows.length ? rows[0].n : 0;
}

function _getCoursesByIds(ids) {
  // Look up the catalog rows for an arbitrary set of course_ids.  Used by
  // the left ("已订阅") and right ("单次运行") columns so they can render
  // without holding the full catalog in JS.  Deduplicates by course_id;
  // a course offered in multiple terms is shown with its most recent term.
  if (!ids || !ids.length) return [];
  var placeholders = ids.map(function () { return "?"; }).join(",");
  // Pull every term row that matches, then collapse to one per course_id
  // (preferring the most recent term) in JS — keeps the SQL simple.
  var rows = _queryAll(
    "SELECT course_id, term, title, teacher, dept FROM all_courses "
    + "WHERE course_id IN (" + placeholders + ") ORDER BY term DESC",
    ids.map(String),
  );
  var seen = {};
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var cid = String(rows[i].course_id);
    if (seen[cid]) continue;
    seen[cid] = true;
    out.push(rows[i]);
  }
  // Synthesize empty placeholders for IDs the catalog doesn't know about,
  // so the count shown to the user matches what's actually in their list.
  var foundIds = {};
  for (var k = 0; k < out.length; k++) foundIds[String(out[k].course_id)] = true;
  for (var m = 0; m < ids.length; m++) {
    var idStr = String(ids[m]);
    if (!foundIds[idStr]) {
      out.push({ course_id: idStr, term: "", title: "", teacher: "", dept: "" });
      foundIds[idStr] = true;
    }
  }
  return out;
}

function _getAllCoursesDepts(termFilter, search) {
  // Distinct dept list, optionally narrowed to a set of terms and a
  // case-insensitive substring search.  Lets the dept dropdown stay
  // responsive without iterating the JS catalog.
  var clauses = ["dept IS NOT NULL", "dept != ''"];
  var params = [];
  if (termFilter && termFilter.length) {
    clauses.push("term IN (" + termFilter.map(function () { return "?"; }).join(",") + ")");
    for (var i = 0; i < termFilter.length; i++) params.push(termFilter[i]);
  }
  if (search && search.trim()) {
    clauses.push("LOWER(dept) LIKE ?");
    params.push("%" + search.trim().toLowerCase() + "%");
  }
  var rows = _queryAll(
    "SELECT DISTINCT dept FROM all_courses WHERE "
    + clauses.join(" AND ") + " ORDER BY dept",
    params,
  );
  return rows.map(function (r) { return r.dept; });
}

function _getMeta(key) {
  var rows = _queryAll("SELECT value FROM meta WHERE key = ?", [key]);
  return rows.length ? rows[0].value : null;
}


// The agent pins this reader for a turn so that a refresh can replace _db
// without changing the data underneath an in-flight query.
function _agentReader() {
  const db = _db;
  if (!db) return null;
  let courses;
  return {
    courses() {
      courses ??= _getCourses(db).map(row => ({
        course_id: row.course_id, title: row.title || "", teacher: row.teacher || "",
        lecture_count: row.total_count, updated_at: row.last_updated,
      }));
      return courses;
    },
    menu(courseId) {
      const rows = _queryAll(
        "SELECT sub_id, sub_title, substr(summary, 1, 80) AS summary, " +
        "processed_at, error_stage, transcript != '' AS transcript " +
        "FROM lectures WHERE course_id = ?", [courseId], db);
      return _sortLectureRows(rows).map(row => {
        const order = _lectureOrderKey(row.sub_title);
        const date = order.dateNum === null ? null : String(order.dateNum);
        return {
          lecture_id: row.sub_id, title: row.sub_title || "",
          date: date === null ? null : [date.slice(0, 4), date.slice(4, 6), date.slice(6)].join("-"),
          period_start: order.period || null, state: _deriveState(row),
          summary_excerpt: row.summary || null,
        };
      });
    },
    lecture: subId => _getLecture(subId, db),
    ocr: subId => _getPptPages(subId, db),
    ocrPending: subId => _queryAll(
      "SELECT 1 FROM ppt_pages WHERE sub_id = ? AND ocr_status IN ('pending', 'processing') LIMIT 1",
      [subId], db).length > 0,
    candidates({ keywords, modes, course_ids }) {
      const modeRank = { summary: 0, transcript: 1, ocr: 2 };
      const sqlCourse = course_ids ? " AND l.course_id IN (" + course_ids.map(() => "?").join(", ") + ")" : "";
      const found = modes.flatMap(mode => {
        const condition = mode === "ocr"
          ? "EXISTS (SELECT 1 FROM ppt_pages p WHERE p.sub_id = l.sub_id AND p.ocr_status = 'done' AND instr(lower(p.text), ?) > 0)"
          : "instr(lower(l." + mode + "), ?) > 0";
        return _queryAll(
          "SELECT l.sub_id AS lecture_id, l.sub_title AS lecture_title, " +
          "c.title AS course_title, l.processed_at " +
          "FROM lectures l JOIN courses c ON c.course_id = l.course_id " +
          "WHERE (" + keywords.map(() => condition).join(" OR ") + ")" + sqlCourse,
          [...keywords, ...(course_ids ?? [])], db,
        ).map(row => ({ ...row, mode, lecture_title: row.lecture_title || "", course_title: row.course_title || "" }));
      });
      return found.sort((a, b) =>
        _orderValues(b.processed_at ?? "", a.processed_at ?? "") ||
        _orderValues(a.lecture_id, b.lecture_id) || modeRank[a.mode] - modeRank[b.mode]);
    },
  };
}

function _orderValues(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

window.ICS.db = {
  agentReader: _agentReader,
  lectureSource: id => _queryAll(
    "SELECT l.sub_title AS lecture_title, c.title AS course_title, c.teacher " +
    "FROM lectures l JOIN courses c ON c.course_id = l.course_id WHERE l.sub_id = ?", [id],
  )[0],
  initDB: _initFromBytes,
  initEmpty: _initEmpty,
  attachShard: _attachShard,
  exportDB: _exportDB,
  getCourses: _getCourses,
  getLectures: _getLectures,
  getLecture: _getLecture,
  getPptPages: _getPptPages,
  searchSummaries: _searchSummaries,
  getAllCoursesTerms: _getAllCoursesTerms,
  searchAllCourses: _searchAllCourses,
  countAllCourses: _countAllCourses,
  getCoursesByIds: _getCoursesByIds,
  getAllCoursesDepts: _getAllCoursesDepts,
  getMeta: _getMeta,
};
