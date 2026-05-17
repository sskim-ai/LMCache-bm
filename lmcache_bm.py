#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

TP_COUNT = int(os.getenv("TP_COUNT", "2"))
FILES_PER_TP = int(os.getenv("FILES_PER_TP", "32"))
WRITE_JOBS_PER_TP = int(os.getenv("WRITE_JOBS_PER_TP", "4"))
TOTAL_FILE_SIZE_MB = float(os.getenv("TOTAL_FILE_SIZE_MB", "24"))
RUNTIME = float(os.getenv("RUNTIME", "60"))
BASE_DIR = Path(os.getenv("BASE_DIR", "/mnt/lmcache_raid/lmcache_fio_workdir"))
READ_BS_LIST = os.getenv("READ_BS_LIST", "").split()
READ_PASS_DELAY_MIN_MS = int(os.getenv("READ_PASS_DELAY_MIN_MS", "0"))
READ_PASS_DELAY_MAX_MS = int(os.getenv("READ_PASS_DELAY_MAX_MS", "10"))
WRITE_FILE_DELAY_MIN_MS = int(os.getenv("WRITE_FILE_DELAY_MIN_MS", "0"))
WRITE_FILE_DELAY_MAX_MS = int(os.getenv("WRITE_FILE_DELAY_MAX_MS", "10"))
MODE = os.getenv("MODE", "concurrent")

HELPER_DIR = Path(os.getenv("HELPER_DIR", "/home/skhynix/lmcache_bm"))
HELPER_SRC = HELPER_DIR / "lmcache_helper.c"
HELPER_BIN = HELPER_DIR / "lmcache_helper"

NUMA_CORE_LIST = os.getenv("NUMA_CORE_LIST", "0-71,144-215")
SPLIT_TP_CORES = os.getenv("SPLIT_TP_CORES", "1") == "1"

# Bind this Python runner and every subprocess to NUMA_CORE_LIST.
BIND_PARENT = os.getenv("BIND_PARENT", "1") == "1"
BIND_ALL_SUBPROCS = os.getenv("BIND_ALL_SUBPROCS", "1") == "1"

READ_ROOT = BASE_DIR / "read"
WRITE_ROOT = BASE_DIR / "write"
RESULTS_BASE = BASE_DIR / "results"
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
RESULT_DIR = RESULTS_BASE / RUN_ID

FILE_SIZE_BYTES = max(1, int(TOTAL_FILE_SIZE_MB * 1024 * 1024 / TP_COUNT))
FILES_PER_WRITE_JOB = FILES_PER_TP // WRITE_JOBS_PER_TP


def default_read_bs_list():
    mb = FILE_SIZE_BYTES / (1024 * 1024)
    if abs(mb - round(mb)) < 1e-9:
        return [f"{int(round(mb))}M"]
    return [str(FILE_SIZE_BYTES)]


if not READ_BS_LIST:
    READ_BS_LIST = default_read_bs_list()


def parse_bs(bs: str) -> int:
    s = bs.strip()
    ls = s.lower()
    if ls.endswith("m"):
        return int(float(ls[:-1]) * 1024 * 1024)
    if ls.endswith("k"):
        return int(float(ls[:-1]) * 1024)
    return int(ls)


def parse_core_list(spec: str) -> List[int]:
    cores = []
    if not spec.strip():
        return cores
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            cores.extend(range(int(a), int(b) + 1))
        else:
            cores.append(int(part))
    return sorted(set(cores))


def format_core_list(cores: List[int]) -> str:
    return ",".join(str(c) for c in cores)


def format_core_ranges(cores: List[int]) -> str:
    cores = sorted(set(cores))
    if not cores:
        return ""
    ranges = []
    start = prev = cores[0]
    for c in cores[1:]:
        if c == prev + 1:
            prev = c
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = c
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def all_cores_str():
    cores = parse_core_list(NUMA_CORE_LIST)
    return format_core_list(cores) if cores else None


def bind_parent_to_numa():
    cores = parse_core_list(NUMA_CORE_LIST)
    if BIND_PARENT and cores and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, set(cores))


def current_affinity_str():
    if hasattr(os, "sched_getaffinity"):
        return format_core_ranges(sorted(os.sched_getaffinity(0)))
    return "unknown"


def with_taskset(cmd: List[str], core_list: str | None = None) -> List[str]:
    if not BIND_ALL_SUBPROCS:
        return cmd
    cores = core_list or all_cores_str()
    if not cores:
        return cmd
    return ["taskset", "-c", cores] + cmd


