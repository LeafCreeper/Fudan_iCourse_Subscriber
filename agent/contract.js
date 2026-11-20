const url = new URL("./tools.schema.json", import.meta.url);
let contract;

export const ok = data => ({ ok: true, data });
export const failure = (code, message, recovery) => ({ ok: false, error: { code, message, recovery } });
export const invalid = message => failure("INVALID_ARGUMENT", message, "fix_arguments");
export const countChars = text => Array.from(text).length;
export const asciiLower = text => text.replace(/[A-Z]/g, char => char.toLowerCase());

export function loadContract() {
  contract ??= fetch(url).then(response => {
    if (!response.ok) throw new Error("无法加载 Agent 工具定义（HTTP " + response.status + "）。");
    return response.json();
  });
  return contract;
}

const types = {
  object: value => value !== null && typeof value === "object" && !Array.isArray(value),
  array: Array.isArray,
  string: value => typeof value === "string",
  integer: Number.isSafeInteger,
  boolean: value => typeof value === "boolean",
  null: value => value === null,
};

function validate(schema, value, defs) {
  if (schema.$ref) return validate(defs[schema.$ref.split("/").at(-1)], value, defs);
  const matches = rule => validate(rule, value, defs);
  if (schema.type && !types[schema.type](value)) return false;
  if (Object.hasOwn(schema, "const") && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  if (schema.anyOf && !schema.anyOf.some(matches)) return false;
  if (schema.oneOf && schema.oneOf.filter(matches).length !== 1) return false;
  if (schema.allOf && !schema.allOf.every(matches)) return false;
  if (schema.if) {
    const branch = matches(schema.if) ? schema.then : schema.else;
    if (branch && !matches(branch)) return false;
  }
  if (schema.properties) {
    if (!schema.required?.every(key => Object.hasOwn(value, key)) && schema.required) return false;
    for (const [key, item] of Object.entries(value)) {
      if (Object.hasOwn(schema.properties, key)) {
        if (!validate(schema.properties[key], item, defs)) return false;
      } else if (schema.additionalProperties === false) {
        return false;
      }
    }
  }
  if (Array.isArray(value)) {
    if (value.length < (schema.minItems ?? 0) || value.length > (schema.maxItems ?? Infinity)) return false;
    if (schema.uniqueItems && new Set(value).size !== value.length) return false;
    if (schema.items && !value.every(item => validate(schema.items, item, defs))) return false;
  }
  if (typeof value === "string") {
    if (schema.pattern && !new RegExp(schema.pattern, "u").test(value)) return false;
    if (schema.minLength !== undefined || schema.maxLength !== undefined) {
      const length = countChars(value);
      if (length < (schema.minLength ?? 0) || length > (schema.maxLength ?? Infinity)) return false;
    }
  }
  if (typeof value === "number" && (value < (schema.minimum ?? -Infinity) || value > (schema.maximum ?? Infinity))) {
    return false;
  }
  return true;
}

export function validateDefinition(spec, name, value) {
  return validate(spec.$defs[name], value, spec.$defs);
}

export function parseCall(spec, name, input) {
  let args;
  try {
    args = JSON.parse(input);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return invalid("工具参数必须是 JSON 对象。");
  }
  const definition = spec.$defs[name + "_input"];
  if (!validate(definition, args, spec.$defs)) {
    const field = Object.keys(definition.properties).find(key =>
      !Object.hasOwn(args ?? {}, key) || !validate(definition.properties[key], args[key], spec.$defs)
    );
    return invalid(field ? "请检查 " + field + "。" : "请检查多余字段或字段间的约束。");
  }
  return ok(Object.fromEntries(Object.keys(definition.properties).map(key => [key, args[key]])));
}

// Most OpenAI-compatible endpoints agree on this subset. The full schema is
// checked locally, including conditions that a provider may not implement.
function project(rule, defs) {
  if (rule.$ref) return project(defs[rule.$ref.split("/").at(-1)], defs);
  const { type, description, enum: values, items, properties, required, additionalProperties, anyOf } = rule;
  const result = {};
  if (type) result.type = type;
  if (description) result.description = description;
  if (values) result.enum = values;
  if (items) result.items = project(items, defs);
  if (properties) result.properties = Object.fromEntries(Object.entries(properties).map(([name, child]) => [name, project(child, defs)]));
  if (required) result.required = required;
  if (additionalProperties !== undefined) result.additionalProperties = additionalProperties;
  if (anyOf) {
    const [value] = anyOf;
    const projected = project(value, defs);
    Object.assign(result, projected, { type: [projected.type, "null"] });
    if (projected.enum) result.enum = [...projected.enum, null];
    if (description) result.description = description;
  }
  return result;
}

export function modelTools(spec) {
  return spec.oneOf.filter(branch => branch.properties.direction.const === "input").map(branch => {
    const name = branch.properties.tool.const;
    const { description, ...schema } = spec.$defs[name + "_input"];
    return { type: "function", function: { name, description, parameters: project(schema, spec.$defs) } };
  });
}
