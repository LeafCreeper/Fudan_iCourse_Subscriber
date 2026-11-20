import { failure, invalid, modelTools, ok, parseCall, validateDefinition } from "./contract.js";
import { createCourseTools, readPageKey } from "./course-tools.js";
import { ModelRequestError, requestModel } from "./model.js";
import {
  appMessage, appendPage, compactedMessages, compressionInput, contextRoom,
  inputSize, makeInput, outputTokens, rememberResult, textBlocks, usageRatio,
} from "./context.js";

const batchLimit = () => failure("BATCH_LIMIT", "本批工具已达到上限，请在下一批调用。", "retry");
const unknown = () => failure("UNKNOWN_TOOL", "当前阶段不允许执行这个工具。", "fix_arguments");
const canceled = () => failure("CANCELLED", "用户停止了检索。", "resume");
const batchResult = (call, value) => ({ role: "tool", tool_call_id: call.id, content: JSON.stringify(value) });
export const dataTools = ["read_course_list", "search_course", "read_course", "read_lecture", "search_lecture"];

function appendNote(session, text) {
  session.visible.push({ role: "progress", text });
}

function appendCalls(session, pending) {
  const calls = [...pending.values()].map(({ name, args }) => ({ name, args }));
  const last = session.visible.at(-1);
  if (last?.role === "tools") session.visible[session.visible.length - 1] = { role: "tools", calls: [...last.calls, ...calls] };
  else session.visible.push({ role: "tools", calls });
}

function referenceReadPages(result) {
  return ok({ items: result.data.items.map(block => block.status === "text" ?
    { status: "already_read", text_id: block.text_id, next_cursor: block.next_cursor } : block) });
}

