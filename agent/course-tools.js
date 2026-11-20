import { asciiLower, countChars, failure, invalid, ok } from "./contract.js";

const textPage = 20000;
const encoder = new TextEncoder();
const missing = message => failure("NOT_FOUND", message, "fix_arguments");
const oversized = () => failure("RESULT_TOO_LARGE", "本轮留给原文的空间不足。请拆分读取。", "fix_arguments");
const unique = values => new Set(values).size === values.length;

async function hash(value) {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(value));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

function pageOf(items, snapshot, page, size) {
  const start = page * size;
  return { snapshot, items: items.slice(start, start + size), total: items.length,
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
  return { keyword: keywords[word], match_start: start + left, match_end: start + right,
    snippet: chars.slice(from, to).join(""), snippet_start: start + from,
    snippet_end: start + to, ocr_page: page };
}

export function createCourseTools(reader, snapshot, cursors, dataset = snapshot) {
  const queryCache = new Map();
  const sourceCache = new Map();
  const cursorIds = new Map(Object.entries(cursors).map(([key, value]) => [JSON.stringify(value), key]));

  function issue(tool, key, state) {
    const info = { tool, key, snapshot, state };
    const serialized = JSON.stringify(info);
    if (!cursorIds.has(serialized)) {
      const id = crypto.randomUUID();
      cursors[id] = info;
      cursorIds.set(serialized, id);
    }
    return cursorIds.get(serialized);
  }

  function take(id, tool, key = null) {
    const value = Object.hasOwn(cursors, id) ? cursors[id] : null;
    if (!value || value.tool !== tool || (key !== null && key !== value.key)) {
      return failure("INVALID_CURSOR", "请使用同一工具和查询参数返回的游标。", "restart");
    }
    if (value.snapshot !== snapshot) return failure("STALE_CURSOR", "课程数据已更新，请重新查询。", "restart");
    return ok(value.state);
  }

  function getSource(lectureId, mode) {
    const key = JSON.stringify([lectureId, mode]);
    if (!sourceCache.has(key)) sourceCache.set(key, loadSource(lectureId, mode));
    return sourceCache.get(key);
  }

  async function loadSource(lectureId, mode) {
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
    const version = await hash(mode === "ocr" ? JSON.stringify(ocr) : text);
    return { source: { course_id: row.course_id, lecture_id: lectureId, mode, source_version: version },
      segments, chars: segments.length === 1 ? segments[0].chars : Array.from(text) };
  }

  function readList(args, query = null) {
    let rows = reader.courses();
    if (query !== null) {
      const term = asciiLower(query.trim());
      rows = rows.filter(row => [row.title, row.teacher].some(value => asciiLower(value).includes(term)));
    }
    return ok(pageOf(rows, snapshot, args.page ?? 0, args.page_max ?? 30));
  }

  function readCourse({ course_id, cursor }) {
    const position = cursor === null ? ok(0) : take(cursor, "read_course", course_id);
    if (!position.ok) return position;
    const course = reader.courses().find(row => row.course_id === course_id);
    if (!course) return missing("找不到该课程。请先查询课程 ID。");
    const rows = reader.menu(course_id);
    const end = position.data + 100;
    return ok({ snapshot, course, items: rows.slice(position.data, end), total: rows.length,
      next_cursor: end < rows.length ? issue("read_course", course_id, end) : null });
  }

  async function readLecture(args, room) {
    const { lecture_ids, mode, offset, source_version, cursor } = args;
    const continuation = cursor === null ? null : take(cursor, "read_lecture");
    if (continuation && !continuation.ok) return continuation;
    const targets = continuation ? [continuation.data] : lecture_ids.map(id => ({
      id, mode: mode ?? "summary", start: offset ?? 0, version: source_version,
    }));
    const items = [];
    let returnedText = false;
    for (const { id, mode, start, version } of targets) {
      const result = await getSource(id, mode);
      if (result.reason) {
        if (version !== null && result.reason !== "LECTURE_NOT_FOUND") {
          return failure("SOURCE_CHANGED", "该版本的原文已不可读取，请重新定位。", "restart");
        }
        items.push({ status: "missing", lecture_id: id, mode, reason: result.reason });
        continue;
      }
      const { chars, source, segments } = result;
      if (version !== null && version !== source.source_version) {
        return failure("SOURCE_CHANGED", "原文已更新，请重新定位。", "restart");
      }
      if (start >= chars.length) return invalid("offset 必须小于原文总长度。");
      const next = position => issue("read_lecture", "", { id, mode, start: position, version: source.source_version });
      if (room <= 0) {
        items.push({ status: "deferred", source, next_offset: start,
          next_cursor: next(start), reason: "CONTEXT_BUDGET" });
        continue;
      }
      const end = Math.min(start + textPage, start + room, chars.length);
      const nextOffset = end < chars.length ? end : null;
      const text_id = JSON.stringify([dataset, id, mode, source.source_version, start, end]);
      items.push({ status: "text", source, text_id, text: chars.slice(start, end).join(""),
        start, end, total_chars: chars.length, next_offset: nextOffset, next_cursor: nextOffset === null ? null : next(nextOffset),
        ocr_pages: mode === "ocr" ? segments.filter(span => span.start < end && span.end > start)
          .map(({ page, start, end }) => ({ page, start, end })) : [] });
      room -= end - start;
      returnedText = true;
    }
    return returnedText || items.every(item => item.status === "missing") ? ok({ snapshot, items }) : oversized();
  }

  async function searchLecture(args) {
    const { cursor, modes, scope, course_ids } = args;
    const keywords = args.keywords.map(word => word.trim());
    if (keywords.some(word => !word) || !unique(keywords.map(asciiLower))) return invalid("keywords 必须非空且互异。");
    // Keep the key used by older OR cursors saved in chat history.
    const key = JSON.stringify([keywords, "or", modes, scope, course_ids]);
    const position = cursor === null ? ok({ kind: "groups", index: 0 }) : take(cursor, "search_lecture", key);
    if (!position.ok) return position;
    if (scope === "courses") {
      const ids = new Set(reader.courses().map(course => course.course_id));
      if (course_ids.some(id => !ids.has(id))) return missing("搜索范围含未知课程 ID。");
    }
    const { kind, index } = position.data;
    if (!queryCache.has(key)) queryCache.set(key, reader.candidates({ keywords: keywords.map(asciiLower), modes, course_ids }));
    const candidates = queryCache.get(key);
    const makeGroup = async (row, from) => {
      const { source, segments } = await getSource(row.lecture_id, row.mode);
      const { counts, hits } = searchText(segments, keywords);
      const next = from + 5;
      return { source, course_title: row.course_title, lecture_title: row.lecture_title,
        hit_count: hits.length, keyword_counts: counts, hits: hits.slice(from, next).map(hit => snippet(hit, keywords)),
        next_hit_cursor: next < hits.length ? issue("search_lecture", key, { kind: "hits", index: next,
          lecture_id: row.lecture_id, mode: row.mode }) : null };
    };
    if (kind === "hits") {
      const row = candidates.find(item => item.lecture_id === position.data.lecture_id && item.mode === position.data.mode);
      return ok({ kind, snapshot, item: await makeGroup(row, index) });
    }
    const next = index + 10;
    const items = await Promise.all(candidates.slice(index, next).map(row => makeGroup(row, 0)));
    return ok({ kind, snapshot, items,
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
    dataset: snapshot,
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
    run(name, args, room = textPage * 4) {
      if (!reader) return failure("DATA_UNAVAILABLE", "课程资料尚未载入。", "retry");
      return actions[name](args, room);
    },
  };
}

export function shorten(text, max) {
  return countChars(text) <= max ? text : Array.from(text).slice(0, max).join("");
}
