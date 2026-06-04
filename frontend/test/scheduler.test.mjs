/** Behavioral tests for the focus-aware download scheduler (scheduler.js). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadModule, tick } from "./helpers.mjs";

function freshScheduler() {
  return loadModule("scheduler.js").ICS.scheduler;
}

// A controllable task: starts when dispatched, resolves on finish(), and
// rejects with an AbortError if its signal fires (mimicking aborted fetch).
function makeTask(label, log) {
  let resolveFn;
  const h = { label, started: false, settled: false, aborted: false };
  h.run = (signal) =>
    new Promise((res, rej) => {
      h.started = true;
      log.push("start:" + label);
      resolveFn = (v) => { h.settled = true; res(v); };
      if (signal) {
        signal.addEventListener("abort", () => {
          h.aborted = true;
          log.push("abort:" + label);
          const e = new Error("aborted");
          e.name = "AbortError";
          rej(e);
        });
      }
    });
  h.finish = (v) => { log.push("finish:" + label); resolveFn(v); };
  return h;
}

// enqueue() returns a promise that rejects on reset(); swallow to avoid noise.
const enq = (S, o) => S.enqueue(o).catch(() => {});

test("bounds concurrency at 6 and refills as tasks finish", async () => {
  const S = freshScheduler();
  const log = [];
  const ts = [];
  for (let i = 0; i < 10; i++) {
    const t = makeTask("A" + i, log);
    ts.push(t);
    enq(S, { key: "k" + i, group: "g", priority: 0, run: t.run });
  }
  await tick();
  assert.equal(ts.filter((t) => t.started && !t.settled).length, 6, "6 in flight");
  ts[0].finish("x");
  await tick();
  await tick();
  assert.equal(ts.filter((t) => t.started).length, 7, "7th dispatched after one finishes");
});

test("de-duplicates by key: same key returns same promise, runs once", async () => {
  const S = freshScheduler();
  const log = [];
  const t = makeTask("D", log);
  let runs = 0;
  const run = (s) => { runs++; return t.run(s); };
  const p1 = S.enqueue({ key: "same", group: "g", priority: 0, run });
  const p2 = S.enqueue({ key: "same", group: "g", priority: 0, run });
  p1.catch(() => {});
  p2.catch(() => {});
  await tick();
  assert.equal(p1, p2, "same promise");
  assert.equal(runs, 1, "run() invoked once");
});

test("dispatches higher priority before lower", async () => {
  const S = freshScheduler();
  const log = [];
  const bl = [];
  for (let i = 0; i < 6; i++) {
    const t = makeTask("BL" + i, log);
    bl.push(t);
    enq(S, { key: "bl" + i, group: "g", priority: 0, run: t.run });
  }
  const lo = makeTask("LO", log);
  enq(S, { key: "lo", group: "g", priority: 1, run: lo.run });
  const hi = makeTask("HI", log);
  enq(S, { key: "hi", group: "g", priority: 100, run: hi.run });
  await tick();
  bl[0].finish("x");
  await tick();
  await tick();
  assert.ok(hi.started && !lo.started, "high priority went first");
});

test("focus(group) aborts in-flight other-group tasks and starts the focused group", async () => {
  const S = freshScheduler();
  const log = [];
  const a = [];
  const b = [];
  for (let i = 0; i < 6; i++) {
    const t = makeTask("A" + i, log);
    a.push(t);
    enq(S, { key: "a" + i, group: "A", priority: 0, run: t.run });
  }
  for (let i = 0; i < 3; i++) {
    const t = makeTask("B" + i, log);
    b.push(t);
    enq(S, { key: "b" + i, group: "B", priority: 0, run: t.run });
  }
  await tick();
  assert.ok(a.every((t) => t.started), "all A in flight");
  assert.ok(b.every((t) => !t.started), "B queued");
  S.focus("B");
  await tick();
  await tick();
  assert.ok(a.every((t) => t.aborted), "A aborted on focus(B)");
  assert.ok(b.every((t) => t.started), "B started");
});

test("aborted other-group tasks are re-queued and resume after focused group drains", async () => {
  const S = freshScheduler();
  const log = [];
  const a = [];
  const b = [];
  for (let i = 0; i < 6; i++) {
    const t = makeTask("A" + i, log);
    a.push(t);
    enq(S, { key: "a" + i, group: "A", priority: 0, run: t.run });
  }
  for (let i = 0; i < 3; i++) {
    const t = makeTask("B" + i, log);
    b.push(t);
    enq(S, { key: "b" + i, group: "B", priority: 0, run: t.run });
  }
  await tick();
  S.focus("B");
  await tick();
  b.forEach((t) => t.finish("b"));
  await tick();
  await tick();
  await tick();
  assert.equal(a.filter((t) => t.started).length, 6, "re-queued A tasks resumed");
});

test("focus holds slots for the focused group, then resumes others in background", async () => {
  const S = freshScheduler();
  const log = [];
  const f = makeTask("F", log);
  S.focus("FG");
  enq(S, { key: "f", group: "FG", priority: 0, run: f.run });
  const o = [];
  for (let i = 0; i < 5; i++) {
    const t = makeTask("O" + i, log);
    o.push(t);
    enq(S, { key: "o" + i, group: "OG", priority: 0, run: t.run });
  }
  await tick();
  await tick();
  assert.ok(f.started, "focused task started");
  assert.ok(o.every((t) => !t.started), "other group held while focus in flight");
  f.finish("done");
  await tick();
  await tick();
  assert.equal(o.filter((t) => t.started).length, 5, "others resume after focus drains");
});

test("blur() releases focus so all queued groups run", async () => {
  const S = freshScheduler();
  const log = [];
  const slow = makeTask("S", log);
  S.focus("FG");
  enq(S, { key: "s", group: "FG", priority: 0, run: slow.run });
  const o = makeTask("O", log);
  enq(S, { key: "o", group: "OG", priority: 0, run: o.run });
  await tick();
  assert.ok(!o.started, "other group held under focus");
  S.blur();
  await tick();
  assert.ok(o.started, "other group runs after blur");
});
