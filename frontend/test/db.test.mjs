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

test("getLoadProgress counts pending courses and lectures, decrementing as shards load", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX); // c1: s1,s2 ; c2: s3
  let p = db.getLoadProgress();
  assert.deepEqual(p, { coursesPending: 2, lecturesPending: 3 });
  await db.loadLectureContent("s1", async () => ({ summary: "x" }));
  assert.deepEqual(db.getLoadProgress(), { coursesPending: 2, lecturesPending: 2 });
  await db.loadLectureContent("s2", async () => ({ summary: "x" }));
  // c1 fully loaded now → only c2 pending
  assert.deepEqual(db.getLoadProgress(), { coursesPending: 1, lecturesPending: 1 });
  await db.loadLectureContent("s3", async () => ({ summary: "x" }));
  assert.deepEqual(db.getLoadProgress(), { coursesPending: 0, lecturesPending: 0 });
});

test("searchSummaries matches loaded summary text and reports hit field", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  await db.loadLectureContent("s1", async () => ({ transcript: "讲解栈和队列", summary: "本节介绍绪论", summary_model: "m" }));
  const byTitle = db.searchSummaries("线性表");
  assert.equal(byTitle[0].hit_field, "sub_title");
  const bySummary = db.searchSummaries("绪论");
  assert.ok(bySummary.some((r) => r.hit_field === "summary"));
  const byTranscript = db.searchSummaries("队列");
  assert.ok(byTranscript.some((r) => r.hit_field === "transcript"));
});

test("searchSummaries matches loaded PPT/OCR text with hit_field 'ocr'", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  // PPT pages are keyed by course_id and contain per-sub_id page text.
  await db.loadPptPages("c1", async () => [
    { sub_id: "s2", page_num: 1, text: "幻灯片提到了傅里叶变换", created_sec: 0 },
  ]);
  const hits = db.searchSummaries("傅里叶");
  const ocr = hits.find((r) => r.sub_id === "s2");
  assert.ok(ocr, "found the lecture via PPT text");
  assert.equal(ocr.hit_field, "ocr");
  assert.equal(ocr.ppt_text, "幻灯片提到了傅里叶变换");
});

test("searchSummaries restricts results to the given course filter", async () => {
  const db = freshDb();
  db.initFromIndex(SAMPLE_INDEX);
  // "进程" only appears in c2/s3's title; "绪论" only in c1/s1's title.
  assert.equal(db.searchSummaries("进程").length, 1, "no filter: matches c2");
  assert.equal(db.searchSummaries("进程", ["c1"]).length, 0, "filtered to c1: excluded");
  assert.equal(db.searchSummaries("进程", ["c2"]).length, 1, "filtered to c2: included");
  // Empty filter array behaves like no filter.
  assert.equal(db.searchSummaries("绪论", []).length, 1, "empty filter == all courses");
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
