#!/usr/bin/env python3
"""
并行跑基础4狗，7天一个批次。

用法:
  python run_base_dogs.py 2026-06-12 2026-07-06    # 全量
  python run_base_dogs.py 2026-06-12 2026-06-18    # 单批7天
"""
import sys
import subprocess
import time
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

DOGS = ["梭哈2狗", "梭哈3狗"]
WORKERS = 2
CHUNK_DAYS = 7


def run_chunk(dog: str, start: str, end: str) -> dict:
    """跑一个狗的一个7天批次"""
    cmd = [
        "python", "dsfootball_cli.py", "agent", dog, "runall",
        start, end, "--jingcai"
    ]
    t0 = time.time()
    tag = f"[{dog}]"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        last_print = t0
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)
            # 始终显示耗时
            elapsed = time.time() - t0
            tag_with_time = f"[{dog} {elapsed:.0f}s]"
            print(f"  {tag_with_time} {line}", flush=True)
        proc.wait(timeout=3600)
        elapsed = time.time() - t0
        capital = "?"
        for line in output_lines:
            if "余额" in line:
                capital = line.strip()
        return {
            "dog": dog, "start": start, "end": end,
            "ok": proc.returncode == 0,
            "elapsed": elapsed, "capital": capital,
        }
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"dog": dog, "start": start, "end": end, "ok": False, "elapsed": 3600, "capital": "TIMEOUT"}
    except Exception as e:
        return {"dog": dog, "start": start, "end": end, "ok": False, "elapsed": time.time()-t0, "capital": str(e)}


def main():
    if len(sys.argv) < 3:
        print("用法: python run_base_dogs.py <start> <end>")
        print("  例: python run_base_dogs.py 2026-06-12 2026-07-06")
        sys.exit(1)

    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])

    # 切成7天批次
    chunks = []
    d = start
    while d <= end:
        chunk_end = min(d + timedelta(days=CHUNK_DAYS - 1), end)
        chunks.append((d.isoformat(), chunk_end.isoformat()))
        d += timedelta(days=CHUNK_DAYS)

    print(f"基础2狗 | {start} ~ {end} | {len(chunks)}批次 × 7天 | workers={WORKERS}")
    print("=" * 60)

    total_start = time.time()
    all_ok = True

    for ci, (cs, ce) in enumerate(chunks):
        print(f"\n📅 批次 {ci+1}/{len(chunks)}: {cs} ~ {ce}")

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(run_chunk, dog, cs, ce): dog for dog in DOGS}
            results = []
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                status = "✅" if r["ok"] else "❌"
                elapsed = f"{r['elapsed']:.0f}s"
                print(f"  {status} {r['dog']} | {elapsed} | {r['capital'][:80]}")

        # 检查是否有狗死了
        for r in results:
            if not r["ok"]:
                print(f"  ⚠️ {r['dog']} 异常: {r['stderr_tail'][:200]}")
                all_ok = False

        if not all_ok:
            print(f"\n⚠️ 批次 {ci+1} 有错误, 继续下一批...")
            all_ok = True  # 继续跑，不中断

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | 总耗时 {total_elapsed/60:.1f}分钟")

    # 最终摘要
    print(f"\n最终资金:")
    import json
    for dog in DOGS:
        rf = Path(f"lota_data/roles/{dog}/{dog}.json")
        if rf.exists():
            r = json.loads(rf.read_text())
            pnl = r["capital"] - r["initial_capital"]
            print(f"  {dog}: {r['capital']:.0f} (PnL {pnl:+.0f})")


if __name__ == "__main__":
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    main()
