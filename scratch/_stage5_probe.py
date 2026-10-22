
import json, sys, os
sys.path.insert(0, "/opt/aiops-stage5")
import readonly

def task(user, path):
    return {"claim_id": "c"*24, "kind": "readonly", "run_as": user,
            "execution_users": [user], "tool": "read_file",
            "program": readonly.WHITELIST["read_file"], "args": ["--", path]}

out = {}
out["root_refused"] = readonly.execute(task("root", "/etc/shadow"), ["root"])
out["root_allowed"] = readonly.execute(task("root", "/etc/shadow"), ["root"], allow_root_readonly=True)
out["unpriv_ok"] = readonly.execute(task("aiops_stage5_read", "/opt/aiops-stage5/secret.env"), ["aiops_stage5_read"])
out["unpriv_denied"] = readonly.execute(task("aiops_stage5_read", "/etc/aiops-stage5-agent.env"), ["aiops_stage5_read"])
print(json.dumps(out))
