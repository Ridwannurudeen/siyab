"""Run intel.feed.follow() with block downloads overlapped by a small thread pool.

Run from the checkout: python scripts/prefetch_follow.py --max-blocks N --stop-height H

follow() still processes every height itself, in order, with the same outputs. Only the
/v0/block/<n> downloads run ahead: the WINDOW heights from the n follow() asks for onward, never at or
past the last final height read and never past --stop-height. The final block is read on the
calling thread, and a final block past --stop-height is never processed. The run continues an
existing state cursor only, and --max-blocks is lowered so the walk ends at --stop-height.
"""

import argparse
import os
import queue
import signal
import sys
import threading
import time
from concurrent.futures import Future, wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gate import rpc  # noqa: E402
from intel import feed  # noqa: E402

WORKERS = 4
WINDOW = 32
REQUESTS_PER_MINUTE = 150
BLOCK_PREFIX = "/v0/block/"
FINAL_PATH = "/v0/last_block/final"
NO_CURSOR = (
    "stopped: the feed state has no last_height; this backfill only continues an existing cursor"
)
LOCK_NAME = "prefetch.lock"
LOCK_HELD = (
    "stopped: another backfill holds prefetch.lock in FEED_STATE_DIR; "
    "remove it by hand only when no backfill runs"
)
REQUEST_DEADLINE_S = 60


class Stopped(Exception):
    """The run was told to stop while a request waited."""


def make_client():
    return rpc.Client(feed.NEARDATA_URL, "off", kind="http")


class Limiter:
    """Request starts spaced 60 / per_minute seconds apart across every thread."""

    def __init__(self, per_minute=REQUESTS_PER_MINUTE, clock=time.monotonic, sleep=time.sleep):
        self.interval = 60 / per_minute
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.next_start = None

    def acquire(self):
        with self.lock:
            now = self.clock()
            if self.next_start is not None and now < self.next_start:
                self.sleep(self.next_start - now)
            self.next_start = self.clock() + self.interval


class LimitedClient:
    """A client whose every request attempt, a 429 retry included, waits for the limiter, is counted,
    is refused once the stop event is set, and is abandoned REQUEST_DEADLINE_S after it starts."""

    def __init__(self, client, limiter, stop_event, count):
        self.client = client
        self.limiter = limiter
        self.stop_event = stop_event
        self.count = count
        self.requests = 0

    def get(self, path):
        if self.stop_event.is_set():
            raise Stopped
        self.limiter.acquire()
        if self.stop_event.is_set():
            raise Stopped
        self.count()
        self.requests += 1
        with rpc.request_deadline(REQUEST_DEADLINE_S):
            return self.client.get(path)


