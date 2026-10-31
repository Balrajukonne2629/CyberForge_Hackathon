import sys
#!/usr/bin/env python3
import subprocess, time, httpx, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from bench.bench import benchmark_queries

def main():
    p_api = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'api.main:app', '--port', '8000'], env=dict(os.environ, PROFILE='10m'))
    time.sleep(2.0)
    try:
        results = benchmark_queries('10m', 'http://127.0.0.1:8000')
        print('\n' + '=' * 70)
        print('10M QUERY SPEED RESULTS (AFTER COVERING INDEX & VACUUM):')
        print('=' * 70)
        for k, v in results.items():
            print(f"  {k:<15}: p50={v['p50']:>6.2f} ms | p95={v['p95']:>6.2f} ms | max={v['max']:>6.2f} ms")
    finally:
        p_api.terminate()
        p_api.wait()

if __name__ == '__main__':
    main()
