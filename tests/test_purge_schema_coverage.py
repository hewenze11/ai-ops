"""Purge must leave nothing behind, including rows in tables that declare no FK.

The first purge fix was driven by the foreign-key graph. That was still not the
whole picture: nine tables carry an `asset_id` column with no FK declared
(`asset_offline_events`, `credential_events`, `wait_handles`, `fanout_children`,
`task_leases`, `blocked_kinds`, `local_host`, `sensitive_hits`,
`account_requests`). Purging only what the FK graph mentions leaves those rows
pointing at an asset that no longer exists -- silent corruption, because nothing
raises.

The second thing these tests pin down is grandchildren. The previous code
deleted direct children in a loop and relied on one hand-written statement for
the single grandchild that happened to exist (`heartbeat_attempts` through
`heartbeat_streams`). A test that only exercises today's schema would not catch
a second grandchild appearing, so the coverage here inserts rows through a
grandchild and checks the delete order holds.
"""
import sqlite3

from test_control import env, submit, claim, finish


def _insert(path, sql, args=()):
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(sql, args)
        db.commit()


def _purge(client, headers, asset_id):
    return client.post("/api/v1/assets/%s/purge" % asset_id, headers=headers,
                       json={"confirm_asset_id": asset_id})


def _leftovers(path, asset_id):
    """Rows anywhere that still reference the purged asset."""
    found = {}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        tables = [r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            if t == "assets":
                continue
            cols = [c[1] for c in db.execute('PRAGMA table_info("%s")' % t)]
            if "asset_id" in cols:
                n = db.execute('SELECT count(*) FROM "%s" WHERE asset_id=?' % t,
                               (asset_id,)).fetchone()[0]
                if n:
                    found[t] = n
    return found


def test_purge_clears_tables_that_declare_no_foreign_key(env):
    c, h, _, path = env
    # One row in every FK-less asset table that exists in this schema.
    _insert(path, "INSERT INTO asset_offline_events(id,asset_id,silent_for,last_seen,emitted_at,delivered) "
                  "VALUES('e1','a',30,0,0,0)")
    _insert(path, "INSERT INTO credential_events(asset_id,credential_id,event,actor,detail,created_at) "
                  "VALUES('a','h1','seen','admin','',0)")
    _insert(path, "INSERT INTO blocked_kinds(asset_id,protocol,task_ids,reported_at) "
                  "VALUES('a','1.1','[]',0)")

    before = _leftovers(path, "a")
    assert "asset_offline_events" in before and "credential_events" in before, \
        "fixture did not plant the rows this test is about: %s" % before

    resp = _purge(c, h, "a")
    assert resp.status_code == 200, resp.text

    after = _leftovers(path, "a")
    assert after == {}, "purge left rows in tables with no foreign key: %s" % after


def test_purge_clears_grandchild_rows_through_their_parent(env):
    """heartbeat_attempts has no asset_id; it hangs off heartbeat_streams."""
    c, h, _, path = env
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO heartbeat_streams(asset_id,session_id,controller,high_seq,updated_at,retained_from_seq) "
                   "VALUES('a','sess','c',0,0,0)")
        stream_id = db.execute("SELECT id FROM heartbeat_streams WHERE asset_id='a'").fetchone()[0]
        db.execute("INSERT INTO heartbeat_attempts(stream_id,attempt_seq,received_at,ack_status,recorded_at) "
                   "VALUES(?,0,0,'ok',0)", (stream_id,))
        db.commit()

    with sqlite3.connect(path) as db:
        n = db.execute("SELECT count(*) FROM heartbeat_attempts").fetchone()[0]
    assert n == 1, "fixture did not plant the grandchild row"

    resp = _purge(c, h, "a")
    assert resp.status_code == 200, resp.text

    with sqlite3.connect(path) as db:
        left = db.execute("SELECT count(*) FROM heartbeat_attempts").fetchone()[0]
        streams = db.execute("SELECT count(*) FROM heartbeat_streams WHERE asset_id='a'").fetchone()[0]
        broken = db.execute("PRAGMA foreign_key_check").fetchall()
    assert left == 0, "grandchild rows survived the purge"
    assert streams == 0, "parent rows survived the purge"
    assert broken == [], "purge left dangling foreign keys: %s" % broken


def test_purge_of_one_asset_leaves_the_other_completely_intact(env):
    """The generic delete plan must not over-delete."""
    c, h, _, path = env
    _insert(path, "INSERT INTO host_tags(asset_id,tag) VALUES('b','keep')")
    _insert(path, "INSERT INTO asset_offline_events(id,asset_id,silent_for,last_seen,emitted_at,delivered) "
                  "VALUES('e2','b',30,0,0,0)")

    assert _purge(c, h, "a").status_code == 200

    with sqlite3.connect(path) as db:
        tags = db.execute("SELECT count(*) FROM host_tags WHERE asset_id='b'").fetchone()[0]
        events = db.execute("SELECT count(*) FROM asset_offline_events WHERE asset_id='b'").fetchone()[0]
        asset = db.execute("SELECT count(*) FROM assets WHERE id='b'").fetchone()[0]
    assert (tags, events, asset) == (1, 1, 1), "purge touched the surviving asset"
