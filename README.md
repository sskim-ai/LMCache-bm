# LMCache-bm

Microbenchmark for the disk I/O pattern observed from a specific LMCache + vLLM workload.

This benchmark is intentionally narrower than a full LMCache replay:

- assumes `O_DIRECT`
- models **read-only** and **write-only** cases
- focuses on disk-visible I/O shape rather than end-to-end LMCache behavior

## Modeled workload

### Read

- per TP, read 32 files sequentially
- sleep after completing one full pass over the files
- use `O_DIRECT`
- reuse one aligned read buffer

### Write

- per TP, run 4 write workers
- each worker writes one file, then sleeps
- use `O_DIRECT`
- reuse one aligned zero-filled write buffer
- do **not** call `fsync()`

## Relation to LMCache

This matches the low-level disk I/O shape of `LocalDiskBackend` reasonably well for the selected workload:

- file-per-chunk storage
- full-file reads and writes
- 4-way write parallelism
- no `fsync()` on the write path

It is not intended to be a full behavioral model of LMCache:

- the fixed 32-file read pass and post-I/O delays are workload assumptions, not backend invariants
- LMCache uses per-object buffers rather than reusing one buffer across files
- mixed read/write scheduling, cache metadata, eviction, and distributed FS adapter behavior are out of scope

## Files

- `lmcache_bm.py` — benchmark driver
- `lmcache_helper.c` — low-level `O_DIRECT` read/write helper

