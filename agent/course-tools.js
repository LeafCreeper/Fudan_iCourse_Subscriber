import { asciiLower, countChars, failure, invalid, ok } from "./contract.js";

const textPage = 20000;
const missing = message => failure("NOT_FOUND", message, "fix_arguments");
const oversized = () => failure("RESULT_TOO_LARGE", "本轮留给原文的空间不足。请拆分读取。", "fix_arguments");
const invalidCursor = () => failure("INVALID_CURSOR", "请使用同一工具和查询参数返回的游标。", "restart");
const staleCursor = () => failure("STALE_CURSOR", "课程数据已更新，请重新查询。", "restart");
const unique = values => new Set(values).size === values.length;
export const readPageKey = ({ source, start }) =>
  JSON.stringify([source.course_id, source.lecture_id, source.mode, start]);

function pageOf(items, page, size) {
  const start = page * size;
  return { items: items.slice(start, start + size), total: items.length,
    page, page_max: size, next_page: start + size < items.length ? page + 1 : null };
}

function toOcr(pages) {
  let position = 0;
  return pages.map(({ page_num, created_sec, text }) => {
    const chars = Array.from(text);
    const start = position;
    const end = start + chars.length;
    position = end + 2;
    return { page: { page_num, created_sec }, start, end, chars };
  });
}

// Search each physical OCR page on its own. A group's keywords may match on
// different pages, but a single match and its snippet never cross a page.
function searchText(segments, keywords) {
  const counts = keywords.map(keyword => ({ keyword, count: 0 }));
  const hits = [];
  const terms = keywords.map(asciiLower);
  for (const { chars, start, page } of segments) {
    const folded = asciiLower(chars.join(""));
    const offsets = [0];
    for (const char of chars) offsets.push(offsets.at(-1) + char.length);
    const index = new Map(offsets.map((offset, char) => [offset, char]));
    terms.forEach((term, word) => {
      for (let from = 0; ; ) {
        const match = folded.indexOf(term, from);
        if (match < 0) break;
        from = match + 1;
        const left = index.get(match);
        const right = index.get(match + term.length);
        counts[word].count++;
        hits.push({ left, right, start, chars, page, word });
      }
    });
  }
  hits.sort((a, b) => a.start + a.left - b.start - b.left || a.word - b.word);
  return { counts, hits };
}

function snippet(hit, keywords) {
  const { left, right, start, chars, page, word } = hit;
  const from = Math.max(0, left - 30);
  const to = Math.min(chars.length, right + 30);
  return { keyword: keywords[word], snippet: chars.slice(from, to).join(""),
    start: start + from, ocr_page: page };
}

export function readCursorLocation(cursor) {
  const match = /^r:(\d+):(summary|transcript|ocr):(\d+):(.+)$/.exec(cursor);
  return match && { version: match[1], mode: match[2], start: Number(match[3]), id: match[4] };
}

