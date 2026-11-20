import { questionPrompt, compressionPrompt } from "./prompts.js";

const utf8 = new TextEncoder();

export const appMessage = (kind, data) => ({ role: "user", content: JSON.stringify({ kind, data }) });
export const makeInput = messages => [{ role: "system", content: questionPrompt }, ...messages];
export const compressionInput = messages => [...messages, appMessage("compression_instruction", compressionPrompt)];
export const newSession = () => ({
  id: crypto.randomUUID(), title: "新对话", updatedAt: Date.now(),
  visible: [], messages: [], seenParts: {}, seenTexts: {}, cursors: {}, compacted: 0, run: null,
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

function sourceKey(dataset, block) {
  const { lecture_id, mode, source_version } = block.source;
  return JSON.stringify([dataset, lecture_id, mode, source_version]);
}

function storeRange(session, key, start, end) {
  const ranges = [...(session.seenTexts[key] ?? []), [start, end]].sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const range of ranges) {
    const last = merged.at(-1);
    if (last && last[1] >= range[0]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
  }
  session.seenTexts[key] = merged;
}

function unseenRanges(position, end, prior) {
  const remaining = [];
  for (const [left, right] of prior) {
    if (right <= position) continue;
    if (left >= end) break;
    if (left > position) remaining.push([position, left]);
    position = Math.max(position, right);
  }
  if (position < end) remaining.push([position, end]);
  return remaining;
}

async function partKey(dataset, kind, data) {
  const digest = await crypto.subtle.digest("SHA-256", utf8.encode(JSON.stringify([dataset, kind, data])));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

async function markPart(session, dataset, kind, data, append) {
  const key = await partKey(dataset, kind, data);
  if (!session.seenParts[key] && append) session.messages.push(appMessage(kind, data));
  session.seenParts[key] = true;
}

const menuContents = result => ({ course_id: result.course.course_id, items: result.items,
  total: result.total, next_cursor: result.next_cursor });

export async function rememberResult(session, dataset, name, result) {
  if (!result.ok) return;
  const data = result.data;
  if (name === "read_lecture") {
    for (const block of data.items.filter(item => item.status === "text")) {
      storeRange(session, sourceKey(dataset, block), block.start, block.end);
    }
  } else if (name === "read_course") {
    await markPart(session, dataset, "course", data.course, false);
    await markPart(session, dataset, "lecture_menu", menuContents(data), false);
  } else if (name === "read_course_list" || name === "search_course") {
    for (const course of data.items) await markPart(session, dataset, "course", course, false);
  }
}

export async function appendPage(session, dataset, page, reader, tools, textRoom = 20000) {
  if (!page || !reader) return null;
  if (page.view === "courses") {
    const courses = new Map(reader.courses().map(row => [row.course_id, row]));
    await markPart(session, dataset, "course_list_order", page.courseIds, true);
    for (const id of page.courseIds) {
      if (courses.has(id)) await markPart(session, dataset, "course", courses.get(id), true);
    }
    return { kind: "course_list" };
  }
  if (page.view === "lectures") {
    const result = await tools.run("read_course", { course_id: page.courseId, cursor: null });
    if (!result.ok) return null;
    await markPart(session, dataset, "course", result.data.course, true);
    await markPart(session, dataset, "lecture_menu", menuContents(result.data), true);
    return { kind: "course", course_id: page.courseId };
  }
  const row = reader.lecture(page.lectureId);
  if (!row) return null;
  const course = reader.courses().find(item => item.course_id === row.course_id);
  if (course) await markPart(session, dataset, "course", course, true);
  const details = { course_id: row.course_id, lecture_id: row.sub_id, title: row.sub_title, state: row.state };
  await markPart(session, dataset, "lecture", details, true);
  if (row.summary && textRoom > 0) {
    const response = await tools.run("read_lecture", {
      lecture_ids: [row.sub_id], mode: "summary", offset: null, source_version: null, cursor: null,
    }, textRoom);
    const block = response.data.items[0];
    const key = sourceKey(dataset, block);
    const chars = Array.from(block.text);
    for (const [start, end] of unseenRanges(block.start, block.end, session.seenTexts[key] ?? [])) {
      const last = end === block.end;
      const text_id = JSON.stringify([dataset, row.sub_id, "summary", block.source.source_version, start, end]);
      session.messages.push(appMessage("page_text", {
        ...block, text_id, start, end, text: chars.slice(start - block.start, end - block.start).join(""),
        next_offset: last ? block.next_offset : null, next_cursor: last ? block.next_cursor : null,
      }));
      storeRange(session, key, start, end);
    }
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

export function compactedMessages(messages, summary, texts, tools, config) {
  const kept = [];
  const recent = new Set(messages.filter(message => message.role === "assistant" &&
    !message.tool_calls && message.content).slice(-4));
  for (const message of messages) {
    if (message.role === "user") {
      if (["question", "prior_tool_calls"].includes(JSON.parse(message.content).kind)) kept.push(message);
    } else if (message.tool_calls) {
      kept.push(appMessage("prior_tool_calls", message.tool_calls));
    } else if (recent.has(message)) {
      kept.push({ role: "assistant", content: message.content });
    }
  }
  const checkpoint = appMessage("compacted_context", { summary, texts });
  let next = [checkpoint, ...kept];
  let clipped = false;
  while (next.length > 1 && usageRatio(inputSize(next, tools), config) >= 0.95) {
    // Drop older records in batches while preserving the latest end of the chat.
    kept.splice(0, Math.max(1, Math.floor(kept.length / 5)));
    next = [checkpoint, ...kept];
    clipped = true;
  }
  return { messages: next, clipped };
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
