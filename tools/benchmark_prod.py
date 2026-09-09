# -*- coding: utf-8 -*-
import urllib.request
import urllib.parse
import time
import json

def test_url(name, url, runs=5, check_json=False):
    print(f"\n==========================================")
    print(f"=== Testing: {name} ===")
    print(f"URL: {url}")
    times = []
    for i in range(runs):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Benchmark"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            status = resp.status
            headers = dict(resp.getheaders())
        duration_ms = (time.perf_counter() - t0) * 1000
        times.append(duration_ms)
        size_kb = len(data) / 1024
        has_preloader = b'class="preloader"' in data or b"class='preloader'" in data

        info = f"  Run {i+1}: HTTP {status} | Size: {size_kb:.2f} KB | Time: {duration_ms:.2f} ms"
        if not check_json:
            info += f" | Preloader: {has_preloader}"
        else:
            try:
                js = json.loads(data.decode('utf-8'))
                info += f" | success={js.get('success')} | total_count={js.get('meta', {}).get('total_count')} | page={js.get('meta', {}).get('page')}"
            except Exception as e:
                info += f" | json_err={e}"
        print(info)
        time.sleep(0.1)

    avg_hot = sum(times[1:]) / len(times[1:]) if len(times) > 1 else times[0]
    print(f"--> Summary: Cold: {times[0]:.2f} ms | Hot Avg: {avg_hot:.2f} ms | Hot Min: {min(times[1:] if len(times) > 1 else times):.2f} ms")

if __name__ == "__main__":
    cat_url = "http://192.168.20.2:6050/zh-hans/" + urllib.parse.quote("公文材料/政务公开题材/")
    test_url("生产目标慢页面: 公文材料/政务公开题材 (122篇文章)", cat_url, runs=5)

    ajax_url = "http://192.168.20.2:6050/zh-hans/blog/api/index-pages/24/results/?page=2"
    test_url("生产AJAX分页接口: 类别ID 24 (第2页)", ajax_url, runs=5, check_json=True)

    home_url = "http://192.168.20.2:6050/zh-hans/"
    test_url("生产首页: zh-hans", home_url, runs=5)
