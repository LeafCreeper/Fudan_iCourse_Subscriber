import { failure, invalid, modelTools, ok, parseCall, validateDefinition } from "./contract.js";
import { createCourseTools } from "./course-tools.js";
import { ModelRequestError, requestModel } from "./model.js";
import {
  appMessage, appendPage, compactedMessages, compressionInput, contextRoom,
  inputSize, makeInput, outputTokens, rememberResult, textBlocks, usageRatio,
} from "./context.js";

const batchLimit = () => failure("BATCH_LIMIT", "本批工具已达到上限，请在下一批调用。", "retry");
const oversized = () => failure("RESULT_TOO_LARGE", "返回内容超出剩余空间，请缩小返回范围。", "fix_arguments");
const unknown = () => failure("UNKNOWN_TOOL", "当前阶段不允许执行这个工具。", "fix_arguments");
const paused = () => failure("BUDGET_PAUSED", "检索已暂停，等用户选择继续。", "resume");
const canceled = () => failure("CANCELLED", "用户停止了检索。", "resume");
const batchResult = (call, value) => ({ role: "tool", tool_call_id: call.id, content: JSON.stringify(value) });
export const dataTools = ["read_course_list", "search_course", "read_course", "read_lecture", "search_lecture"];

function appendNote(session, text) {
  session.visible.push({ role: "progress", text });
}

function verifiedResult(spec, name, result) {
  if (!validateDefinition(spec, name + "_output", result)) {
    throw new Error("Agent tool result does not match its contract: " + name);
  }
  return result;
}