class PrefetchFetcher(feed.Fetcher):
    """A feed.Fetcher whose /v0/block/<n> answers are downloaded ahead by daemon worker threads.

    next_height is the height follow() walks first: the state's last_height plus one."""

    def __init__(
        self,
        stop_height,
        next_height,
        client_factory=None,
        limiter=None,
        backoff_wait=None,
        workers=WORKERS,
        window=WINDOW,
    ):
        self.client_factory = client_factory or make_client
        self.limiter = limiter or Limiter()
        self.stop_event = threading.Event()
        self.attempts = 0
        self.attempts_lock = threading.Lock()
        super().__init__(self._limited(), sleep=self._wait, max_requests=None)
        self.stop_height = stop_height
        self.next_height = next_height
        self.backoff_wait = backoff_wait
        self.window = window
        self.walked = 0
        self.final = None
        self.pending = {}
        self.queue = queue.Queue()
        self.local = threading.local()
        self.threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]
        for thread in self.threads:
            thread.start()

    def _limited(self):
        return LimitedClient(
            self.client_factory(), self.limiter, self.stop_event, self._count
        )

    def _count(self):
        with self.attempts_lock:
            self.attempts += 1

    def _wait(self, seconds):
        if self.stop_event.wait(seconds if self.backoff_wait is None else self.backoff_wait):
            raise Stopped

    def _work(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            height, future = item
            if not future.set_running_or_notify_cancel():
                continue
            if not hasattr(self.local, "fetcher"):
                self.local.fetcher = feed.Fetcher(
                    self._limited(), sleep=self._wait, max_requests=None
                )
            fetcher = self.local.fetcher
            before = fetcher.client.requests
            try:
                answer = fetcher.get(f"{BLOCK_PREFIX}{height}")
            except Exception as error:
                future.set_result((fetcher.client.requests - before, None, error))
            else:
                future.set_result((fetcher.client.requests - before, answer, None))

    def _submit(self, height):
        future = Future()
        self.pending[height] = future
        self.queue.put((height, future))

    def at_boundary(self):
        """True when follow() would next process a final block past --stop-height."""
        return (
            self.final is not None
            and self.final > self.stop_height
            and self.next_height >= self.final
        )

    def get(self, path):
        tail = path[len(BLOCK_PREFIX) :] if path.startswith(BLOCK_PREFIX) else ""
        if not tail.isdecimal():
            if path == FINAL_PATH and self.final is not None and self.next_height == self.final:
                self.next_height = self.final + 1
                self.walked += 1
            direct = feed.Fetcher(self.client, sleep=self._wait, max_requests=None)
            before = self.client.requests
            try:
                answer = direct.get(path)
            finally:
                self.requests += self.client.requests - before
            if path == FINAL_PATH:
                self.final = answer["block"]["header"]["height"]
            return answer
        height = int(tail)
        if height > self.stop_height:
            raise feed.FeedError(f"height {height} is past --stop-height {self.stop_height}")
        if height != self.next_height:
            raise feed.FeedError(
                f"follow asked for height {height}, not the cursor height {self.next_height}"
            )
        self.next_height = height + 1
        for stale in [h for h in self.pending if h < height]:
            self.pending.pop(stale).cancel()
        if height not in self.pending:
            self._submit(height)
        last = self.stop_height if self.final is None else min(self.stop_height, self.final - 1)
        for ahead in range(height + 1, min(height + self.window - 1, last) + 1):
            if ahead not in self.pending and len(self.pending) < self.window:
                self._submit(ahead)
        future = self.pending.pop(height)
        while not wait([future], timeout=0.5).done:
            if self.stop_event.is_set():
                future.cancel()
                raise Stopped
        used, answer, error = future.result()
        self.requests += used
        if error is None or (isinstance(error, rpc.HttpError) and error.status == 404):
            self.walked += 1
        if error is not None:
            raise error
        return answer

    def unused_requests(self):
        with self.attempts_lock:
            return self.attempts - self.requests

    def close(self):
        """Stop every worker without waiting on a request in flight."""
        self.stop_event.set()
        for future in self.pending.values():
            future.cancel()
        self.pending.clear()
        for _ in self.threads:
            self.queue.put(None)


def follow_sleep(seconds):
    """Skip follow()'s request interval waits; keep its 1 s waits at the final height."""
    if seconds >= 1:
        time.sleep(seconds)


def positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="intel feed --follow with prefetched block downloads"
    )
    parser.add_argument("--max-blocks", type=int, required=True)
    parser.add_argument("--stop-height", type=int, required=True)
    parser.add_argument("--workers", type=positive_int, default=WORKERS)
    parser.add_argument("--per-minute", type=positive_int, default=REQUESTS_PER_MINUTE)
    args = parser.parse_args(argv)
    if args.per_minute > REQUESTS_PER_MINUTE:
        parser.error(f"--per-minute must be at most {REQUESTS_PER_MINUTE}")
    state_dir = Path(os.environ.get("FEED_STATE_DIR", feed.FEED_DIR))
    lock_path = state_dir / LOCK_NAME
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(LOCK_HELD, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(NO_CURSOR, file=sys.stderr)
        return 1
    os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
    os.close(descriptor)
    try:
        return backfill(args, state_dir / "state.json")
    finally:
        lock_path.unlink()


def backfill(args, state_path):
    """The run itself, while this process holds the state directory's lock."""
    last_height = feed.load_state(state_path)["last_height"]
    if last_height is None:
        print(NO_CURSOR, file=sys.stderr)
        return 1
    max_blocks = max(0, min(args.max_blocks, args.stop_height - last_height))
    fetcher = PrefetchFetcher(
        args.stop_height,
        last_height + 1,
        limiter=Limiter(args.per_minute),
        workers=args.workers,
    )
    interrupted = False

    def stop(signum, frame):
        nonlocal interrupted
        interrupted = True
        fetcher.stop_event.set()

    previous = signal.signal(signal.SIGINT, stop)
    try:
        state, walked = feed.follow(
            fetcher=fetcher,
            state_path=state_path,
            max_blocks=max_blocks,
            sleep=follow_sleep,
            stopped=lambda: interrupted or fetcher.at_boundary(),
        )
    except Stopped:
        state, walked = feed.load_state(state_path), fetcher.walked
    except Exception as error:
        status = f" HTTP {error.status}" if isinstance(error, rpc.HttpError) else ""
        print(f"stopped: {type(error).__name__}{status}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
        fetcher.close()
    print(
        f"followed {walked} heights to height {state['last_height']}, lag {state['lag_blocks']} blocks"
    )
    print(f"prefetch: {fetcher.unused_requests()} speculative requests not used")
    return 0


if __name__ == "__main__":
    sys.exit(main())
