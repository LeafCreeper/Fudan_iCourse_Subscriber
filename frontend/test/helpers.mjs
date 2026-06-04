/**
 * Test helper: load a frontend file (written as browser globals) into a
 * fresh `window` object so its `window.ICS.*` exports can be tested in Node.
 *
 * The frontend JS attaches to `window` and relies on a few globals
 * (AbortController, Promise, Set) that already exist in Node, so we just
 * inject a clean `window` per call to give each test isolated module state.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const JS_DIR = join(__dirname, "..", "js");

export function loadModule(filename) {
  const src = readFileSync(join(JS_DIR, filename), "utf8");
  const window = {};
  new Function("window", src)(window);
  return window;
}

/** Advance the microtask/macrotask queue once. */
export const tick = () => new Promise((r) => setTimeout(r, 0));
