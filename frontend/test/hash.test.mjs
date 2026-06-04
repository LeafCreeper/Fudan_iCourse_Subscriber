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
  assert.equal(r.hashFor("lectures", "c1"), "#/lectures/c1");
  assert.equal(r.hashFor("detail", null, "s9"), "#/detail/s9");
  assert.equal(r.hashFor("search"), "#/search");
  assert.equal(r.hashFor("subscriptions"), "#/subscriptions");
  assert.equal(r.hashFor("settings"), "#/settings");
});

test("hashFor falls back to courses when an id is missing", () => {
  const r = fresh();
  assert.equal(r.hashFor("lectures"), "#/courses");
  assert.equal(r.hashFor("detail"), "#/courses");
});

test("hashFor returns empty for non-routable views", () => {
  const r = fresh();
  assert.equal(r.hashFor("setup"), "");
  assert.equal(r.hashFor("loading"), "");
  assert.equal(r.hashFor("error"), "");
});

test("parseHash decodes each form", () => {
  const r = fresh();
  assert.deepEqual(r.parseHash("#/courses"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/lectures/c1"), { view: "lectures", courseId: "c1" });
  assert.deepEqual(r.parseHash("#/detail/s9"), { view: "detail", subId: "s9" });
  assert.deepEqual(r.parseHash("#/search"), { view: "search" });
  assert.deepEqual(r.parseHash("#/subscriptions"), { view: "subscriptions" });
  assert.deepEqual(r.parseHash("#/settings"), { view: "settings" });
});

test("parseHash treats empty/unknown/garbage as courses", () => {
  const r = fresh();
  assert.deepEqual(r.parseHash(""), { view: "courses" });
  assert.deepEqual(r.parseHash("#"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/bogus"), { view: "courses" });
  assert.deepEqual(r.parseHash("#/lectures"), { view: "courses" }, "lectures without id");
  assert.deepEqual(r.parseHash("#/detail"), { view: "courses" }, "detail without id");
});

test("round-trips and handles ids needing URL-encoding", () => {
  const r = fresh();
  for (const [view, cid, sid] of [["lectures", "c 1", null], ["detail", null, "s/9"]]) {
    const parsed = r.parseHash(r.hashFor(view, cid, sid));
    assert.equal(parsed.view, view);
    if (view === "lectures") assert.equal(parsed.courseId, cid);
    if (view === "detail") assert.equal(parsed.subId, sid);
  }
});
