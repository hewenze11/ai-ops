import json, glob, os
for f in glob.glob('/var/lib/docker/containers/*/config.v2.json'):
    d = json.load(open(f))
    print('NAME', d.get('Name'), 'IMAGE', d.get('Config',{}).get('Image'), 'STATE', d.get('State',{}).get('Status'), 'RUNNING', d.get('State',{}).get('Running'))
