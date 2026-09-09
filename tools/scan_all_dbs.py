# -*- coding: utf-8 -*-
import redis

client = redis.Redis(host='192.168.20.2', port=6379, password='123456')
for db in range(16):
    r = redis.Redis(host='192.168.20.2', port=6379, password='123456', db=db)
    try:
        size = r.dbsize()
        if size > 0:
            keys = [k.decode('utf-8', errors='replace') for k in r.keys('*')[:10]]
            print(f"DB {db:2d}: {size:4d} keys | Sample: {keys}")
        else:
            print(f"DB {db:2d}:    0 keys (空闲)")
    except Exception as e:
        print(f"DB {db:2d}: error {e}")
