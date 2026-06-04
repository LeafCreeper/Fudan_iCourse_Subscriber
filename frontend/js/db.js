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
  const cols = result[0].columns;
  const placeholders = cols.map(() => "?").join(",");
  const stmt = dst.prepare(
    `INSERT OR IGNORE INTO ${table} (${cols.join(",")}) VALUES (${placeholders})`
  );
  for (const row of result[0].values) {
    stmt.bind(row);
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
  if (row.error_stage) return "failed";
  if (row.summary && row.processed_at) return "ready";
  if (row.transcript && !row.summary) return "processing";
  return "waiting";
}

function _queryAll(sql, params) {
  if (!_db) return [];
  const stmt = _db.prepare(sql);
  if (params) stmt.bind(params);
  const results = [];
  while (stmt.step()) results.push(stmt.getAsObject());
  stmt.free();
  return results;
}

function _getCourses() {
  return _queryAll(`
    SELECT c.course_id AS course_id, c.title AS title, c.teacher AS teacher,
           COUNT(CASE WHEN l.summary IS NOT NULL THEN 1 END) AS summary_count,
           COUNT(l.sub_id) AS total_count,
           MAX(l.processed_at) AS last_updated
    FROM courses c
    LEFT JOIN lectures l ON c.course_id = l.course_id
    GROUP BY c.course_id
    ORDER BY last_updated DESC NULLS LAST
  `);
}

function _getLectures(courseId) {
  const rows = _queryAll(`
    SELECT sub_id, sub_title, date, summary, processed_at,
           error_stage, error_msg, summary_model, transcript
    FROM lectures
    WHERE course_id = ?
    ORDER BY CAST(sub_id AS INTEGER) ASC, sub_id ASC
  `, [courseId]);
  return rows.map((r) => {
    r.state = _deriveState(r);
    delete r.transcript;
    return r;
  });
}

function _getLecture(subId) {
  const rows = _queryAll(`
    SELECT l.*, c.title AS course_title, c.teacher
    FROM lectures l JOIN courses c ON l.course_id = c.course_id
    WHERE l.sub_id = ?
  `, [subId]);
  if (!rows.length) return null;
  rows[0].state = _deriveState(rows[0]);
  return rows[0];
}

function _getPptPages(subId) {
  // Only return done pages with non-empty text — keeps the PPT viewer
  // free of pending placeholders and dropped pages.
  return _queryAll(`
    SELECT page_num, created_sec, text
    FROM ppt_pages
    WHERE sub_id = ? AND ocr_status = 'done'
      AND text IS NOT NULL AND text != ''
    ORDER BY created_sec ASC
  `, [subId]);
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
    ORDER BY l.processed_at DESC LIMIT ? OFFSET ?
  `, params.concat([pageSize + 1, offset]));

  var hasMore = rows.length > pageSize;
  if (hasMore) rows.pop();
  return { results: rows, page: page, hasMore: hasMore };
}

function _getAllCourses(term) {
  // Catalog of every course offered by the school for ``term`` (or all
  // terms if undefined).  Populated by main.py's CRAWL_TERM-driven crawl;
  // empty until that env var has been set at least once on the CI side.
  if (term) {
    return _queryAll(
      "SELECT * FROM all_courses WHERE term = ? ORDER BY title",
      [term],
    );
  }
  return _queryAll(
    "SELECT * FROM all_courses ORDER BY term DESC, title"
  );
}

function _getAllCoursesTerms() {
  return _queryAll(
    "SELECT DISTINCT term FROM all_courses ORDER BY term DESC"
  ).map((r) => r.term);
}

function _buildCatalogWhere(filters) {
  // Shared WHERE/params builder for paged search + count + dept distinct.
  // Filters: { terms: string[], depts: string[], title: string, teacher: string }
  var clauses = [];
  var params = [];
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

function _getSubscribedCourseIds() {
  // The ``courses`` table holds courses we've actually run.  This is our
  // best signal of "currently subscribed" without reading the
  // COURSE_IDS secret (which GitHub never exposes back).
  return _queryAll("SELECT course_id FROM courses").map((r) => r.course_id);
}

function _getMeta(key) {
  var rows = _queryAll("SELECT value FROM meta WHERE key = ?", [key]);
  return rows.length ? rows[0].value : null;
}

window.ICS.db = {
  initDB: _initFromBytes,
  initEmpty: _initEmpty,
  attachShard: _attachShard,
  exportDB: _exportDB,
  getCourses: _getCourses,
  getLectures: _getLectures,
  getLecture: _getLecture,
  getPptPages: _getPptPages,
  searchSummaries: _searchSummaries,
  getAllCourses: _getAllCourses,
  getAllCoursesTerms: _getAllCoursesTerms,
  searchAllCourses: _searchAllCourses,
  countAllCourses: _countAllCourses,
  getCoursesByIds: _getCoursesByIds,
  getAllCoursesDepts: _getAllCoursesDepts,
  getSubscribedCourseIds: _getSubscribedCourseIds,
  getMeta: _getMeta,
};
