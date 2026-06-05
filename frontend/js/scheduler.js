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

// Default cap until the app sets it from the loaded index (setConcurrency).
var _CONCURRENCY = 6;

var _queue = [];            // pending tasks
var _inFlight = new Set();  // running tasks
var _byKey = {};            // key -> task (pending or in-flight; for de-dup)
var _focused = null;        // focused group (course_id) or null

/**
 * Count running tasks belonging to a specific group.
 * @param {string|null} group - The group identifier (course_id)
 * @returns {number} Number of in-flight tasks in the group
 */
function _inflightGroupCount(group) {
  var n = 0;
  _inFlight.forEach(function (t) { if (t.group === group) n++; });
  return n;
}

/**
 * Select the next task to run from the queue.
 * Prioritizes focused group tasks, then picks highest priority within the pool.
 * @returns {Object|null} Task object or null if queue is empty or focused group has in-flight tasks
 */
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

/**
 * Pump: start pending tasks up to the concurrency limit.
 * Called after a task completes or when focus/concurrency changes.
 */
function _pump() {
  while (_inFlight.size < _CONCURRENCY) {
    var task = _pickNext();
    if (!task) break;
    _start(task);
  }
}

/**
 * Start a task: create an AbortController, add to in-flight set, and invoke task.fn with the signal.
 * On resolution: remove from tracking and resolve the promise; on abortion: re-queue the task.
 * @param {Object} task - Task object with fn, key, group, priority, promise, _resolve, _reject
 */
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

/**
 * Enqueue a task or upgrade an existing one if already pending/in-flight.
 * De-duplicates by key: same key returns the existing promise.
 * @param {Object} opts - Task options
 * @param {string} opts.key - Unique identifier for de-duplication
 * @param {string|number|null} opts.group - Group identifier (course_id)
 * @param {number} opts.priority - Priority value (higher runs first)
 * @param {Function} opts.run - Async function receiving (signal) and returning a promise
 * @returns {Promise} Promise that resolves when the task completes
 */
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

/**
 * Set focus to a specific group (course_id).
 * Aborts all in-flight tasks from other groups and reserves free slots for the focused group.
 * @param {string|number|null} group - Group identifier or null to clear focus
 */
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

/**
 * Clear focus, resuming background preloading.
 */
function _blur() { _focus(null); }

/**
 * Set the maximum number of concurrent tasks.
 * @param {number} n - Concurrency limit (clamped to at least 1)
 */
function _setConcurrency(n) {
  _CONCURRENCY = Math.max(1, n | 0);
  _pump();
}

/**
 * Reset the scheduler: discard all pending and in-flight tasks, clear state.
 * All pending promises are rejected with "scheduler reset".
 */
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
  setConcurrency: _setConcurrency,
  reset: _reset,
};
