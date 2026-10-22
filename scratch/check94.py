import sys
sys.path.insert(0, r'C:\Users\Administrator\.easyclaw\workspace\scratch\ai-ops-development')
import mc_hosts

t = mc_hosts.connect('94')
print(mc_hosts.run(t, 'curl -s -o /dev/null -w "website:%{http_code}" http://127.0.0.1:18080/; echo; '
                      'ls /etc/sudoers.d/; id aiops_stage5_read 2>&1; ls /opt/ | grep -i stage5; echo done')[1])
t.close()