export function createCourseTools(reader, snapshot, cursors, nextTextId) {
  const queryCache = new Map();
  const sourceCache = new Map();
  const knownSnapshot = cursors.snapshots.indexOf(snapshot);
  const version = String(knownSnapshot === -1 ? cursors.snapshots.push(snapshot) - 1 : knownSnapshot);
  const readCursor = (id, mode, start) => "r:" + version + ":" + mode + ":" + start + ":" + id;

  function issue(tool, key, state) {
    return String(cursors.pages.push({ tool, key, version, state }) - 1);
  }

  function take(id, tool, key) {
    const value = cursors.pages[id];
    if (!value || value.tool !== tool || value.key !== key) return invalidCursor();
    if (value.version !== version) return staleCursor();
    return ok(value.state);
  }

  function getSource(lectureId, mode) {
    const key = JSON.stringify([lectureId, mode]);
    if (!sourceCache.has(key)) sourceCache.set(key, loadSource(lectureId, mode));
    return sourceCache.get(key);
  }

  function loadSource(lectureId, mode) {
    const row = reader.lecture(lectureId);
    if (!row) return { reason: "LECTURE_NOT_FOUND" };
    const ocr = mode === "ocr" ? reader.ocr(lectureId) : [];
    const segments = mode === "ocr" ? toOcr(ocr) :
      row[mode] ? [{ chars: Array.from(row[mode]), start: 0, page: null }] : [];
    if (!segments.length) {
      const pending = mode === "ocr" ? reader.ocrPending(lectureId) :
        !row.processed_at && ["waiting", "processing"].includes(row.state);
      return { reason: pending ? "TEXT_PENDING" : "TEXT_MISSING" };
    }
    const text = mode === "ocr" ? ocr.map(page => page.text).join("\n\n") : row[mode];
    return { source: { course_id: row.course_id, lecture_id: lectureId, mode },
      segments, chars: segments.length === 1 ? segments[0].chars : Array.from(text) };
  }

  function readList(args, query = null) {
    let rows = reader.courses();
    if (query !== null) {
      const term = asciiLower(query.trim());
      rows = rows.filter(row => [row.title, row.teacher].some(value => asciiLower(value).includes(term)));
    }
    return ok(pageOf(rows, args.page ?? 0, args.page_max ?? 30));
  }

  function readCourse({ course_id, cursor }) {
    const position = cursor === null ? ok(0) : take(cursor, "read_course", course_id);
    if (!position.ok) return position;
    const course = reader.courses().find(row => row.course_id === course_id);
    if (!course) return missing("找不到该课程。请先查询课程 ID。");
    const rows = reader.menu(course_id);
    const end = position.data + 100;
    return ok({ course, items: rows.slice(position.data, end), total: rows.length,
      next_cursor: end < rows.length ? issue("read_course", course_id, end) : null });
  }

  function readLecture(args, room, sentPages) {
    const { lecture_ids, mode, cursor } = args;
    const continuation = readCursorLocation(cursor);
    if (cursor !== null && !continuation) return invalidCursor();
    if (continuation && continuation.version !== version) return staleCursor();
    const targets = continuation ? [continuation] : lecture_ids.map(id => ({
      id, mode: mode ?? "summary", start: 0,
    }));
    const items = [];
    let returnedPage = false;
    for (const { id, mode, start } of targets) {
      const result = getSource(id, mode);
      if (result.reason) {
        items.push({ status: "missing", lecture_id: id, mode, reason: result.reason });
        continue;
      }
      const { chars, source, segments } = result;
      if (start >= chars.length) return invalidCursor();
      const next = position => readCursor(id, mode, position);
      const key = readPageKey({ source, start });
      const original = sentPages?.get(key);
      if (original && chars.slice(start, original.end).join("") === original.text) {
        items.push({ status: "already_read", text_id: original.text_id,
          next_cursor: original.end < chars.length ? next(original.end) : null });
        returnedPage = true;
        continue;
      }
      if (room <= 0) {
        items.push({ status: "deferred", source, next_cursor: next(start), reason: "CONTEXT_BUDGET" });
        continue;
      }
      const end = Math.min(start + textPage, start + room, chars.length);
      const block = { status: "text", source, text_id: nextTextId(), text: chars.slice(start, end).join(""),
        start, end, total_chars: chars.length, next_cursor: end < chars.length ? next(end) : null,
        ocr_pages: mode === "ocr" ? segments.filter(span => span.start < end && span.end > start)
          .map(({ page, start, end }) => ({ page, start, end })) : [] };
      items.push(block);
      sentPages?.set(key, block);
      room -= end - start;
      returnedPage = true;
    }
    return returnedPage || items.every(item => item.status === "missing") ? ok({ items }) : oversized();
  }

  function searchLecture(args) {
    const { cursor, modes, scope, course_ids } = args;
    const keywords = args.keywords.map(word => word.trim());
    if (keywords.some(word => !word) || !unique(keywords.map(asciiLower))) return invalid("keywords 必须非空且互异。");
    const key = JSON.stringify([keywords, modes, scope, course_ids]);
    const position = cursor === null ? ok({ kind: "groups", index: 0 }) : take(cursor, "search_lecture", key);
    if (!position.ok) return position;
    if (scope === "courses") {
      const ids = new Set(reader.courses().map(course => course.course_id));
      if (course_ids.some(id => !ids.has(id))) return missing("搜索范围含未知课程 ID。");
    }
    const { kind, index } = position.data;
    if (!queryCache.has(key)) queryCache.set(key, reader.candidates({ keywords: keywords.map(asciiLower), modes, course_ids }));
    const candidates = queryCache.get(key);
    const makeGroup = (row, from) => {
      const { lecture_id: id, mode } = row;
      const { source, segments } = getSource(id, mode);
      const { counts, hits } = searchText(segments, keywords);
      const next = from + 5;
      const matches = hits.slice(from, next).map(hit => {
        const { start, ...match } = snippet(hit, keywords);
        return { ...match, read_cursor: readCursor(id, mode, start) };
      });
      return { source, course_title: row.course_title, lecture_title: row.lecture_title,
        hit_count: hits.length, keyword_counts: counts, hits: matches,
        next_hit_cursor: next < hits.length ? issue("search_lecture", key, { kind: "hits", index: next,
          lecture_id: row.lecture_id, mode: row.mode }) : null };
    };
    if (kind === "hits") {
      const row = candidates.find(item => item.lecture_id === position.data.lecture_id && item.mode === position.data.mode);
      return ok({ kind, item: makeGroup(row, index) });
    }
    const next = index + 10;
    const items = candidates.slice(index, next).map(row => makeGroup(row, 0));
    return ok({ kind, items,
      next_cursor: next < candidates.length ? issue("search_lecture", key, { kind, index: next }) : null });
  }

  const actions = {
    read_course_list: args => readList(args),
    search_course: args => readList(args, args.query),
    read_course: readCourse,
    read_lecture: readLecture,
    search_lecture: searchLecture,
  };
  return {
    canonical(name, args) {
      const normalized = { ...args };
      if (name === "search_course") normalized.query = asciiLower(args.query.trim());
      if (name === "search_lecture") normalized.keywords = args.keywords.map(word => word.trim());
      if (name === "search_course" || name === "read_course_list") {
        normalized.page ??= 0;
        normalized.page_max ??= 30;
      }
      return JSON.stringify([name, normalized]);
    },
    run(name, args, room = textPage * 4, sentPages) {
      if (!reader) return failure("DATA_UNAVAILABLE", "课程资料尚未载入。", "retry");
      return actions[name](args, room, sentPages);
    },
  };
}

export function shorten(text, max) {
  return countChars(text) <= max ? text : Array.from(text).slice(0, max).join("");
}
