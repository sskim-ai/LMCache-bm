# LMCache-bm

Microbenchmark for the disk I/O pattern observed from a specific LMCache + vLLM workload.

This benchmark is intentionally narrower than a full LMCache replay:

- can use `O_DIRECT`, but keeps it configurable with `USE_ODIRECT=0|1`
- models **read-only** and **write-only** cases
- focuses on disk-visible I/O shape rather than end-to-end LMCache behavior

## Modeled workload

### Read

- per TP, read 32 files sequentially
- sleep after completing one full pass over the files
- use Linux `O_DIRECT` when `USE_ODIRECT=1`; on macOS the helper uses `F_NOCACHE`
- reuse one aligned read buffer

### Write

- per TP, run 4 write workers
- each worker writes one file, then sleeps
- use Linux `O_DIRECT` when `USE_ODIRECT=1`; on macOS the helper uses `F_NOCACHE`
- reuse one aligned zero-filled write buffer
- do **not** call `fsync()`

## Relation to LMCache

This matches the low-level disk I/O shape of `LocalDiskBackend` reasonably well for the selected workload:

- file-per-chunk storage
- full-file reads and writes
- default 4-way disk write thread pool
- no `fsync()` on the write path

It is not intended to be a full behavioral model of LMCache:

- the fixed 32-file read pass and post-I/O delays are workload assumptions, not backend invariants
- LMCache uses per-object buffers rather than reusing one buffer across files
- mixed read/write scheduling, cache metadata, eviction, and distributed FS adapter behavior are out of scope

## Upstream alignment notes

Verified against upstream `LMCache/LMCache` at `c9742db4`:

| LMCache behavior | Current benchmark behavior | Gap / risk | Suggested knob |
| --- | --- | --- | --- |
| `LocalDiskBackend` stores one `.pt` file per `CacheEngineKey`; filename is `key.to_string().replace("/", "-") + ".pt"`. | Stores one `.bin` file per synthetic TP/file/job slot. | File count and per-file I/O shape match, but filenames do not model key namespace/cardinality. | Add generated LMCache-like key filenames when validating metadata/path behavior. |
| Disk writes are async task submissions through a default 4-thread `LocalDiskWorker`; each key becomes one write task. | Per TP, `WRITE_JOBS_PER_TP=4` independent writer processes. | Similar low-level concurrency, but scheduling differs from LMCache priority executor and request burst pattern. | Sweep `WRITE_JOBS_PER_TP`; compare against LMCache `disk_io_threads`. |
| `use_odirect` defaults to false and is enabled only via extra config; unaligned sizes fall back to buffered I/O. | `USE_ODIRECT=1` by default, now configurable with `USE_ODIRECT=0`. | Always-direct runs can overstate mismatch if the real deployment uses buffered I/O or unaligned chunks. | Record `USE_ODIRECT` in results and run both modes. |
| `write_file()` uses `open(..., "wb")` or `os.open(..., O_DIRECT)` and does not call `fsync()`, temp file, or rename. | Writes directly to target files and does not call `fsync()`. | Good match for durability semantics. | Keep no-fsync default; add explicit fsync mode only for sensitivity testing. |
| `read_file()` reads the whole object into a preallocated `MemoryObj.byte_array`; batched async read loops paths sequentially inside a task. | Reads full file(s) sequentially per read worker into a reused aligned buffer. | Full-file read shape matches; buffer lifetime/object allocation pressure is not modeled. | Add optional per-file allocation mode if CPU staging overhead matters. |
| `contains()`/`batched_contains()` are in-memory metadata checks and prefix-hit based; no disk stat on hit path. | Benchmark skips metadata lookup and starts I/O directly. | Does not model hit/miss control-flow or prefix misses. | Add a trace/replay mode with hit-prefix length and miss gaps if needed. |

## Result files

Each run writes:

- `summary.json` with config, per-worker metrics, per-TP totals, and total read/write summaries.
- `workers.csv` with one row per worker for spreadsheet or plotting workflows.

Useful environment knobs:

- `USE_ODIRECT=0|1` selects buffered I/O vs Linux `O_DIRECT` / macOS `F_NOCACHE`.
- `HELPER_SRC=/path/to/lmcache_helper.c` and `HELPER_BIN=/path/to/lmcache_helper` can separate source and compiled helper locations.

## Files

- `lmcache_bm.py` — benchmark driver
- `lmcache_helper.c` — low-level `O_DIRECT` read/write helper
