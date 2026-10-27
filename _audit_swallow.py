import sys
sys.path.insert(0, r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops")
import os
os.chdir(r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops")
import tempfile, sqlite3
from ai_ops.app import create_app
from ai_ops import sensitive

d = tempfile.mkdtemp()
db_path = os.path.join(d, "control.db")
create_app(db_path, "t0ken-" + "x" * 32, "test-model")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

# Make one live asset + one orphan hit.
db.execute("INSERT INTO assets(id,name,allowed_users,notes,token_hash) VALUES('live','Live','[]','','h')")
sensitive.record(db, turn_id='t1', task_id='k1', asset_id='live', path='/a', rule_ids=['r'], baseline_version='v')
sensitive.record(db, turn_id='t1', task_id='k2', asset_id='gone', path='/b', rule_ids=['r'], baseline_version='v')
db.commit()

print("=== normal: orphan filtered")
print("  listed:", len(sensitive.pending(db)), "(expect 1)")

print()
print("=== SIMULATE: query on assets fails for a NON-'missing table' reason")
# A corrupted/locked schema is the realistic case. Use a connection whose
# assets table is replaced by a view that raises.
db2 = sqlite3.connect(db_path)
db2.row_factory = sqlite3.Row
db2.execute("PRAGMA foreign_keys=OFF")
db2.execute("ALTER TABLE assets RENAME TO assets_real")
db2.execute("CREATE VIEW assets AS SELECT * FROM does_not_exist")
try:
    print("  listed:", len(sensitive.pending(db2)), "(if 2, filtering silently switched OFF)")
    print("  -> the except-Exception swallowed the failure and stopped filtering")
except Exception as e:
    print("  raised:", type(e).__name__, e)
