/** Tests for URL hash routing helpers (routing.js). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./helpers.mjs";

function fresh() {
  return loadModule("routing.js").ICS.routing;
}

test("hashFor encodes each routable view", () => {
  const r = fresh();
  assert.equal(r.hashFor("courses"), "#/courses");
  assert.equal(r.hashFor("lectures", "c1"), "#/course/c1");
  assert.equal(r.hashFor("detail", "c1", "s9"), "#/course/c1/s9");
  assert.equal(r.hashFor("search"), "#/search");
  assert.equal(r.hashFor("subscriptions"), "#/subscriptions");
  assert.equal(r.hashFor("settings"), "#/settings");
});

test("hashFor falls back to courses when required ids are missing", () => {
  const r = fresh();
  assert.equal(r.hashFor("lectures"), "#/courses");
  assert.equal(r.hashFor("detail"), "#/courses");
  assert.equal(r.hashFor("detail", "c1"), "#/courses"); // detail 必须同时有 courseId 和 subId
});

test("hashFor returns empty for non-routable views", () => {
  const r = fresh();
  assert.equal(r.hashFor("setup"), "");
  assert.equal(r.hashFor("loading"), "");
  assert.equal(r.hashFor("error"), "");
});

test("parseHash decodes new-format hashes", () => {
  const r = fresh();
  assert.deepEqual(r.parseHash("#/courses"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/course/c1"), { view: "lectures", courseId: "c1" });
  assert.deepEqual(r.parseHash("#/course/c1/s9"), { view: "detail", courseId: "c1", subId: "s9" });
  assert.deepEqual(r.parseHash("#/search"), { view: "search" });
  assert.deepEqual(r.parseHash("#/subscriptions"), { view: "subscriptions" });
  assert.deepEqual(r.parseHash("#/settings"), { view: "settings" });
});

test("parseHash 向后兼容旧格式", () => {
  const r = fresh();
  assert.deepEqual(r.parseHash("#/lectures/c1"), { view: "lectures", courseId: "c1" });
  assert.deepEqual(r.parseHash("#/detail/s9"), { view: "detail", courseId: null, subId: "s9" });
});

test("parseHash treats empty/unknown/garbage as courses", () => {
  const r = fresh();
  assert.deepEqual(r.parseHash(""), { view: "courses" });
  assert.deepEqual(r.parseHash("#"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/bogus"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/course"), { view: "courses" }, "course 无 id");
  assert.deepEqual(r.parseHash("#/lectures"), { view: "courses" }, "旧格式 lectures 无 id");
  assert.deepEqual(r.parseHash("#/detail"), { view: "courses" }, "旧格式 detail 无 id");
});

test("round-trips：新格式 ids 带特殊字符", () => {
  const r = fresh();
  for (const [view, cid, sid] of [
    ["lectures", "c 1", null],
    ["detail", "c/1", "s/9"],
  ]) {
    const parsed = r.parseHash(r.hashFor(view, cid, sid));
    assert.equal(parsed.view, view);
    if (view === "lectures") assert.equal(parsed.courseId, cid);
    if (view === "detail") { assert.equal(parsed.courseId, cid); assert.equal(parsed.subId, sid); }
  }
});
