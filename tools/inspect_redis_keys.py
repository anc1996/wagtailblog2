# -*- coding: utf-8 -*-
import subprocess
import json
import sys

# Remote script
remote_code = '''
import redis
import json

res = {}
for db in [1, 2, 3, 4, 5, 10, 12]:
    r = redis.Redis(host='localhost', port=6379, password='123456', db=db)
    keys = [k.decode('utf-8', errors='replace') for k in r.keys('*')]
    items = []
    for k in keys:
        t = r.type(k).decode()
        ttl = r.ttl(k)
        items.append({'key': k, 'type': t, 'ttl': ttl})
    res[str(db)] = {'total': len(keys), 'items': items}

print(json.dumps(res, ensure_ascii=False))
'''

# Check if running in Linux or Windows
cmd = ['ssh', 'root@192.168.20.2', f'/root/anaconda3/envs/wagtailblog/bin/python -c "{remote_code}"']
p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')

if p.returncode != 0:
    print('ERR:', p.stderr)
    sys.exit(1)

data = json.loads(p.stdout)
for db, info in data.items():
    print(f"\n=================== DB {db} (Total {info['total']}) ===================")
    for it in info['items'][:25]:
        print(f"  [{it['type']:<6}] (ttl={it['ttl']:<8}) {it['key']}")
    if info['total'] > 25:
        print(f"  ... and {info['total'] - 25} more keys")
