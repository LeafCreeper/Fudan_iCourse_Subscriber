import { questionPrompt, compressionPrompt } from "./prompts.js";

export const appMessage = (kind, data) => ({ role: "user", content: JSON.stringify({ kind, data }) });
export const makeInput = messages => [{ role: "system", content: questionPrompt }, ...messages];
export function compressionInput(messages, blocks) {
  const candidates = Array.from(blocks.values(), block =>
    [block.text_id, block.source.lecture_id, block.source.mode, block.start, block.end]);
  return [...messages, appMessage("compression_instruction", { instructions: compressionPrompt, candidates })];
}
export const newSession = () => ({
  id: crypto.randomUUID(), title: "新对话", updatedAt: Date.now(),
  visible: [], messages: [], seenPages: {}, cursors: { snapshots: [], pages: [] },
  nextTextId: 0, compacted: 0, compressions: [], run: null,
});

export function inputSize(messages, tools) {
  const text = JSON.stringify({ messages: makeInput(messages), tools });
  let chars = 0;
  let estimate = 0;
  for (const char of text) {
    chars++;
    estimate += char.charCodeAt(0) < 128 ? 0.32 : 1.5;
  }
  return { chars, tokens: Math.ceil(estimate) };
}

export const outputTokens = 8192;

export function usageRatio(size, config) {
  const tokenRatio = config.contextTokens > 0 ? (size.tokens + outputTokens) / config.contextTokens : 0;
  return Math.max(size.chars / config.contextChars, tokenRatio);
}

export function contextRoom(size, config) {
  const room = config.contextChars - size.chars;
  return config.contextTokens > 0
    ? Math.min(room, Math.floor((config.contextTokens - size.tokens - outputTokens) / 1.5))
    : room;
}

const pageKey = (dataset, kind, id) => JSON.stringify([dataset, kind, id]);

function appendPart(session, dataset, kind, id, data) {
  const key = pageKey(dataset, kind, id);
  if (session.seenPages[key]) return;
  session.messages.push(appMessage(kind, data));
  session.seenPages[key] = true;
}

const menuContents = result => ({ course_id: result.course.course_id, items: result.items,
  total: result.total, next_cursor: result.next_cursor });

export function rememberResult(session, dataset, name, result, args) {
  const data = result.data;
  if (name === "read_lecture") {
    for (const block of data.items) {
      if (block.status === "text" && block.source.mode === "summary" && block.start === 0) {
        session.seenPages[pageKey(dataset, "page_text", block.source.lecture_id)] = true;
      }
    }
  } else if (name === "read_course") {
    const id = data.course.course_id;
    session.seenPages[pageKey(dataset, "course", id)] = true;
    if (args.cursor === null) {
      session.seenPages[pageKey(dataset, "lecture_menu", id)] = true;
    }
  } else if (name === "read_course_list" || name === "search_course") {
    for (const course of data.items) {
      session.seenPages[pageKey(dataset, "course", course.course_id)] = true;
    }
  }
}

export function appendPage(session, dataset, page, reader, tools, textRoom = 20000) {
  if (!page || !reader) return null;
  if (page.view === "courses") {
    const courses = new Map(reader.courses().map(row => [row.course_id, row]));
    appendPart(session, dataset, "course_list_order", page.courseIds, page.courseIds);
    for (const id of page.courseIds) {
      if (courses.has(id)) appendPart(session, dataset, "course", id, courses.get(id));
    }
    return { kind: "course_list" };
  }
  if (page.view === "lectures") {
    const result = tools.run("read_course", { course_id: page.courseId, cursor: null });
    if (!result.ok) return null;
    appendPart(session, dataset, "course", page.courseId, result.data.course);
    appendPart(session, dataset, "lecture_menu", page.courseId, menuContents(result.data));
    return { kind: "course", course_id: page.courseId };
  }
  const row = reader.lecture(page.lectureId);
  if (!row) return null;
  const course = reader.courses().find(item => item.course_id === row.course_id);
  if (course) appendPart(session, dataset, "course", row.course_id, course);
  const details = { course_id: row.course_id, lecture_id: row.sub_id, title: row.sub_title, state: row.state };
  appendPart(session, dataset, "lecture", row.sub_id, details);
  const key = pageKey(dataset, "page_text", row.sub_id);
  if (row.summary && textRoom > 0 && !session.seenPages[key]) {
    const response = tools.run("read_lecture", {
      lecture_ids: [row.sub_id], mode: "summary", cursor: null,
    }, textRoom);
    session.messages.push(appMessage("page_text", response.data.items[0]));
    session.seenPages[key] = true;
  }
  return { kind: "lecture", course_id: row.course_id, lecture_id: row.sub_id };
}

export function textBlocks(messages) {
  const items = new Map();
  function record(block) { items.set(block.text_id, block); }
  for (const message of messages) {
    if (message.role === "tool") {
      const result = JSON.parse(message.content);
      if (result.ok && Array.isArray(result.data.items)) {
        for (const block of result.data.items) if (block.status === "text") record(block);
      }
    } else if (message.role === "user") {
      const { kind, data } = JSON.parse(message.content);
      if (kind === "page_text") record(data);
      if (kind === "compacted_context") for (const block of data.texts) record(block);
    }
  }
  return items;
}

export function compactedMessages(messages, summary, texts) {
  const kept = [appMessage("compacted_context", { summary, texts })];
  for (const message of messages) {
    if (message.role === "user") {
      if (["question", "prior_tool_calls"].includes(JSON.parse(message.content).kind)) kept.push(message);
    } else if (message.tool_calls) {
      kept.push(appMessage("prior_tool_calls", message.tool_calls));
    }
  }
  return kept;
}

function shortCount(value) {
  if (value >= 1000000) return String(Math.round(value / 10000) / 100) + "M";
  if (value < 1000) return String(value);
  return String(Math.round(value / 1000)) + "k";
}

export function contextStatus(session, tools, config) {
  const counts = { summary: 0, transcript: 0, ocr: 0 };
  for (const item of textBlocks(session.messages).values()) counts[item.source.mode]++;
  const size = inputSize(session.messages, tools);
  return {
    line: "context " + shortCount(size.chars) + "/" + shortCount(config.contextChars) +
      "; kept sum " + counts.summary + " trsc " + counts.transcript + " ocr " + counts.ocr +
      "; compacted " + session.compacted,
    percent: Math.min(100, Math.floor(usageRatio(size, config) * 100)),
  };
}
