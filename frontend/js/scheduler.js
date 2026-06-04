/**
 * Focus-aware priority download scheduler.
 *
 * Wraps the lazy data loaders so that:
 *   - At most _CONCURRENCY tasks run at once (bounded parallelism).
 *   - Tasks are tagged with a `group` (course_id) and a numeric `priority`.
 *   - focus(group) dedicates the pipe to one course: in-flight tasks belonging
 *     to other courses are aborted (and re-queued), and no other-course task
 *     starts until the focused course is fully drained — after which background
 *     preloading of the remaining courses resumes automatically.
 *   - Tasks are de-duplicated by `key`, so the same shard is never fetched twice
 *     concurrently (a detail-view click reuses an in-flight preload).
 *
 * Each task's run(signal) receives an AbortSignal; pass it to fetch() so the
 * request can be cancelled the moment focus changes.
 */

window.ICS = window.ICS || {};

var _CONCURRENCY = 6;

var _queue = [];            // pending tasks
var _inFlight = new Set();  // running tasks
var _byKey = {};            // key -> task (pending or in-flight; for de-dup)
var _focused = null;        // focused group (course_id) or null

function _inflightGroupCount(group) {
  var n = 0;
  _inFlight.forEach(function (t) { if (t.group === group) n++; });
  return n;
}

function _pickNext() {
  if (!_queue.length) return null;
  var pool = _queue;
  if (_focused !== null) {
    var focused = _queue.filter(function (t) { return t.group === _focused; });
    if (focused.length) {
      pool = focused;
    } else if (_inflightGroupCount(_focused) > 0) {
      return null; // hold free slots for the focused course until it drains
    }
  }
  var best = pool[0];
  for (var i = 1; i < pool.length; i++) {
    if (pool[i].priority > best.priority) best = pool[i];
  }
  _queue.splice(_queue.indexOf(best), 1);
  return best;
}

function _pump() {
  while (_inFlight.size < _CONCURRENCY) {
    var task = _pickNext();
    if (!task) break;
    _start(task);
  }
}

function _start(task) {
  task.controller = new AbortController();
  task._aborted = false;
  _inFlight.add(task);
  task.fn(task.controller.signal).then(
    function (val) {
      if (task._discarded) return;
      _inFlight.delete(task);
      delete _byKey[task.key];
      task._resolve(val);
      _pump();
    },
    function (err) {
      if (task._discarded) return;
      _inFlight.delete(task);
      if (task._aborted) {
        task._aborted = false;
        task.controller = null;
        _queue.push(task); // re-queue; keep de-dup entry + promise alive
      } else {
        delete _byKey[task.key];
        task._reject(err);
      }
      _pump();
    }
  );
}

function _enqueue(opts) {
  var existing = _byKey[opts.key];
  if (existing) {
    if (opts.priority != null && opts.priority > existing.priority) {
      existing.priority = opts.priority;
      _pump();
    }
    return existing.promise;
  }
  var task = {
    key: opts.key,
    group: opts.group == null ? null : String(opts.group),
    priority: opts.priority || 0,
    fn: opts.run,
    controller: null,
    _aborted: false,
    _discarded: false,
  };
  task.promise = new Promise(function (res, rej) {
    task._resolve = res; task._reject = rej;
  });
  _byKey[opts.key] = task;
  _queue.push(task);
  _pump();
  return task.promise;
}

function _focus(group) {
  group = group == null ? null : String(group);
  if (_focused === group) { _pump(); return; }
  _focused = group;
  if (group !== null) {
    _inFlight.forEach(function (t) {
      if (t.group !== group && t.controller) {
        t._aborted = true;
        try { t.controller.abort(); } catch (e) {}
      }
    });
  }
  _pump();
}

function _blur() { _focus(null); }

function _reset() {
  var all = _queue.slice();
  _inFlight.forEach(function (t) { all.push(t); });
  _queue = [];
  _inFlight = new Set();
  _byKey = {};
  _focused = null;
  all.forEach(function (t) {
    t._discarded = true;
    if (t.controller) { try { t.controller.abort(); } catch (e) {} }
    if (t._reject) t._reject(new Error("scheduler reset"));
  });
}

window.ICS.scheduler = {
  enqueue: _enqueue,
  focus: _focus,
  blur: _blur,
  reset: _reset,
};
