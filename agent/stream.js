// Chat Completions splits tool arguments across deltas. The executor only
// receives complete calls; the UI can render the text as it arrives.
export async function readChatStream(chunks, onText) {
  const message = { role: "assistant", content: null };
  const calls = [];
  let finish = null;
  let refusal = "";

  function accept(data) {
    const event = JSON.parse(data);
    if (!event || (event.choices != null && !Array.isArray(event.choices))) {
      throw new SyntaxError("Invalid chat completion event");
    }
    const choice = event.choices?.[0];
    if (!choice) return;
    finish = choice.finish_reason ?? finish;
    const delta = choice.delta ?? {};
    if (delta.content) {
      message.content = (message.content ?? "") + delta.content;
      onText(message.content);
    }
    if (delta.reasoning_content) message.reasoning_content = (message.reasoning_content ?? "") + delta.reasoning_content;
    if (delta.extra_content !== undefined) message.extra_content = delta.extra_content;
    refusal += delta.refusal ?? "";
    const fragments = delta.tool_calls ?? [];
    if (!Array.isArray(fragments)) throw new SyntaxError("Invalid tool call list");
    for (const fragment of fragments) {
      if (!Number.isInteger(fragment?.index) || fragment.index < 0) throw new SyntaxError("Invalid tool call index");
      const item = calls[fragment.index] ??= { type: "function", function: { name: "", arguments: "" } };
      if (fragment.id) item.id = fragment.id;
      if (fragment.type) item.type = fragment.type;
      if (fragment.extra_content !== undefined) item.extra_content = fragment.extra_content;
      item.function.name += fragment.function?.name ?? "";
      item.function.arguments += fragment.function?.arguments ?? "";
    }
  }

  let buffer = "";
  let fields = [];
  for await (const chunk of chunks) {
    buffer += chunk;
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith("data:")) fields.push(line.slice(5).trimStart());
      else if (line === "" && fields.length) {
        const data = fields.join("\n");
        fields = [];
        if (data !== "[DONE]") accept(data);
      }
    }
  }
  if (calls.length) message.tool_calls = calls;
  return { message, finish, refusal };
}
