/** Tests for the in-memory data layer (db.js). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadModule } from "./helpers.mjs";

function freshDb() {
  return loadModule("db.js").ICS.db;
}

const SAMPLE_INDEX = {
  courses: [
    { course_id: "c1", title: "数据结构", teacher: "张三", summary_count: 1, total_count: 2, last_updated: "2026-01-01" },
    { course_id: "c2", title: "操作系统", teacher: "李四", summary_count: 0, total_count: 1, last_updated: null },
  ],
  lectures: [
    { sub_id: "s1", course_id: "c1", sub_title: "绪论", date: "2026-01-01", state: "ready" },
    { sub_id: "s2", course_id: "c1", sub_title: "线性表", date: "2026-01-08", state: "waiting" },
    { sub_id: "s3", course_id: "c2", sub_title: "进程", date: "2026-01-02", state: "ready" },
  ],
  meta: { course_ids: "c1,c2" },
};

test("getCourses returns all courses with counts", () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  const courses = db.getCourses();
  assert.equal(courses.length, 2);
  assert.equal(courses[0].course_id, "c1");
  assert.equal(courses[0].summary_count, 1);
});

test("getLectures filters by course_id", () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  assert.equal(db.getLectures("c1").length, 2);
  assert.equal(db.getLectures("c2").length, 1);
  assert.equal(db.getLectures("nope").length, 0);
});

test("lecture content lazy-loads once and is then marked loaded", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  assert.equal(db.isLectureLoaded("s1"), false);
  let calls = 0;
  const fetcher = async () => { calls++; return { transcript: "T", summary: "S", summary_model: "m" }; };
  await db.loadLectureContent("s1", fetcher);
  await db.loadLectureContent("s1", fetcher); // cached
  assert.equal(calls, 1, "fetcher invoked once");
  assert.equal(db.isLectureLoaded("s1"), true);
  assert.equal(db.getLecture("s1").summary, "S");
});

test("searchSummaries matches loaded summary/transcript and reports hit field", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  await db.loadLectureContent("s1", async () => ({ transcript: "讲解栈和队列", summary: "本节介绍绪论", summary_model: "m" }));
  const bySummary = db.searchSummaries("绪论");
  assert.ok(bySummary.results.some((r) => r.hit_field === "summary"));
  const byTranscript = db.searchSummaries("队列");
  assert.ok(byTranscript.results.some((r) => r.hit_field === "transcript"));
  // sub_title is no longer a search domain, so a title-only term misses.
  assert.equal(db.searchSummaries("线性表").results.length, 0);
});

test("searchSummaries matches loaded PPT/OCR text with hit_field 'ocr'", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  // PPT pages are keyed by course_id and contain per-sub_id page text.
  await db.loadPptPages("c1", async () => [
    { sub_id: "s2", page_num: 1, text: "幻灯片提到了傅里叶变换", created_sec: 0 },
  ]);
  const hits = db.searchSummaries("傅里叶");
  const ocr = hits.results.find((r) => r.sub_id === "s2");
  assert.ok(ocr, "found the lecture via PPT text");
  assert.equal(ocr.hit_field, "ocr");
  assert.equal(ocr.ppt_text, "幻灯片提到了傅里叶变换");
});

test("searchSummaries restricts results to the given course filter", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  // Load a shared term into summaries of lectures in different courses.
  await db.loadLectureContent("s1", async () => ({ summary: "算法导论", summary_model: "m" }));
  await db.loadLectureContent("s3", async () => ({ summary: "算法分析", summary_model: "m" }));
  assert.equal(db.searchSummaries("算法").results.length, 2, "no filter: both courses");
  assert.equal(db.searchSummaries("算法", ["c1"]).results.length, 1, "filtered to c1");
  assert.equal(db.searchSummaries("算法", ["c2"]).results.length, 1, "filtered to c2");
  assert.equal(db.searchSummaries("算法", []).results.length, 2, "empty filter == all");
});

test("searchSummaries honors domain toggles", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  await db.loadLectureContent("s1", async () => ({ transcript: "提到向量", summary: "提到矩阵", summary_model: "m" }));
  // term only in summary
  assert.equal(db.searchSummaries("矩阵", [], 1, 50, { summary: true }).results.length, 1);
  assert.equal(db.searchSummaries("矩阵", [], 1, 50, { summary: false, transcript: true, ocr: true }).results.length, 0);
  // term only in transcript
  assert.equal(db.searchSummaries("向量", [], 1, 50, { transcript: true }).results.length, 1);
  assert.equal(db.searchSummaries("向量", [], 1, 50, { transcript: false }).results.length, 0);
});

test("searchSummaries paginates with hasMore", async () => {
  const db = freshDb();
  // 3 lectures all in one course, all matching the same summary term.
  db.initFromIndex({
    courses: [{ course_id: "c1", title: "C", teacher: "T" }],
    lectures: [
      { sub_id: "s1", course_id: "c1", sub_title: "a", state: "ready" },
      { sub_id: "s2", course_id: "c1", sub_title: "b", state: "ready" },
      { sub_id: "s3", course_id: "c1", sub_title: "c", state: "ready" },
    ],
    meta: {},
  });
  for (const id of ["s1", "s2", "s3"]) {
    await db.loadLectureContent(id, async () => ({ summary: "公共关键词", summary_model: "m" }));
  }
  const p1 = db.searchSummaries("公共关键词", [], 1, 2);
  assert.equal(p1.results.length, 2, "page 1 full");
  assert.equal(p1.hasMore, true, "more remain");
  const p2 = db.searchSummaries("公共关键词", [], 2, 2);
  assert.equal(p2.results.length, 1, "page 2 remainder");
  assert.equal(p2.hasMore, false, "no more");
});

test("getMeta reads index meta values", () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  assert.equal(db.getMeta("course_ids"), "c1,c2");
  assert.equal(db.getMeta("missing"), null);
});

test("catalog search filters by term, dept, title, teacher", () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  db.loadCatalog([
    { course_id: "a1", term: "2025秋", title: "微积分", teacher: "王五", dept: "数学" },
    { course_id: "a2", term: "2026春", title: "线性代数", teacher: "赵六", dept: "数学" },
    { course_id: "a3", term: "2026春", title: "物理", teacher: "王五", dept: "物理" },
  ]);
  assert.equal(db.searchAllCourses({ terms: ["2026春"] }, 50).length, 2);
  assert.equal(db.searchAllCourses({ depts: ["物理"] }, 50).length, 1);
  assert.equal(db.searchAllCourses({ title: "代数" }, 50).length, 1);
  assert.equal(db.searchAllCourses({ teacher: "王五" }, 50).length, 2);
  assert.equal(db.countAllCourses({ terms: ["2026春"] }), 2);
});
