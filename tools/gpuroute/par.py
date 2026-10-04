"""Parallelism of the CPU stages of the pipeline: environment flags and a thread pool.

    KAT_PAR=0          disable (enabled by default; verified bit-exact against the serial path:
                       board-item basket, EDT/label in threads, vectorised fill, grouped SDF)
    KAT_PAR_WORKERS=N  number of threads (default min(8, cores): one sub-raster EDT takes ~1 GB)
    KAT_PAR_CHECK=1    compute both the serial and the parallel path and compare them
                       bit-exact (slow; for verification)

scipy.ndimage.distance_transform_edt and label release the GIL (measured: 4 threads give
3.2x / 3.1x), so a ThreadPoolExecutor is enough - no multiprocessing, no pcbnew pickling.
Flags are re-read on every call: check_par.py toggles them inside one process.
"""
import os
from concurrent.futures import ThreadPoolExecutor


def enabled():
    # KAT_PAR: '0' disables the threaded CPU stages
    return os.environ.get('KAT_PAR', '1') != '0'


def check():
    # KAT_PAR_CHECK: '1' runs serial and parallel paths and compares them
    return os.environ.get('KAT_PAR_CHECK', '0') == '1'


def workers():
    # KAT_PAR_WORKERS: thread count (0 or unset = min(8, cores))
    try:
        n = int(os.environ.get('KAT_PAR_WORKERS', '0'))
    except ValueError:
        n = 0
    return n if n > 0 else min(8, os.cpu_count() or 1)


def map_threads(fn, items):
    """[fn(item)] in the order of items; sequential when the flag is off."""
    items = list(items)
    if not enabled() or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(workers(), len(items))) as ex:
        return list(ex.map(fn, items))


def edt_many(dist_map, tasks):
    """tasks is a list of (mask, step, H, W); returns [dist_map(*t)] in the same order."""
    return map_threads(lambda t: dist_map(*t), tasks)