export async function runTurn({
  session, config, spec, reader, dataset, snapshot, page, text, signal, publish, transport = requestModel,
}) {
  const tools = modelTools(spec);
  const courseTools = createCourseTools(reader, snapshot, session.cursors, dataset);
  const available = new Set(dataTools);
  const memo = new Map();
  const measure = messages => inputSize(messages, tools);
  const ratio = messages => usageRatio(measure(messages), config);
  const room = messages => contextRoom(measure(messages), config);
  const callModel = async messages => {
    const reply = await transport(config, tools, makeInput(messages), signal, outputTokens);
    const usage = reply.usage;
    if (usage?.prompt_tokens !== undefined) {
      session.lastUsage = {
        input: usage.prompt_tokens,
        cached: usage.prompt_tokens_details?.cached_tokens ?? usage.prompt_cache_hit_tokens ?? null,
      };
    }
    return reply;
  };
  const notify = () => publish(session);
  const persist = async () => {
    session.updatedAt = Date.now();
    await notify();
  };

  async function compact() {
    const blocks = textBlocks(session.messages);
    const input = compressionInput(session.messages);
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
              value = verifiedResult(spec, "retain_texts", ok({ kept_ids: kept }));
            }
          }
          input.push(batchResult(call, value));
        }
        continue;
      }
      if (finish !== "stop" || !validateDefinition(spec, "CompressionOutput", message.content)) return false;
      const texts = (kept ?? []).map(id => blocks.get(id));
      const next = compactedMessages(session.messages, message.content.trim(), texts, tools, config);
      if (ratio(next.messages) >= 0.98) return false;
      session.messages = next.messages;
      session.compacted++;
      if (next.clipped) appendNote(session, "已开启新的上下文段。较早的完整消息仍可在本地聊天记录中查看。");
      await persist();
      return true;
    }
    return false;
  }

  async function processBatch(message, run) {
    const entries = message.tool_calls.map((call, index) => ({ call, name: call.function.name, index }));
    let dataSlots = 0;
    let remaining = 40 - run.dataCalls;
    let limitReached = false;
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
        if (remaining === 0) {
          entry.result = paused();
          limitReached = true;
        } else {
          remaining--;
          pending.set(entry.key, entry);
        }
      }
    }

    const progress = entries.find(entry => entry.name === "report_progress" && entry.args);
    const roomPerTool = Math.max(0, Math.floor((room([...session.messages, message]) - 1800) /
      (2 * Math.max(1, entries.length))));
    if (pending.size && !signal.aborted) {
      const line = progress?.args.text ?? message.content?.trim().split("\n")[0].slice(0, 160);
      if (line) appendNote(session, line);
      if (progress) progress.result = verifiedResult(spec, "report_progress", ok({ shown: true }));
      if (line) await notify();
    } else if (progress) {
      progress.result = signal.aborted ? canceled() : limitReached ? paused() :
        invalid("进度消息需要至少一个会执行的同批数据调用。");
    }
    const handles = new Map([...pending].map(([key, entry]) => [key, (async () => {
      if (signal.aborted) return canceled();
      const result = verifiedResult(spec, entry.name, await courseTools.run(entry.name, entry.args, roomPerTool));
      if (JSON.stringify(result).length > Math.max(600, roomPerTool * 2)) return oversized();
      if (result.ok) {
        memo.set(key, result);
        run.dataCalls++;
      }
      return result;
    })()]));
    const resolved = await Promise.all(entries.map(async entry => {
      const value = entry.result ?? await handles.get(entry.key);
      if (value.ok) await rememberResult(session, dataset, entry.name, value);
      return { value, entry };
    }));
    session.messages.push(message, ...resolved.map(({ entry, value }) => batchResult(entry.call, value)));
    const key = JSON.stringify(resolved.filter(({ entry }) => entry.name !== "report_progress")
      .map(({ entry, value }) => [entry.key ?? entry.name, value.ok ? "ok" : value.error.code]));
    const stalled = pending.size === 0 || resolved.every(x => !x.value.ok || x.entry.name === "report_progress");
    const repeated = stalled && key === run.lastBatch;
    run.lastBatch = stalled ? key : "";
    if (run.dataCalls >= 12 && !run.warned) {
      run.warned = true;
      session.messages.push(appMessage("tool_budget", "已执行至少 12 次数据调用。请判断还有哪些证据会改变答案。"));
    }
    return { limitReached, repeated };
  }

  function stop(reason, detail) {
    session.run.status = "paused";
    session.run.reason = reason;
    session.run.detail = detail;
  }

  if (text) {
    const pageRoom = Math.max(0, Math.min(20000, Math.floor((room(session.messages) - 8000) / 2)));
    const current = await appendPage(session, dataset, page, reader, courseTools, pageRoom);
    session.messages.push(appMessage("question", { text, current_page: current }));
    if (!session.visible.length) session.title = Array.from(text).slice(0, 36).join("");
    session.visible.push({ role: "user", text });
  } else {
    session.messages.push(appMessage("resume", "用户已选择继续检索。沿用已有证据，从尚未完成的查询继续。"));
  }
  session.run = { status: "running", dataCalls: 0, steps: 0, warned: false, lastBatch: "", reason: "", detail: "" };
  await persist();

  let triedOverflow = false;
  let lastCompactionSize = 0;

  async function nextReply() {
    try {
      return await callModel(session.messages);
    } catch (error) {
      if (!(error instanceof ModelRequestError) || error.kind !== "context" || triedOverflow) throw error;
      triedOverflow = true;
      if (!await compact()) throw error;
      lastCompactionSize = measure(session.messages).chars;
      return callModel(session.messages);
    }
  }
  try {
    while (!signal.aborted && session.run.steps < 48) {
      const run = session.run;
      const size = measure(session.messages);
      const pressure = usageRatio(size, config);
      const grew = size.chars - lastCompactionSize >= Math.min(20000, config.contextChars * 0.08);
      if (pressure >= 0.98 || (pressure >= 0.8 && (lastCompactionSize === 0 || grew))) {
        const compressed = await compact();
        lastCompactionSize = measure(session.messages).chars;
        if (signal.aborted) break;
        if (!compressed && ratio(session.messages) >= 0.98) {
          stop("context", "上下文空间不足。可调大设置里的上下文预算，或开启新对话。");
          break;
        }
      }
      run.steps++;
      const reply = await nextReply();
      if (signal.aborted) break;
      const { message, finish, refusal } = reply;
      if (finish === "length") {
        stop("truncated", "模型输出超出单次长度，已保留当前检索进度。");
        break;
      }
      if (refusal || finish === "content_filter") {
        stop("refused", "模型未能回答这次提问。");
        break;
      }
      if (message.tool_calls?.length) {
        const result = await processBatch(message, run);
        if (result.limitReached) stop("budget", "本段检索调用已用完，可以继续检索。");
        else if (result.repeated) stop("no_progress", "模型重复调用且没有取得新资料，已暂停检索。");
        await persist();
        if (session.run.status !== "running") break;
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
