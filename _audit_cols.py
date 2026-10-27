import sys
sys.path.insert(0, r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops")
import os
os.chdir(r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops")
import tempfile, sqlite3
from ai_ops.app import create_app

d = tempfile.mkdtemp()
db_path = os.path.join(d, "control.db")
create_app(db_path, "t0ken-" + "x" * 32, "test-model")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

for t in ("asset_offline_events", "local_host", "sensitive_hits",
          "account_requests", "wait_handles", "fanout_children", "blocked_kinds",
          "credential_events", "task_leases"):
    cols = [c[1] for c in db.execute("PRAGMA table_info(%s)" % t)]
    print("%-24s %s" % (t, cols))
    print()

print("=== is local_host.asset_id a PK / unique?")
for r in db.execute("PRAGMA index_list(local_host)"):
    print("  index:", dict(r))
