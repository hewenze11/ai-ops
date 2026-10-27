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

# Reproduce the ordering the code actually uses.
rows = db.execute(
    "SELECT m.name AS child, p.'from' AS col FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS p "
    "WHERE m.type='table' AND p.'table'='assets' ORDER BY m.name").fetchall()
order = [(r["child"], r["col"]) for r in rows]
print("=== deletion order produced by _asset_fk_children (alphabetical)")
for i, (t, c) in enumerate(order, 1):
    print("  %2d. %s" % (i, t))

print()
print("=== PROBLEM: heartbeat_streams must be deleted BEFORE heartbeat_attempts,")
print("    but the loop deletes heartbeat_streams (asset child) and never")
print("    touches heartbeat_attempts (it is NOT an asset child).")
print("    The code relies on one hand-written line above the loop to fix this.")
print()
print("    If that line is ever removed/reordered, DELETE FROM heartbeat_streams")
print("    fails with a FK error and rolls back mid-transaction -- the exact bug")
print("    this rewrite was meant to end.")
