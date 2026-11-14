"""Shard the SQLite database into encrypted chunks for incremental fetch.

WHY SHARD
=========
Once the DB grows past ~50MB the frontend pays a long re-download tax for
every commit, even if only one course changed.  Sharding by course lets the
frontend hold a content-addressed cache (sha256 → bytes) and re-pull only
the shards whose hash actually changed, which collapses the typical update
to <1MB on the wire.

WHY *PERSISTENT SLOT ASSIGNMENT* (PSA), NOT HASH-MOD
====================================================
An earlier version used ``int(course_id) % num_shards``.  That had two
structural problems that broke the cache-locality goal of sharding itself:

  1. *Volatile and stable courses were spread evenly across all shards*.
     The user's currently-subscribed (volatile) courses and their long
     tail of historical / single-run (stable) courses were interleaved
     by hash.  Every routine run produced new bytes for at least one
     subscribed course, which dirtied at least one course per shard
     on average → roughly every shard changed → cache invalidated.

  2. *Shard count depended on ceil(N_courses / 15)*.  Whenever
     ``N_courses`` crossed a 15-multiple — a new course discovered by
     single-run, or a course removed — ``num_shards`` ticked and
     ``cid % num_shards`` reassigned EVERY course in one go.  A single
     unrelated event triggered a full global reshuffle.

Cache-locality requires three properties simultaneously:

    (a) Volatile courses cluster together so a routine run only dirties
        a small known set of shards.
    (b) Stable courses cluster separately so their shards never change
        after the single event that first wrote them.
    (c) No event — adding a course, growing a bin past its pack size,
        crossing any threshold — ever triggers a global reassignment.

PSA delivers all three by persisting each course's ``(bin, shard_idx)``
slot in the ``meta`` table and reusing it across runs:

    - ``hot`` bin   = courses in the current subscription list.
    - ``cold`` bin  = everything else (single-run residue, unsubscribed
                      historic courses, etc.).
    - Each course's slot is sticky.  A course only moves when its bin
      changes (subscribe / unsubscribe an existing-in-DB course), which
      is rare — by far the most common subscription change is "+1 new
      course" which has never been in the DB and goes straight to hot.
    - New courses go to the lowest non-full shard in their bin; if all
      shards in that bin are full, the lowest unused index opens a
      fresh shard.  Opening a fresh shard does NOT renumber any
      existing one.

The full event matrix:

    +-----------------------------------+-------------------+----------+
    | event                             | shards dirtied    | reshuffle|
    +-----------------------------------+-------------------+----------+
    | routine run: hot course produces  |        1 (hot)    |    no    |
    |     new lecture / new summary     |                   |          |
    | single-run on brand-new course    |        1 (cold)   |    no    |
    | single-run on existing cold course|        1 (cold)   |    no    |
    | subscribe brand-new course        |        1 (hot)    |    no    |
    |     (the common case — course is  |                   |          |
    |     not yet in the DB)            |                   |          |
    | subscribe existing-cold course    |    2 (1H + 1C)    |    no    |
    | unsubscribe (rare)                |    2 (1H + 1C)    |    no    |
    | hot bin needs new shard           |        1 (hot)    |    no    |
    | cold bin needs new shard          |        1 (cold)   |    no    |
    | "global reshuffle"                |        0          |   never  |
    +-----------------------------------+-------------------+----------+

SUBSCRIPTION SOURCE
===================
The sharder needs to know which courses are currently in the subscription
list (= hot bin), without modifying any other module of the codebase.

The strategy:

    1. Env var ``SUBSCRIBED_COURSE_IDS`` — when set, it is authoritative
       and is persisted to ``meta('subscribed_course_ids')``.  This is
       set only in ``check.yml``'s shard step, where the workflow knows
       the full subscription (the GitHub Actions secret).

    2. Otherwise — read the previously persisted value from
       ``meta('subscribed_course_ids')``.  ``single_run.yml``,
       ``delete_course.yml``, and manual reshards take this path; they
       would otherwise be at risk of treating their one-shot
       ``COURSE_IDS`` input as the full subscription and corrupting the
       bin layout.

LAYOUT under ``output_dir``
===========================
    icourse-index.enc                — encrypted JSON index (small,
                                       always re-fetched)
    shards/meta-0000.db.gz.enc       — meta + all_courses catalog
    shards/shard-hot-NNNN.db.gz.enc  — hot bin (≤10 courses each)
    shards/shard-cold-NNNN.db.gz.enc — cold bin (≤30 courses each)

Trust model: every shard and the index are encrypted with the v2 password
(sha256("ICSv2:" + stuid + ":" + uispsw)).  The data branch is public; the
file names and shard count leak, but no row content does.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile

from src.data import crypto_box
from src.data.schema import SCHEMA_SQL as _SCHEMA_SQL

# ── Public layout constants ───────────────────────────────────────────────
INDEX_FILENAME = "icourse-index.enc"
SHARDS_DIR = "shards"
INDEX_VERSION = 4
META_SHARD_NAME = "meta-0000.db.gz.enc"

# Legacy parameters of ``shard_database`` — accepted but ignored.  Kept so
# the CLI in scripts/db_shard.py and existing call sites don't break.
SHARD_TARGET_BYTES = 3 * 1024 * 1024
COMPRESSION_RATIO_GUESS = 4

# ── PSA bin capacities ────────────────────────────────────────────────────
# Different caps per bin reflect their access pattern:
#   - hot is small (a user's active subscription is ~5-30 courses) but each
#     course re-emits bytes weekly.  Keeping HOT_MAX_COURSES tight (10)
#     means a "one course got new data" event re-pulls ~10 courses' worth
#     of bytes, not a packed shard's worth.
#   - cold accumulates over years (every course the user ever single-ran
#     or historically subscribed to).  Each cold course's data is
#     essentially frozen after first appearance.  Larger COLD_MAX_COURSES
#     gives fewer total shards (less manifest overhead) with zero cache
#     cost since cold shards almost never change after writing.
HOT_MAX_COURSES = 10
COLD_MAX_COURSES = 30

# ── meta-table keys (only this module reads / writes these) ───────────────
META_KEY_SUBSCRIBED = "subscribed_course_ids"
META_KEY_PSA = "psa_assignment"


# ── PSA assignment ─────────────────────────────────────────────────────────

def _get_subscribed_set(conn: sqlite3.Connection) -> set[str]:
    """Resolve the current subscription set.

    See module docstring (SUBSCRIPTION SOURCE) for why we read env first
    and persist for downstream workflows.  Empty string is a legitimate
    "user unsubscribed everything" signal and is persisted as such.
    """
    env_value = os.environ.get("SUBSCRIBED_COURSE_IDS")
    if env_value is not None:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (META_KEY_SUBSCRIBED, env_value),
            )
        raw = env_value
    else:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (META_KEY_SUBSCRIBED,),
        ).fetchone()
        raw = row[0] if row else ""
    return {s.strip() for s in raw.split(",") if s.strip()}


def _load_psa_state(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Decode the persisted ``(bin, shard_idx)`` assignment from meta.

    Returns ``{"hot": {cid: idx}, "cold": {cid: idx}}``.  A missing key
    or a malformed JSON blob → fresh empty state, which forces a full
    re-assignment on this run.  That's the same code path as a first
    deploy, so behaviour is well-defined.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_KEY_PSA,),
    ).fetchone()
    if not row or not row[0]:
        return {"hot": {}, "cold": {}}
    try:
        parsed = json.loads(row[0])
        return {
            "hot": {str(k): int(v) for k, v in parsed.get("hot", {}).items()},
            "cold": {str(k): int(v) for k, v in parsed.get("cold", {}).items()},
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"hot": {}, "cold": {}}


def _save_psa_state(
    conn: sqlite3.Connection, state: dict[str, dict[str, int]],
) -> None:
    """Persist new assignment.

    Tight, byte-stable serialization (sorted keys, no spaces) so the
    meta shard's bytes only differ when the assignment actually differs
    — that keeps the meta shard's blob SHA cache-friendly too.
    """
    payload = json.dumps(
        {
            "hot": dict(sorted(state["hot"].items())),
            "cold": dict(sorted(state["cold"].items())),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (META_KEY_PSA, payload),
        )


def _assign_psa(
    conn: sqlite3.Connection, subscribed: set[str],
) -> tuple[dict[str, list[list[str]]], dict[str, dict[str, int]]]:
    """Compute ``(bin, shard_idx)`` for every course in the DB.

    Two passes:

      Pass 1 — preserve each course's previous slot iff its bin is
               unchanged.  This is the whole point of PSA: course X
               lands in the same shard run after run → byte-identical
               encrypted blob → identical Git blob sha → frontend cache
               hit.

      Pass 2 — assign every other course (brand new, OR migrated
               because its bin flipped) to the lowest-numbered shard
               in its bin with capacity remaining.  If every shard
               in the bin is full, the lowest UNUSED index opens a
               fresh shard.  "Lowest unused" (not max+1) means a slot
               vacated by cross-bin migration gets reused before any
               new index is minted — shard indices stay compact even
               after many subscription churns.

    Returns ``(groups, new_assignment)`` where:
      - ``groups[bin][i]`` is the sorted list of course_ids in shard i
        of that bin.  Sparse: empty slots stay as ``[]`` and are
        skipped at shard-emit time.
      - ``new_assignment`` is the canonical PSA state to persist.
    """
    course_ids = sorted(
        str(r[0]) for r in conn.execute(
            "SELECT course_id FROM courses"
        ).fetchall()
    )

    prev = _load_psa_state(conn)
    new_assignment: dict[str, dict[str, int]] = {"hot": {}, "cold": {}}
    counts: dict[tuple[str, int], int] = {}
    pack = {"hot": HOT_MAX_COURSES, "cold": COLD_MAX_COURSES}

    def _bin_of(cid: str) -> str:
        return "hot" if cid in subscribed else "cold"

    # Pass 1: honor prior slots where bin is unchanged.
    for cid in course_ids:
        b = _bin_of(cid)
        prev_idx = prev.get(b, {}).get(cid)
        if prev_idx is not None:
            new_assignment[b][cid] = prev_idx
            counts[(b, prev_idx)] = counts.get((b, prev_idx), 0) + 1

    # Pass 2: assign the rest (new courses, or courses whose bin flipped).
    for cid in course_ids:
        b = _bin_of(cid)
        if cid in new_assignment[b]:
            continue
        target_idx = None
        # Lowest existing shard with capacity wins (deterministic).
        existing_indices = sorted({idx for (bb, idx) in counts if bb == b})
        for idx in existing_indices:
            if counts.get((b, idx), 0) < pack[b]:
                target_idx = idx
                break
        if target_idx is None:
            # Open the lowest unused index, filling holes left by
            # cross-bin migration before minting a brand-new one.
            existing_set = {idx for (bb, idx) in counts if bb == b}
            target_idx = 0
            while target_idx in existing_set:
                target_idx += 1
        new_assignment[b][cid] = target_idx
        counts[(b, target_idx)] = counts.get((b, target_idx), 0) + 1

    # Materialize groups.  Sparse: indices that have no courses (because
    # every course in that slot migrated to the other bin) are kept as
    # empty lists here and dropped at shard-emit time.
    groups: dict[str, list[list[str]]] = {"hot": [], "cold": []}
    for b in ("hot", "cold"):
        if not new_assignment[b]:
            continue
        max_idx = max(new_assignment[b].values())
        groups[b] = [[] for _ in range(max_idx + 1)]
        for cid in sorted(new_assignment[b]):
            groups[b][new_assignment[b][cid]].append(cid)

    return groups, new_assignment


# ── Shard file building ──────────────────────────────────────────────────

def _build_meta_shard(source_db: str, output_path: str) -> None:
    """Build a tiny metadata-only shard containing ``all_courses`` and
    ``meta``.

    ``all_courses`` changes when a new semester is discovered and its
    catalog is fetched.  ``meta`` changes whenever PSA state or the persisted
    subscription set changes — both of which happen at most once per
    run.  Keeping this in a dedicated tiny shard lets the frontend's
    subscription editor know the catalog without decrypting any
    course-data shard.
    """
    if os.path.exists(output_path):
        os.remove(output_path)
    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(output_path)
    try:
        dst.executescript(_SCHEMA_SQL)
        for table in ("all_courses", "meta"):
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            cols = list(rows[0].keys())
            col_str = ", ".join(cols)
            ph_str = ", ".join("?" * len(cols))
            dst.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_str}) "
                f"VALUES ({ph_str})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        dst.commit()
    finally:
        dst.close()
        src.close()


def _build_shard_db(source_db: str, course_ids: list[str],
                    output_path: str) -> None:
    """Materialize a self-contained sqlite shard for the given courses.

    ``all_courses`` and ``meta`` live in the separate meta shard.
    """
    if os.path.exists(output_path):
        os.remove(output_path)

    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(output_path)
    try:
        dst.executescript(_SCHEMA_SQL)

        if not course_ids:
            dst.commit()
            return

        placeholders = ",".join("?" * len(course_ids))

        course_rows = src.execute(
            f"SELECT * FROM courses WHERE course_id IN ({placeholders})",
            course_ids,
        ).fetchall()
        for row in course_rows:
            dst.execute(
                "INSERT OR REPLACE INTO courses (course_id, title, teacher)"
                " VALUES (?, ?, ?)",
                (row["course_id"], row["title"], row["teacher"]),
            )

        for table in ("lectures", "ppt_pages"):
            if table == "lectures":
                rows = src.execute(
                    f"SELECT * FROM lectures WHERE course_id IN ({placeholders})",
                    course_ids,
                ).fetchall()
            else:
                rows = src.execute(
                    f"""SELECT pp.* FROM ppt_pages pp
                        JOIN lectures l ON pp.sub_id = l.sub_id
                        WHERE l.course_id IN ({placeholders})""",
                    course_ids,
                ).fetchall()
            if not rows:
                continue
            cols = list(rows[0].keys())
            col_str = ", ".join(cols)
            ph_str = ", ".join("?" * len(cols))
            dst.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({ph_str})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        dst.commit()
    finally:
        dst.close()
        src.close()


# ── Top-level entry point ─────────────────────────────────────────────────

def shard_database(
    db_path: str,
    output_dir: str,
    password: str,
    target_size: int = 0,
) -> dict:
    """Split ``db_path`` into encrypted shards under ``output_dir``.

    Uses Persistent Slot Assignment (see module docstring).  The legacy
    ``target_size`` parameter is accepted but ignored (the CLI in
    scripts/db_shard.py still passes it).
    """
    del target_size  # legacy parameter; PSA caps are constants now.

    shards_dir = os.path.join(output_dir, SHARDS_DIR)
    os.makedirs(shards_dir, exist_ok=True)

    # ── Compute PSA assignment first ───────────────────────────────
    # The order matters: we must persist the new PSA + subscription
    # state to the source DB BEFORE building the meta shard, otherwise
    # the meta shard would carry the previous run's state and the
    # next run would re-assign from stale data.
    src_conn = sqlite3.connect(db_path)
    try:
        subscribed = _get_subscribed_set(src_conn)
        groups, new_assignment = _assign_psa(src_conn, subscribed)
        _save_psa_state(src_conn, new_assignment)
    finally:
        src_conn.close()

    # ── Meta shard (now includes the freshly-saved PSA state) ──────
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        meta_tmp = tmp.name
    try:
        _build_meta_shard(db_path, meta_tmp)
        with open(meta_tmp, "rb") as f:
            meta_raw = f.read()
    finally:
        os.unlink(meta_tmp)
    meta_gz = gzip.compress(meta_raw, compresslevel=9, mtime=0)
    meta_enc = crypto_box.encrypt(meta_gz, password, deterministic=True)
    meta_sha = hashlib.sha256(meta_enc).hexdigest()
    meta_path = os.path.join(shards_dir, META_SHARD_NAME)
    with open(meta_path, "wb") as f:
        f.write(meta_enc)
    meta_entry = {
        "name": META_SHARD_NAME,
        "bin": "meta",
        "sha256": meta_sha,
        "size": len(meta_enc),
    }

    # ── Course-data shards ────────────────────────────────────────
    shard_entries: list[dict] = []
    for bin_name, prefix in (("hot", "shard-hot"), ("cold", "shard-cold")):
        for shard_idx, cids in enumerate(groups[bin_name]):
            if not cids:
                # Empty slot (a previously-populated shard whose entire
                # course set migrated to the other bin).  Skipping
                # means the frontend never sees a name for this slot;
                # the next new course in this bin will refill the
                # index via _assign_psa's "lowest unused" rule.
                continue
            name = f"{prefix}-{shard_idx:04d}.db.gz.enc"
            shard_path = os.path.join(shards_dir, name)

            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                _build_shard_db(db_path, cids, tmp_path)
                with open(tmp_path, "rb") as f:
                    raw = f.read()
            finally:
                os.unlink(tmp_path)

            gzipped = gzip.compress(raw, compresslevel=9, mtime=0)
            encrypted = crypto_box.encrypt(gzipped, password, deterministic=True)
            sha256 = hashlib.sha256(encrypted).hexdigest()
            with open(shard_path, "wb") as f:
                f.write(encrypted)

            shard_entries.append({
                "name": name,
                "bin": bin_name,
                "sha256": sha256,
                "size": len(encrypted),
                "course_ids": cids,
            })

    shard_entries.insert(0, meta_entry)

    index = {
        "version": INDEX_VERSION,
        "shards": shard_entries,
    }
    index_bytes = json.dumps(
        index, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    encrypted_index = crypto_box.encrypt(
        index_bytes, password, deterministic=True,
    )
    with open(os.path.join(output_dir, INDEX_FILENAME), "wb") as f:
        f.write(encrypted_index)

    return index


def load_index(index_path: str, password: str) -> dict:
    """Decrypt and parse an icourse-index.enc file."""
    with open(index_path, "rb") as f:
        encrypted = f.read()
    plaintext = crypto_box.decrypt(encrypted, password)
    if not crypto_box.is_json_obj(plaintext):
        raise ValueError(
            "decrypted index does not look like JSON — wrong password?"
        )
    return json.loads(plaintext)


def _migrate_shard_schema(target: sqlite3.Connection) -> None:
    """Ensure every attached shard has the same columns as ``main``.

    Older shards (created by previous code versions) may lack migration
    columns (from previous schema versions), which causes ``INSERT ... SELECT *``
    to fail with a column-count mismatch.  Adding the missing column
    to the shard before the INSERT makes the ``*`` lists match.
    """
    meta_exists = target.execute(
        "SELECT 1 FROM shard.sqlite_master "
        "WHERE type='table' AND name='meta'"
    ).fetchone()
    if not meta_exists:
        target.execute(
            "CREATE TABLE IF NOT EXISTS shard.meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )

    for table in ("lectures", "ppt_pages", "courses", "all_courses", "meta"):
        main_cols = {
            row[1] for row in target.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        }
        shard_cols = {
            row[1] for row in target.execute(
                f"PRAGMA shard.table_info('{table}')"
            ).fetchall()
        }
        from src.data.schema import (
            LECTURES_MIGRATION_COLUMNS,
            PPT_PAGES_MIGRATION_COLUMNS,
        )
        if table == "lectures":
            migrate = LECTURES_MIGRATION_COLUMNS
        elif table == "ppt_pages":
            migrate = PPT_PAGES_MIGRATION_COLUMNS
        else:
            migrate = []
        # Add columns that main has but shard is missing.
        for col, typedef in migrate:
            if col in main_cols and col not in shard_cols:
                target.execute(
                    f"ALTER TABLE shard.{table} ADD COLUMN {col} {typedef}"
                )
        # Drop columns that shard has but main no longer has (schema
        # evolution — e.g. ``summary_format_version``, ``old_summary``).
        extra_cols = shard_cols - main_cols
        for col in extra_cols:
            target.execute(f"ALTER TABLE shard.{table} DROP COLUMN {col}")


def reassemble_database(
    index: dict, shards_dir: str, output_db: str, password: str,
) -> None:
    """UNION every shard into a fresh sqlite at output_db.

    Used by the CI workflow on first run after the sharded format ships,
    and by tests that round-trip through shard → reassemble.
    """
    if os.path.exists(output_db):
        os.remove(output_db)

    target = sqlite3.connect(output_db)
    try:
        target.executescript(_SCHEMA_SQL)
        target.commit()

        for shard in index.get("shards", []):
            shard_path = os.path.join(shards_dir, shard["name"])
            with open(shard_path, "rb") as f:
                encrypted = f.read()
            gzipped = crypto_box.decrypt(encrypted, password)
            if not crypto_box.is_gzip(gzipped):
                raise ValueError(
                    f"decrypted shard {shard['name']!r} is not gzip — "
                    f"wrong password?"
                )
            raw = gzip.decompress(gzipped)

            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            try:
                target.execute("ATTACH DATABASE ? AS shard", (tmp_path,))
                try:
                    _migrate_shard_schema(target)
                    target.execute(
                        "INSERT OR IGNORE INTO main.courses "
                        "SELECT * FROM shard.courses"
                    )
                    target.execute(
                        "INSERT OR IGNORE INTO main.lectures "
                        "SELECT * FROM shard.lectures"
                    )
                    target.execute(
                        "INSERT OR IGNORE INTO main.ppt_pages "
                        "SELECT * FROM shard.ppt_pages"
                    )
                    # all_courses and meta are only ever populated in
                    # the meta shard, but INSERT OR IGNORE makes this
                    # safe even if a course-data shard happened to
                    # carry rows for them (legacy shards do).
                    target.execute(
                        "INSERT OR IGNORE INTO main.all_courses "
                        "SELECT * FROM shard.all_courses"
                    )
                    target.execute(
                        "INSERT OR IGNORE INTO main.meta "
                        "SELECT * FROM shard.meta"
                    )
                    target.commit()
                finally:
                    target.execute("DETACH DATABASE shard")
            finally:
                os.unlink(tmp_path)
    finally:
        target.close()
