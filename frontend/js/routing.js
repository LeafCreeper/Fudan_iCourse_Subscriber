/**
 * URL hash routing helpers — pure encode/decode between the app's
 * navigable position and the `#/...` hash. Kept separate from app.js so it
 * loads and unit-tests without a DOM/Alpine environment.
 *
 *   #/courses                      → course list
 *   #/lectures/<courseId>          → a course's lecture list
 *   #/detail/<subId>               → a lecture detail (course inferred)
 *   #/search | #/subscriptions | #/settings
 *
 * setup/loading/error are intentionally not routable; the detailView
 * sub-tab is not encoded (refresh → summary).
 */

window.ICS = window.ICS || {};

var _ROUTABLE_VIEWS = ["courses", "lectures", "detail", "search", "subscriptions", "settings"];

function _hashFor(view, courseId, subId) {
  switch (view) {
    case "lectures": return courseId ? "#/lectures/" + encodeURIComponent(courseId) : "#/courses";
    case "detail":   return subId ? "#/detail/" + encodeURIComponent(subId) : "#/courses";
    case "search":
    case "subscriptions":
    case "settings": return "#/" + view;
    case "courses":  return "#/courses";
    default:         return "";
  }
}

function _parseHash(hash) {
  var h = (hash || "").replace(/^#\/?/, ""); // strip leading "#/" or "#"
  if (!h) return { view: "courses" };
  var parts = h.split("/");
  var head = parts[0];
  var arg = parts[1] ? decodeURIComponent(parts[1]) : null;
  if (head === "lectures" && arg) return { view: "lectures", courseId: arg };
  if (head === "detail" && arg)   return { view: "detail", subId: arg };
  if (head === "search" || head === "subscriptions" || head === "settings") return { view: head };
  return { view: "courses" };
}

window.ICS.routing = {
  ROUTABLE_VIEWS: _ROUTABLE_VIEWS,
  hashFor: _hashFor,
  parseHash: _parseHash,
};
