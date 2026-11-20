import { modelTools, parseCall } from "./contract.js";
import { readChatStream } from "./stream.js";

export const presets = {
  custom: { label: "自定义", url: "", model: "", contextTokens: 0 },
  openai: { label: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-5.6-terra", contextTokens: 1050000 },
  deepseek: { label: "DeepSeek", url: "https://api.deepseek.com", model: "deepseek-flash", contextTokens: 1000000 },
  gemini: { label: "Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-3.8-flash", contextTokens: 1048576 },
  modelscope: { label: "ModelScope", url: "https://api-inference.modelscope.cn/v1", model: "deepseek-ai/DeepSeek-V4.1-Flash", contextTokens: 0 },
};

export const defaultConfig = {
  provider: "custom", url: "", model: "", apiKey: "", rememberKey: false,
  contextChars: 1500000, contextTokens: 0,
};

export class ModelRequestError extends Error {
  constructor(message, kind) {
    super(message);
    this.kind = kind;
  }
}

export function modelUrl(base) {
  const url = URL.parse(base.trim());
  if (!url || !["https:", "http:"].includes(url.protocol) || url.username || url.password) {
    throw new ModelRequestError("请输入有效的 HTTP 或 HTTPS API 地址。", "configuration");
  }
  const path = url.pathname.replace(/\/+$/, "");
  url.pathname = path.endsWith("/chat/completions") ? path : path + "/chat/completions";
  return url;
}

function readFailure(error, signal) {
  if (signal?.aborted) throw error;
  throw new ModelRequestError("模型响应中断，请继续检索。", "connection");
}

async function* responseChunks(chunks, signal) {
  try {
    yield* chunks;
  } catch (error) {
    readFailure(error, signal);
  }
}

export async function requestModel(config, tools, messages, signal, maxTokens = 4096, onText = () => {}) {
  const destination = modelUrl(config.url);
  const limitKey = config.provider === "openai" ? "max_completion_tokens" : "max_tokens";
  const reasoning = config.provider === "deepseek" ? { thinking: { type: "disabled" } } :
    config.provider === "openai" && config.model.startsWith("gpt-5.6-") ? { reasoning_effort: "none" } : {};
  const response = await fetch(destination, {
    method: "POST",
    signal,
    redirect: "error",
    headers: {
      "Content-Type": "application/json",
      ...(config.apiKey ? { Authorization: "Bearer " + config.apiKey } : {}),
    },
    body: JSON.stringify({ model: config.model, messages, tools, tool_choice: "auto",
      stream: true, [limitKey]: maxTokens, ...reasoning }),
  }).catch(error => {
    if (signal?.aborted) throw error;
    throw new ModelRequestError("无法访问模型端点。请检查地址、网络和浏览器跨域支持。", "connection");
  });
  if (!response.ok) {
    const body = await response.text();
    if (response.status === 401 || response.status === 403) {
      throw new ModelRequestError("端点拒绝了请求。请检查 API key 和模型权限。", "configuration");
    }
    if (response.status === 413 ||
        (response.status === 400 && /context[_ ]length|context window|maximum context|input.+exceeds/i.test(body))) {
      throw new ModelRequestError("请求超出模型的上下文窗口。", "context");
    }
    throw new ModelRequestError("模型端点返回 HTTP " + response.status + "。请检查配置或稍后重试。", "endpoint");
  }
  if (response.headers.get("content-type")?.includes("text/event-stream")) {
    if (!response.body) throw new ModelRequestError("模型端点没有返回流式消息。", "protocol");
    const chunks = response.body.pipeThrough(new TextDecoderStream());
    return readChatStream(responseChunks(chunks, signal), onText).catch(error => {
      if (error instanceof SyntaxError) throw new ModelRequestError("模型端点返回了无效的流式消息。", "protocol");
      throw error;
    });
  }
  const result = await response.json().catch(error => {
    if (error instanceof SyntaxError) throw new ModelRequestError("端点返回的内容不是有效 JSON。", "protocol");
    readFailure(error, signal);
  });
  const choice = result?.choices?.[0];
  if (!choice?.message) throw new ModelRequestError("端点没有返回有效的模型消息。", "protocol");
  const { content, tool_calls, reasoning_content, refusal, extra_content } = choice.message;
  if (tool_calls != null && !Array.isArray(tool_calls)) {
    throw new ModelRequestError("模型端点返回了无效的工具调用列表。", "protocol");
  }
  const message = { role: "assistant", content: content ?? null };
  if (reasoning_content !== undefined) message.reasoning_content = reasoning_content;
  if (tool_calls?.length) message.tool_calls = tool_calls;
  if (extra_content !== undefined) message.extra_content = extra_content;
  return { message, finish: choice.finish_reason, refusal };
}

// Uses only a synthetic prompt, and tests the round trip, not just ordinary chat.
export async function testConnection(config, spec, signal) {
  const tools = modelTools(spec);
  const messages = [{ role: "user", content: 'Call search_course with query "Ping course" and null for page and page_max. After getting the result, reply pong.' }];
  const reply = await requestModel(config, tools, messages, signal, 2048);
  const call = reply.message.tool_calls?.[0];
  if (reply.message.tool_calls?.length !== 1 || call.function.name !== "search_course" ||
      !parseCall(spec, "search_course", call.function.arguments).ok) {
    throw new ModelRequestError("端点可访问，但模型没有正确完成测试工具调用。", "protocol");
  }
  const result = { ok: true, data: { items: [], total: 0, page: 0, page_max: 30, next_page: null } };
  messages.push(reply.message, { role: "tool", tool_call_id: call.id, content: JSON.stringify(result) });
  const checked = await requestModel(config, tools, messages, signal, 2048);
  if (checked.finish !== "stop" || !checked.message.content?.toLowerCase().includes("pong") || checked.message.tool_calls) {
    throw new ModelRequestError("端点可访问，但模型没有成功接收测试工具的结果。", "protocol");
  }
}
