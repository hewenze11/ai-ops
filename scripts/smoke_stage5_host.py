"""Stage 5 real-host check: accounts, sudoers, ownership, readonly floor.

Runs on 172.236.254.94 as root. Installs nothing permanent: it provisions the
two accounts the way install-agent.sh does, imports the same readonly module the
agent uses, and asserts the kernel floor is real.

Env for the shell step: PASSWORD_HASH is not needed -- accounts get locked
passwords, which is what the installer does (no login, only setuid).
"""
import json
import sys

sys.path.insert(0, r"C:\Users\Administrator\.easyclaw\workspace\scratch\ai-ops-development")
import mc_hosts

REMOTE_DIR = "/opt/aiops-stage5"
READ = "aiops_stage5_read"
OPS = "aiops_stage5_ops"
SECRET = "/opt/aiops-stage5/secret.env"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (" :: " + detail if detail else ""))


def sh(cmd, timeout=180):
    return mc_hosts.host("94", cmd, timeout)


def main():
    t = mc_hosts.connect("94")
    try:
        # 0. clean slate
        sh("pkill -f aiops-stage5 || true; rm -rf %s; mkdir -p %s" % (REMOTE_DIR, REMOTE_DIR))
        for user in (READ, OPS):
            sh("userdel -r %s 2>/dev/null || true" % user)
            sh("rm -f /etc/sudoers.d/aiops-stage5-%s" % user)

        # 1. accounts the way the installer creates them (no login shell needed
        #    for setuid, a real shell needed if a future change execs one)
        code, out = sh("useradd --system --create-home --shell /bin/bash %s && "
                       "useradd --system --create-home --shell /bin/bash %s && "
                       "id -u %s && id -u %s" % (READ, OPS, READ, OPS))
        ids = out.strip().splitlines()[-2:]
        check("two unprivileged accounts created", code == 0 and int(ids[0]) > 0 and int(ids[1]) > 0, ",".join(ids))

        # 2. sudoers: only exact commands, read account gets nothing. All sudo
        #    verification uses a THROWAWAY oneshot unit, so no real service
        #    (nginx/docker/the website) is ever touched by a test.
        TEST_UNIT = "aiops-stage5-test.service"
        sh("printf '%%s\\n' '[Unit]' 'Description=stage5 sudoers probe' '[Service]' "
           "'Type=oneshot' 'ExecStart=/usr/bin/true' > /etc/systemd/system/%s" % TEST_UNIT)
        sh("systemctl daemon-reload")
        sudoers = (
            "printf '%%s\\n' "
            "'# managed by stage5 smoke' "
            "'%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart %s' "
            "'%s ALL=(root) NOPASSWD: /usr/bin/systemctl stop %s' "
            "> /etc/sudoers.d/aiops-stage5-ops && chmod 440 /etc/sudoers.d/aiops-stage5-ops && "
            "visudo -cf /etc/sudoers.d/aiops-stage5-ops") % (OPS, TEST_UNIT, OPS, TEST_UNIT)
        code, out = sh(sudoers)
        check("exact-command sudoers validates", code == 0, out.strip().splitlines()[-1] if out.strip() else "")

        code, out = sh("sudo -l -U %s 2>&1" % READ)
        check("read account has no sudo", "not allowed to run sudo" in out, out.strip()[:80])

        code, out = sh("sudo -l -U %s 2>&1" % OPS)
        check("ops account may run exactly the listed commands",
              TEST_UNIT in out and out.count("systemctl") >= 2, out.strip()[:200])

        code, out = sh("runuser -u %s -- sudo -n systemctl restart %s 2>&1; echo rc=$?" % (OPS, TEST_UNIT))
        check("ops account may restart the listed unit", "rc=0" in out, out.strip()[:120])

        code, out = sh("runuser -u %s -- sudo -n systemctl restart ssh.service 2>&1; echo rc=$?" % OPS)
        check("ops account may NOT restart another unit",
              ("not allowed" in out.lower() or "password is required" in out) and "rc=0" not in out,
              out.strip()[:120])

        code, out = sh("runuser -u %s -- sudo -n systemctl status %s 2>&1; echo rc=$?" % (OPS, TEST_UNIT))
        check("status on an unlisted verb is refused",
              ("not allowed" in out.lower() or "password is required" in out) and "rc=0" not in out,
              out.strip()[:120])

        # 3. core file ownership: the read account cannot read the agent config
        sh("printf 'AGENT_TOKEN=%s\\n' abcdef0123456789 > /etc/aiops-stage5-agent.env && "
           "chown root:root /etc/aiops-stage5-agent.env && chmod 600 /etc/aiops-stage5-agent.env")
        code, out = sh("runuser -u %s -- cat /etc/aiops-stage5-agent.env 2>&1; echo END" % READ)
        check("read account cannot read the 0600 agent secret", "Permission denied" in out, out.strip().splitlines()[-1][:80])

        # 4. the readonly floor, using the real module
        sh("mkdir -p %s && printf 'PASSWORD=stage5-plain-value\\nDEBUG=1\\n' > %s && "
           "chmod 644 %s" % (REMOTE_DIR, SECRET, SECRET))
        mc_hosts.put(t, r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops-agent\ai_ops_agent\readonly.py",
                     REMOTE_DIR + "/readonly.py")
        mc_hosts.put(t, r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops-agent\ai_ops_agent\credentials.py",
                     REMOTE_DIR + "/credentials.py")

        probe = r'''
import json, sys, os
sys.path.insert(0, "%s")
import readonly

def task(user, path):
    return {"claim_id": "c"*24, "kind": "readonly", "run_as": user,
            "execution_users": [user], "tool": "read_file",
            "program": readonly.WHITELIST["read_file"], "args": ["--", path]}

out = {}
out["root_refused"] = readonly.execute(task("root", "/etc/shadow"), ["root"])
out["root_allowed"] = readonly.execute(task("root", "/etc/shadow"), ["root"], allow_root_readonly=True)
out["unpriv_ok"] = readonly.execute(task("%s", "%s"), ["%s"])
out["unpriv_denied"] = readonly.execute(task("%s", "/etc/aiops-stage5-agent.env"), ["%s"])
print(json.dumps(out))
''' % (REMOTE_DIR, READ, SECRET, READ, READ, READ)

        code, out = sh("python3 -c " + repr(probe).replace("'", '"', 0), 300) if False else (0, "")
        # safer: write the probe through a heredoc so quoting never matters
        with open(r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops\scratch\_stage5_probe.py", "w") as handle:
            handle.write(probe)
        sftp = __import__("paramiko").SFTPClient.from_transport(t)
        sftp.put(r"C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops\scratch\_stage5_probe.py",
                 REMOTE_DIR + "/probe.py")
        sftp.close()
        code, out = sh("cd %s && python3 probe.py 2>&1" % REMOTE_DIR, 300)
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except Exception:
            check("probe ran", False, out.strip()[:300])
            return
        check("readonly as root is refused",
              data["root_refused"].get("error_code") == "READONLY_USER_ROOT",
              data["root_refused"].get("error_code", ""))
        check("readonly as root runs when explicitly allowed",
              data["root_allowed"].get("status") == "succeeded" and "root:" in data["root_allowed"].get("stdout", ""),
              str(data["root_allowed"].get("status")))
        check("readonly as an unprivileged account reads the file",
              data["unpriv_ok"].get("status") == "succeeded" and "DEBUG=1" in data["unpriv_ok"].get("stdout", ""),
              str(data["unpriv_ok"].get("status")))
        check("the value is masked before it leaves the host",
              "stage5-plain-value" not in data["unpriv_ok"].get("stdout", "")
              and "[REDACTED]" in data["unpriv_ok"].get("stdout", ""),
              data["unpriv_ok"].get("stdout", "")[:60].replace("\n", " "))
        check("unprivileged account cannot read a root 0600 file",
              data["unpriv_denied"].get("exit_code") not in (0, None),
              "exit=%s" % data["unpriv_denied"].get("exit_code"))

        # 5. cleanup
        sh("rm -rf %s /etc/aiops-stage5-agent.env /etc/sudoers.d/aiops-stage5-ops /etc/systemd/system/%s" % (REMOTE_DIR, TEST_UNIT))
        sh("systemctl daemon-reload")
        for user in (READ, OPS):
            sh("userdel -r %s 2>/dev/null || true" % user)
        code, out = sh("id %s 2>&1; id %s 2>&1; ls %s 2>&1" % (READ, OPS, REMOTE_DIR))
        check("everything was removed", "no such user" in out and "No such file" in out, "")
    finally:
        t.close()

    failed = [r for r in results if not r[1]]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    print(("FAILED: " + ", ".join(r[0] for r in failed)) if failed else "ALL PASS")
    sys.exit(1 if failed else 0)


main()
