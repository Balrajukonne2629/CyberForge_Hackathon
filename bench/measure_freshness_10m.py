#!/usr/bin/env python3
import subprocess, time, httpx, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from bench.bench import benchmark_freshness

def main():
    p_api = subprocess.Popen(['py', '-3.12', '-m', 'uvicorn', 'api.main:app', '--port', '8000'], env=dict(os.environ, PROFILE='10m'))
    p_worker = subprocess.Popen(['py', '-3.12', 'worker/worker.py', '--profile', '10m', '--poll-interval', '0.5'])
    time.sleep(2.0)
    try:
        res = benchmark_freshness(profile='10m', base_url='http://127.0.0.1:8000', count=50)
        print('\n' + '=' * 70)
        print('10M FRESHNESS BENCHMARK RESULTS (50 PROBES):')
        print('=' * 70)
        print(f"  p50 : {res['p50']:.2f} ms")
        print(f"  p95 : {res['p95']:.2f} ms")
        print(f"  max : {res['max']:.2f} ms")
    finally:
        p_worker.terminate()
        p_api.terminate()
        p_worker.wait()
        p_api.wait()

if __name__ == '__main__':
    main()
