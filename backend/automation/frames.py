"""In-memory store of live-browser screenshot frames, one bucket per job.

Not persisted, not shared across processes, gone on restart.

Bounded on purpose: frames are JPEG blobs and a single live-view run can
produce dozens of them, so an always-on server that never evicted would
leak memory steadily. Two caps:

- MAX_FRAMES_PER_JOB drops the *oldest* frames within a job. Frames are
  keyed by a monotonically increasing index (not list position) precisely
  so this eviction doesn't renumber anything — `count()` keeps returning
  "how many frames have ever been captured", so a viewer asking for
  index `count - 1` always gets the newest frame, and an evicted older
  index just returns None (the API already 404s on that).
- MAX_JOBS_RETAINED drops the least-recently-started job's bucket
  entirely once too many jobs have accumulated.
"""

MAX_FRAMES_PER_JOB = 60
MAX_JOBS_RETAINED = 20

_store: dict[str, dict[int, bytes]] = {}
_next_index: dict[str, int] = {}


def start_job(job_id: str) -> None:
    _store[job_id] = {}
    _next_index[job_id] = 0

    # dicts keep insertion order, so the first keys are the oldest jobs.
    while len(_store) > MAX_JOBS_RETAINED:
        oldest = next(iter(_store))
        _store.pop(oldest, None)
        _next_index.pop(oldest, None)


def add_frame(job_id: str, frame_bytes: bytes) -> None:
    bucket = _store.setdefault(job_id, {})
    index = _next_index.get(job_id, 0)
    bucket[index] = frame_bytes
    _next_index[job_id] = index + 1

    while len(bucket) > MAX_FRAMES_PER_JOB:
        bucket.pop(min(bucket), None)


def count(job_id: str) -> int:
    """Total frames ever captured for this job — NOT len(retained), so
    index arithmetic stays stable across eviction."""
    return _next_index.get(job_id, 0)


def get_frame(job_id: str, index: int):
    return _store.get(job_id, {}).get(index)