def run_subprocess(cmd: List[str], *, core_list: str | None = None, **kwargs):
    return subprocess.run(with_taskset(cmd, core_list), **kwargs)


def tp_core_subset(tp: int):
    cores = parse_core_list(NUMA_CORE_LIST)
    if not cores:
        return None
    if not SPLIT_TP_CORES:
        return format_core_list(cores)
    chunk = max(1, len(cores) // TP_COUNT)
    start = tp * chunk
    end = len(cores) if tp == TP_COUNT - 1 else min(len(cores), start + chunk)
    subset = cores[start:end] or cores
    return format_core_list(subset)


def ensure_tools():
    run_subprocess(
        ["dd", "--version"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run_subprocess(
        ["gcc", "--version"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def install_helper():
    src_local = Path("/home/skhynix/lmcache_bm/lmcache_helper.c")
    if not src_local.exists():
        raise FileNotFoundError(f"helper source not found: {src_local}")
    run_subprocess(["gcc", "-O2", "-o", str(HELPER_BIN), str(HELPER_SRC)], check=True)


def reset_dirs():
    shutil.rmtree(READ_ROOT, ignore_errors=True)
    shutil.rmtree(WRITE_ROOT, ignore_errors=True)
    READ_ROOT.mkdir(parents=True, exist_ok=True)
    WRITE_ROOT.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)


def create_files_and_dirs():
    # create exact-size read files
    bs_m = max(1, FILE_SIZE_BYTES // (1024 * 1024))
    for tp in range(TP_COUNT):
        tp_read = READ_ROOT / f"tp{tp}"
        tp_read.mkdir(parents=True, exist_ok=True)
        for idx in range(FILES_PER_TP):
            p = tp_read / f"file_{idx}.bin"
            run_subprocess(
                [
                    "dd",
                    "if=/dev/zero",
                    f"of={p}",
                    "bs=1M",
                    f"count={bs_m}",
                    "conv=fsync",
                    "status=none",
                ],
                check=True,
            )
            with open(p, "r+b") as f:
                f.truncate(FILE_SIZE_BYTES)
                f.flush()
                os.fsync(f.fileno())

    for tp in range(TP_COUNT):
        for job in range(WRITE_JOBS_PER_TP):
            (WRITE_ROOT / f"tp{tp}" / f"job{job}").mkdir(parents=True, exist_ok=True)


def run_helper(args, tp: int):
    # Helper workers get either TP-specific subset or full NUMA_CORE_LIST.
    core_subset = tp_core_subset(tp)
    cmd = [str(HELPER_BIN)] + args
    res = run_subprocess(
        cmd, core_list=core_subset, check=True, capture_output=True, text=True
    )
    out = json.loads(res.stdout.strip())
    out["cpu_core_list"] = core_subset
    if core_subset:
        out["cpu_core_range"] = format_core_ranges(parse_core_list(core_subset))
    else:
        out["cpu_core_range"] = None
    return out


def run_read(tp: int, bs: str):
    bs_bytes = parse_bs(bs)
    args = [
        "read",
        str(READ_ROOT),
        str(tp),
        str(-1),
        str(FILES_PER_TP),
        str(FILE_SIZE_BYTES),
        str(bs_bytes),
        str(RUNTIME),
        str(READ_PASS_DELAY_MIN_MS),
        str(READ_PASS_DELAY_MAX_MS),
    ]
    out = run_helper(args, tp)
    out["worker"] = f"read_tp{tp}_seq"
    out["bs"] = bs
    return out


def run_write(tp: int, job: int):
    args = [
        "write",
        str(WRITE_ROOT),
        str(tp),
        str(job),
        str(FILES_PER_WRITE_JOB),
        str(FILE_SIZE_BYTES),
        str(-1),
        str(RUNTIME),
        str(WRITE_FILE_DELAY_MIN_MS),
        str(WRITE_FILE_DELAY_MAX_MS),
    ]
    out = run_helper(args, tp)
    out["worker"] = f"write_tp{tp}_job{job}"
    return out


def summarize(results):
    grouped = {}
    for r in results:
        grouped.setdefault((r["mode"], r["tp"]), []).append(r)

    summary = {
        "config": {
            "TP_COUNT": TP_COUNT,
            "FILES_PER_TP": FILES_PER_TP,
            "WRITE_JOBS_PER_TP": WRITE_JOBS_PER_TP,
            "TOTAL_FILE_SIZE_MB": TOTAL_FILE_SIZE_MB,
            "FILE_SIZE_BYTES": FILE_SIZE_BYTES,
            "READ_BS_LIST": READ_BS_LIST,
            "NUMA_CORE_LIST": NUMA_CORE_LIST,
            "SPLIT_TP_CORES": SPLIT_TP_CORES,
            "BIND_PARENT": BIND_PARENT,
            "BIND_ALL_SUBPROCS": BIND_ALL_SUBPROCS,
            "PARENT_AFFINITY": current_affinity_str(),
        },
        "workers": results,
        "tp_summary": {},
        "total_summary": {},
    }

    totals = {
        "read": {"bytes": 0, "ops": 0, "wall": 0.0},
        "write": {"bytes": 0, "ops": 0, "wall": 0.0},
    }

    for (mode, tp), arr in grouped.items():
        total_bytes = sum(x["total_bytes"] for x in arr)
        total_ops = sum(x["total_ops"] for x in arr)
        wall = max(x["wall_sec"] for x in arr) if arr else 0.0
        gbps = (total_bytes / 1e9) / wall if wall > 0 else 0.0
        iops = total_ops / wall if wall > 0 else 0.0
        summary["tp_summary"][f"{mode}_tp{tp}"] = {
            "mode": mode,
            "tp": tp,
            "num_workers": len(arr),
            "workers": [x["worker"] for x in arr],
            "cpu_core_lists": [x.get("cpu_core_list") for x in arr],
            "cpu_core_ranges": [x.get("cpu_core_range") for x in arr],
            "total_bytes": total_bytes,
            "total_ops": total_ops,
            "wall_sec": wall,
            "throughput_GBps": gbps,
            "IOPS": iops,
        }
        totals[mode]["bytes"] += total_bytes
        totals[mode]["ops"] += total_ops
        totals[mode]["wall"] = max(totals[mode]["wall"], wall)

    for mode in ("read", "write"):
        wall = totals[mode]["wall"]
        summary["total_summary"][mode] = {
            "total_bytes": totals[mode]["bytes"],
            "total_ops": totals[mode]["ops"],
            "wall_sec": wall,
            "throughput_GBps": (
                (totals[mode]["bytes"] / 1e9) / wall if wall > 0 else 0.0
            ),
            "IOPS": totals[mode]["ops"] / wall if wall > 0 else 0.0,
        }
    return summary


def print_summary(summary):
    print("\\n=== Config ===")
    print(json.dumps(summary["config"], indent=2))
    print("\\n=== TP summary ===")
    for key, item in summary["tp_summary"].items():
        print(
            f"{key}: mode={item['mode']} num_workers={item['num_workers']} "
            f"workers={item['workers']} cpu_core_ranges={item.get('cpu_core_ranges')} "
            f"GB/s={item['throughput_GBps']:.3f} IOPS={item['IOPS']:.1f}"
        )
    print("\\n=== TP total summary ===")
    for mode in ("read", "write"):
        item = summary["total_summary"][mode]
        print(
            f"{mode}: total GB/s={item['throughput_GBps']:.3f} total IOPS={item['IOPS']:.1f}"
        )


def main():
    bind_parent_to_numa()
    print(f"Parent PID={os.getpid()} affinity={current_affinity_str()}")
    ensure_tools()
    install_helper()
    reset_dirs()
    create_files_and_dirs()

    results = []
    if MODE == "concurrent":
        max_workers = TP_COUNT + TP_COUNT * WRITE_JOBS_PER_TP
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = []
            for tp in range(TP_COUNT):
                for bs in READ_BS_LIST:
                    futs.append(ex.submit(run_read, tp, bs))
            for tp in range(TP_COUNT):
                for job in range(WRITE_JOBS_PER_TP):
                    futs.append(ex.submit(run_write, tp, job))
            for f in futs:
                results.append(f.result())
    else:
        with ThreadPoolExecutor(max_workers=TP_COUNT) as ex:
            futs = []
            for tp in range(TP_COUNT):
                for bs in READ_BS_LIST:
                    futs.append(ex.submit(run_read, tp, bs))
            for f in futs:
                results.append(f.result())
        with ThreadPoolExecutor(max_workers=TP_COUNT * WRITE_JOBS_PER_TP) as ex:
            futs = []
            for tp in range(TP_COUNT):
                for job in range(WRITE_JOBS_PER_TP):
                    futs.append(ex.submit(run_write, tp, job))
            for f in futs:
                results.append(f.result())

    summary = summarize(results)
    out = RESULT_DIR / "summary.json"
    out.write_text(json.dumps(summary, indent=2))
    print(f"\\nHelper source: {HELPER_SRC}\\nHelper bin: {HELPER_BIN}")
    print(f"\\nResults directory:\\n  {RESULT_DIR}")
    print_summary(summary)


if __name__ == "__main__":
    main()
