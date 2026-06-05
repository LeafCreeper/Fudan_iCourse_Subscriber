/**
 * URL hash routing helpers — pure encode/decode between the app's
 * navigable position and the `#/...` hash.
 *
 *   #/courses                            → 课程列表
 *   #/course/<courseId>                  → 某门课的课次列表
 *   #/course/<courseId>/<subId>          → 某节课的详情
 *   #/search | #/subscriptions | #/settings
 *
 * 向后兼容（仅解析，不再生成）：
 *   #/lectures/<courseId>                → 解析为 lectures（旧格式）
 *   #/detail/<subId>                     → 解析为 detail，courseId 为 null（旧格式）
 *
 * setup/loading/error 不可路由；detailView 子标签不编码。
 */

window.ICS = window.ICS || {};

var _ROUTABLE_VIEWS = ["courses", "lectures", "detail", "search", "subscriptions", "settings"];

function _hashFor(view, courseId, subId) {
  switch (view) {
    case "lectures":
      return courseId ? "#/course/" + encodeURIComponent(courseId) : "#/courses";
    case "detail":
      return (courseId && subId)
        ? "#/course/" + encodeURIComponent(courseId) + "/" + encodeURIComponent(subId)
        : "#/courses";
    case "search":
    case "subscriptions":
    case "settings":
      return "#/" + view;
    case "courses":
      return "#/courses";
    default:
      return "";
  }
}

function _parseHash(hash) {
  var h = (hash || "").replace(/^#\/?/, "");
  if (!h) return { view: "courses" };
  var parts = h.split("/");
  var head = parts[0];

  // 新格式：#/course/<cid> 或 #/course/<cid>/<sid>
  if (head === "course") {
    const cid = parts[1] ? decodeURIComponent(parts[1]) : null;
    if (!cid) return { view: "courses" };
    const sid = parts[2] ? decodeURIComponent(parts[2]) : null;
    if (sid) return { view: "detail", courseId: cid, subId: sid };
    return { view: "lectures", courseId: cid };
  }

  // 向后兼容旧格式
  if (head === "lectures") {
    const cid = parts[1] ? decodeURIComponent(parts[1]) : null;
    if (!cid) return { view: "courses" };
    return { view: "lectures", courseId: cid };
  }
  if (head === "detail") {
    const sid = parts[1] ? decodeURIComponent(parts[1]) : null;
    if (!sid) return { view: "courses" };
    return { view: "detail", courseId: null, subId: sid };
  }

  if (head === "search" || head === "subscriptions" || head === "settings") {
    return { view: head };
  }
  return { view: "courses" };
}

window.ICS.routing = {
  ROUTABLE_VIEWS: _ROUTABLE_VIEWS,
  hashFor: _hashFor,
  parseHash: _parseHash,
};
