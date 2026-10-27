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

print("=== 1. every FK edge between tables (who points at whom)")
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
edges = {}
for t in tables:
    try:
        fks = db.execute("PRAGMA foreign_key_list(%s)" % t).fetchall()
    except Exception:
        continue
    for fk in fks:
        edges.setdefault(fk["table"], []).append((t, fk["from"]))

for parent, children in sorted(edges.items()):
    print("  %-26s <- %s" % (parent, ", ".join("%s.%s" % c for c in children)))

print()
print("=== 2. does any ASSET-child table depend on another ASSET-child table?")
asset_children = set(t for t, c in
    [(r["child"], r["col"]) for r in db.execute(
        "SELECT m.name AS child, p.'from' AS col FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS p "
        "WHERE m.type='table' AND p.'table'='assets'")])
print("  asset children:", sorted(asset_children))
risky = []
for parent in asset_children:
    for child, col in edges.get(parent, []):
        if child != parent:
            risky.append((parent, child, col))
print("  edges from an asset-child to another table:", risky if risky else "none")

print()
print("=== 3. tables with an asset_id column but NO foreign key (invisible to the loop)")
for t in tables:
    cols = [c[1] for c in db.execute("PRAGMA table_info(%s)" % t)]
    if "asset_id" in cols and t not in asset_children:
        n = db.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
        print("  %-26s rows=%d" % (t, n))