export async function runTurn({
  session, config, spec, reader, dataset, snapshot, page, text, signal, publish, onText = () => {}, transport = requestModel,
}) {
  const tools = modelTools(spec);
  const nextTextId = () => "t" + ++session.nextTextId;
  const courseTools = createCourseTools(reader, snapshot, session.cursors, nextTextId);
  const available = new Set(dataTools);
  const memo = new Map();
  const measure = messages => inputSize(messages, tools);
  const ratio = messages => usageRatio(measure(messages), config);
  // Leave one share for tool metadata and the next model request.
  const textRoom = (count = 1) => Math.max(0, Math.floor(contextRoom(measure(session.messages), config) / (count + 1)));
  const callModel = (messages, streamText) =>
    transport(config, tools, makeInput(messages), signal, outputTokens, streamText);
  const notify = () => publish(session);
  const persist = async () => {
    session.updatedAt = Date.now();
    await notify();
  };

  async function compact() {
    const blocks = textBlocks(session.messages);
    const input = compressionInput(session.messages, blocks);
    const debug = { after_message: session.visible.length, candidate_count: blocks.size, calls: [], kept_ids: null };
    session.compressions.push(debug);
    let kept = null;
    for (let step = 0; step < 4; step++) {
      if (signal.aborted) return false;
      const response = await callModel(input);
      if (signal.aborted) return false;
      const { message, finish } = response;
      if (finish === "length" || response.refusal) return false;
      if (message.tool_calls) {
        input.push(message);
        for (const call of message.tool_calls) {
          let value = unknown();
          if (call.function.name === "retain_texts") {
            const parsed = parseCall(spec, "retain_texts", call.function.arguments);
            if (!parsed.ok) value = parsed;
            else if (kept !== null) value = invalid("已经选定原文，请输出摘要。");
            else if (!parsed.data.ids.every(id => blocks.has(id))) value = invalid("只能选择上下文中已出现的 text_id。");
            else {
              kept = parsed.data.ids;
              value = ok({ kept_ids: kept });
            }
            debug.calls.push({ arguments: call.function.arguments, result: value });
          }
          input.push(batchResult(call, value));
        }
        continue;
      }
      if (finish !== "stop" || !validateDefinition(spec, "CompressionOutput", message.content)) return false;
      const texts = (kept ?? []).map(id => blocks.get(id));
      const next = compactedMessages(session.messages, message.content.trim(), texts);
      if (ratio(next) >= 1) return false;
      session.messages = next;
      sentPages = new Map(texts.map(block => [readPageKey(block), block]));
      session.compacted++;
      debug.kept_ids = kept ?? [];
      await persist();
      return true;
    }
    return false;
  }

  async function processBatch(message) {
    const entries = message.tool_calls.map((call, index) => ({ call, name: call.function.name, index }));
    let dataSlots = 0;
    const pending = new Map();
    let progressSeen = false;
    for (const entry of entries) {
      if (entry.name === "report_progress") {
        entry.result = progressSeen ? batchLimit() :
          entry.index !== 0 ? invalid("进度工具应在同批的数据工具之前。") : null;
        progressSeen = true;
      } else if (++dataSlots > 4) {
        entry.result = batchLimit();
      } else if (!available.has(entry.name)) {
        entry.result = unknown();
      }
      if (entry.result) continue;
      const parsed = parseCall(spec, entry.name, entry.call.function.arguments);
      if (!parsed.ok) {
        entry.result = parsed;
        continue;
      }
      entry.args = parsed.data;
      if (entry.name === "report_progress") continue;
      entry.key = courseTools.canonical(entry.name, entry.args);
      if (memo.has(entry.key)) {
        entry.result = memo.get(entry.key);
      } else if (!pending.has(entry.key)) {
        pending.set(entry.key, entry);
      }
    }

    const progress = entries.find(entry => entry.name === "report_progress" && entry.args);
    const roomPerTool = textRoom(pending.size);
    if (pending.size && !signal.aborted) {
      const line = progress?.args.text ?? message.content?.trim();
      if (line) appendNote(session, line);
      if (progress) progress.result = ok({ shown: true });
      appendCalls(session, pending);
      await notify();
    } else if (progress) {
      progress.result = signal.aborted ? canceled() : invalid("进度消息需要至少一个会执行的同批数据调用。");
    }
    const results = new Map();
    for (const [key, entry] of pending) {
      const value = signal.aborted ? canceled() : courseTools.run(entry.name, entry.args, roomPerTool, sentPages);
      results.set(key, value);
      if (value.ok && entry.name !== "read_lecture") memo.set(key, value);
    }
    const toolResults = entries.map(entry => {
      const value = entry.result ?? results.get(entry.key);
      if (value.ok) rememberResult(session, dataset, entry.name, value, entry.args);
      const duplicate = value.ok && entry.name === "read_lecture" && pending.get(entry.key) !== entry;
      return batchResult(entry.call, duplicate ? referenceReadPages(value) : value);
    });
    session.messages.push(message, ...toolResults);
  }

  function stop(reason, detail) {
    session.run.status = "paused";
    session.run.reason = reason;
    session.run.detail = detail;
  }

  if (text) {
    const current = appendPage(session, dataset, page, reader, courseTools, textRoom());
    session.messages.push(appMessage("question", { text, current_page: current }));
    if (!session.visible.length) session.title = Array.from(text.trim()).slice(0, 36).join("");
    session.visible.push({ role: "user", text });
  } else {
    session.messages.push(appMessage("resume", "用户已选择继续检索。沿用已有证据，从尚未完成的查询继续。"));
  }
  let sentPages = new Map(Array.from(textBlocks(session.messages).values(), block => [readPageKey(block), block]));
  session.run = { status: "running", steps: 0, reason: "", detail: "" };
  await persist();

  let compactionFailed = false;
  let lastCompactionSize = 0;

  async function tryCompact(size) {
    if (compactionFailed || size <= lastCompactionSize) return false;
    const compressed = await compact();
    compactionFailed = !compressed;
    if (compressed) lastCompactionSize = measure(session.messages).chars;
    return compressed;
  }

  async function nextReply() {
    try {
      return await callModel(session.messages, onText);
    } catch (error) {
      if (!(error instanceof ModelRequestError) || error.kind !== "context") throw error;
      if (!await tryCompact(measure(session.messages).chars)) throw error;
      return callModel(session.messages, onText);
    }
  }
  try {
    while (!signal.aborted && session.run.steps < 48) {
      const run = session.run;
      const size = measure(session.messages);
      const pressure = usageRatio(size, config);
      if (pressure >= 0.8) {
        const compressed = await tryCompact(size.chars);
        if (signal.aborted) break;
        if (!compressed && pressure >= 1) {
          stop("context", "上下文空间不足。可调大设置里的上下文预算，或开启新对话。");
          break;
        }
      }
      run.steps++;
      const reply = await nextReply();
      if (signal.aborted) break;
      const { message, finish, refusal } = reply;
      if (!finish || finish === "length") {
        stop("truncated", "模型输出中断或超出单次长度，已保留当前检索进度。");
        break;
      }
      if (refusal || finish === "content_filter") {
        stop("refused", "模型未能回答这次提问。");
        break;
      }
      if (message.tool_calls?.length) {
        onText("");
        await processBatch(message);
        await persist();
        continue;
      }
      if (finish !== "stop" || !message.content?.trim()) {
        stop("empty", "模型没有返回完整回答。");
        break;
      }
      session.messages.push(message);
      session.visible.push({ role: "assistant", text: message.content });
      session.run = null;
      break;
    }
  } catch (error) {
    if (!signal.aborted && !(error instanceof ModelRequestError)) throw error;
    if (!signal.aborted) {
      stop(error.kind, error.message);
    }
  }

  if (session.run?.status === "running") {
    if (signal.aborted) stop("cancelled", "已停止，可继续检索。");
    else stop("budget", "模型请求次数已达到本段上限，可以继续检索。");
  }
  await persist();
}
