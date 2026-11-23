"""Offline backfill tests against the repository feed and RPC modules."""

import base64
import contextlib
import functools
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import feed
from scripts import prefetch_follow


def _no_network(*args, **kwargs):
    raise AssertionError("a test tried a network request")


def _isolated_sigint(test):
    system_root = os.environ.get("SYSTEMROOT")

    @functools.wraps(test)
    def run(self):
        # Real signals must not share another test's handler or asyncio wakeup socket.
        probe = (
            "import io, sys, tempfile, unittest\n"
            "sys.path[:0] = sys.argv[1:3]\n"
            "from test_prefetch_follow import PrefetchFollowTest\n"
            "tempfile.gettempdir()\n"
            "name = sys.argv[3]\n"
            "setattr(PrefetchFollowTest, name, getattr(PrefetchFollowTest, name).__wrapped__)\n"
            "output = io.StringIO()\n"
            "result = unittest.TextTestRunner(stream=output).run(PrefetchFollowTest(name))\n"
            "if not result.wasSuccessful():\n"
            "    sys.stderr.write(output.getvalue())\n"
            "sys.exit(not result.wasSuccessful())\n"
        )
        environment = {"GATE_FIXTURES_MODE": "off", "TEMP": tempfile.gettempdir(),
                       "TMP": tempfile.gettempdir()}
        if system_root is not None:
            environment["SYSTEMROOT"] = system_root
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe, str(Path(__file__).resolve().parent),
             str(Path(__file__).resolve().parent.parent), test.__name__],
            capture_output=True, text=True, timeout=30, env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    return run


REAL_FETCH = rpc._fetch
START = 1000
NEAR = 10**24


def _receipt(rid, predecessor, receiver, actions):
    return {
        "receipt": {
            "receipt_id": rid,
            "predecessor_id": predecessor,
            "receiver_id": receiver,
            "receipt": {"Action": {"actions": actions}},
        },
        "tx_hash": f"tx-{rid}",
        "execution_outcome": {"outcome": {"status": {"SuccessValue": ""}}},
    }


def _block(height):
    """A deterministic block; some heights carry receipts that raise full_access_key_then_outflow."""
    receipts = []
    if height % 7 == 0:
        receipts.append(
            _receipt(
                f"add{height}",
                f"acct{height}.near",
                f"acct{height}.near",
                [
                    {
                        "AddKey": {
                            "public_key": f"ed25519:K{height}",
                            "access_key": {"nonce": 0, "permission": "FullAccess"},
                        }
                    }
                ],
            )
        )
    if height % 7 == 2:
        source = f"acct{height - 2}.near"
        receipts.append(
            _receipt(
                f"out{height}", source, "sink.near", [{"Transfer": {"deposit": str(5 * NEAR)}}]
            )
        )
    if height % 5 == 0:
        receipts.append(
            _receipt(
                f"new{height}",
                "near",
                f"fresh{height}.near",
                ["CreateAccount", {"Transfer": {"deposit": "1000"}}],
            )
        )
    if height % 11 == 0:
        receipts.append(
            _receipt(
                f"dep{height}",
                f"c{height}.near",
                f"c{height}.near",
                [
                    {
                        "DeployContract": {
                            "code": base64.b64encode(b"\0asm" + bytes([height % 256])).decode()
                        }
                    },
                    {"FunctionCall": {"method_name": "new", "args": "", "deposit": "0", "gas": 1}},
                ],
            )
        )
    return {
        "block": {
            "header": {
                "height": height,
                "hash": f"hash{height}",
                "timestamp_nanosec": str(1_700_000_000_000_000_000 + height * 10**9),
            }
        },
        "shards": [{"receipt_execution_outcomes": receipts}],
    }


class Server:
    """Synthetic neardata shared by every client: one height answers 404, one 429 once, one maybe 500,
    some always 429. finals is the final height each final read answers, the last one repeating."""

    def __init__(
        self,
        final,
        missing=START + 5,
        throttled=START + 10,
        broken=None,
        persistent=(),
        finals=None,
    ):
        self.finals = list(finals or [final])
        self.missing, self.throttled, self.broken = missing, throttled, broken
        self.persistent = set(persistent)
        self.lock = threading.Lock()
        self.log = []
        self.throttled_once = False
        self.on_persistent = None
        self.on_final = None

    def get(self, path):
        if path == "/v0/last_block/final" and self.on_final is not None:
            self.on_final()
        with self.lock:
            self.log.append(path)
            if path == "/v0/last_block/final":
                return _block(self.finals.pop(0) if len(self.finals) > 1 else self.finals[0])
            height = int(path.rsplit("/", 1)[1])
            if height == self.throttled and not self.throttled_once:
                self.throttled_once = True
                raise rpc.HttpError(429, path)
            if height == self.missing:
                raise rpc.HttpError(404, path)
            if height == self.broken:
                raise rpc.HttpError(500, path)
            persistent = height in self.persistent
        if persistent:
            if self.on_persistent is not None:
                self.on_persistent(height)
            raise rpc.HttpError(429, path)
        return _block(height)

    def block_heights(self):
        with self.lock:
            return [int(p.rsplit("/", 1)[1]) for p in self.log if p.startswith("/v0/block/")]


class Store:
    assertions = []
    sha256 = "0" * 64


class RecordingLimiter:
    def __init__(self):
        self.count = 0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            self.count += 1


def _noop(seconds):
    return None


def _run(fetcher, root, max_blocks, start=START, stopped=lambda: False):
    root = Path(root)
    state, walked = feed.follow(
        start=start,
        fetcher=fetcher,
        store=Store(),
        state_path=root / "state.json",
        events_dir=root / "events",
        event_blocks_dir=root / "event-blocks",
        max_blocks=max_blocks,
        sleep=_noop,
        clock=lambda: 1_800_000_000.0,
        stopped=stopped,
    )
    return state, walked


def _tree(root):
    root = Path(root)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "state.json"
    }


def _state(root):
    return json.loads((Path(root) / "state.json").read_text(encoding="utf-8"))


def _prefetch(
    server,
    stop_height,
    limiter=None,
    next_height=START,
    backoff_wait=0,
    window=prefetch_follow.WINDOW,
    workers=prefetch_follow.WORKERS,
):
    return prefetch_follow.PrefetchFetcher(
        stop_height,
        next_height,
        client_factory=lambda: server,
        limiter=limiter or RecordingLimiter(),
        backoff_wait=backoff_wait,
        window=window,
        workers=workers,
    )


class PrefetchFollowTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "off"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        network = mock.patch.object(rpc, "_fetch", side_effect=_no_network)
        network.start()
        self.addCleanup(network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plain_dir = Path(self.temp.name) / "plain"
        self.prefetch_dir = Path(self.temp.name) / "prefetch"
        self.state_dir = Path(self.temp.name) / "state"

    def _compare(self, final, stop_height, max_blocks):
        plain_server = Server(final)
        _, plain_walked = _run(
            feed.Fetcher(plain_server, sleep=_noop, max_requests=None), self.plain_dir, max_blocks
        )
        server = Server(final)
        limiter = RecordingLimiter()
        fetcher = _prefetch(server, stop_height, limiter)
        try:
            state, walked = _run(fetcher, self.prefetch_dir, max_blocks)
        finally:
            fetcher.close()
        self.assertEqual(walked, plain_walked)
        self.assertEqual(_state(self.prefetch_dir), _state(self.plain_dir))
        self.assertEqual(_tree(self.prefetch_dir), _tree(self.plain_dir))
        self.assertTrue(_tree(self.plain_dir), "the synthetic blocks raised no event")
        self.assertEqual(limiter.count, len(server.log))
        self.assertEqual(fetcher.unused_requests(), len(server.log) - state["requests"])
        return state, server, plain_server

    def _main(self, argv, server=None, factory=None, cursor=START - 1, fetcher_class=None):
        self.state_dir.mkdir(exist_ok=True)
        if cursor is not None:
            state = feed.new_state()
            state["last_height"] = cursor
            (self.state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        saved = (prefetch_follow.make_client, prefetch_follow.PrefetchFetcher, feed.labels.load)
        prefetch_follow.make_client = factory or (lambda: server)
        if fetcher_class is not None:
            prefetch_follow.PrefetchFetcher = fetcher_class
        feed.labels.load = Store
        os.environ["FEED_STATE_DIR"] = str(self.state_dir)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = prefetch_follow.main(argv)
        finally:
            prefetch_follow.make_client, prefetch_follow.PrefetchFetcher, feed.labels.load = saved
            os.environ.pop("FEED_STATE_DIR")
        return code, out.getvalue(), err.getvalue()

    def test_outputs_and_requests_match_plain_fetcher(self):
        stop = START + 79
        state, server, plain_server = self._compare(
            final=START + 200, stop_height=stop, max_blocks=80
        )
        self.assertEqual(state["last_height"], stop)
        self.assertIn(START + 5, state["skipped_heights"])
        self.assertTrue(server.throttled_once)
        self.assertEqual(state["requests"], len(plain_server.log))
        self.assertLessEqual(max(server.block_heights()), stop)
        self.assertEqual(sorted(set(server.block_heights())), [h for h in range(START, stop + 1)])

    def test_prefetch_stays_below_final_height(self):
        final = START + 15
        state, server, _ = self._compare(final=final, stop_height=START + 500, max_blocks=16)
        self.assertEqual(state["last_height"], final)
        self.assertLess(max(server.block_heights()), final)

    def test_non_404_error_propagates_after_earlier_heights(self):
        server = Server(START + 200, broken=START + 20)
        fetcher = _prefetch(server, START + 100)
        try:
            with self.assertRaises(rpc.HttpError) as caught:
                _run(fetcher, self.prefetch_dir, 100)
        finally:
            fetcher.close()
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(_state(self.prefetch_dir)["last_height"], START + 19)

    def test_no_request_past_stop_height(self):
        server = Server(START + 200)
        fetcher = _prefetch(server, START + 3)
        try:
            self.assertEqual(fetcher.get(f"/v0/block/{START}")["block"]["header"]["height"], START)
            with self.assertRaises(feed.FeedError):
                fetcher.get(f"/v0/block/{START + 4}")
        finally:
            fetcher.close()
        heights = sorted(server.block_heights())
        self.assertEqual(heights[0], START)
        self.assertLessEqual(set(heights), {START, START + 1, START + 2, START + 3})
        self.assertEqual(fetcher.requests, 1)

    def test_fresh_state_is_refused(self):
        server = Server(200, missing=None, throttled=None)
        code, out, err = self._main(
            ["--max-blocks", "1", "--stop-height", "100"], server=server, cursor=None
        )
        self.assertEqual(code, 1)
        self.assertEqual(err, prefetch_follow.NO_CURSOR + "\n")
        self.assertEqual(out, "")
        self.assertEqual(server.log, [])
        state_path = self.state_dir / "state.json"
        self.assertTrue(not state_path.exists() or _state(self.state_dir)["last_height"] != 200)

    def test_stop_height_equal_to_final_is_processed_like_sequential(self):
        plain_server = Server(START + 9, missing=None, throttled=None)
        plain_state, plain_walked = _run(
            feed.Fetcher(plain_server, sleep=_noop, max_requests=None), self.plain_dir, 10
        )
        server = Server(START + 9, missing=None, throttled=None)
        code, out, err = self._main(
            ["--max-blocks", "10", "--stop-height", str(START + 9)], server=server
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines()[0],
            f"followed {plain_walked} heights to height {START + 9}, lag 0 blocks",
        )
        self.assertEqual(plain_walked, 10)
        self.assertEqual(_state(self.state_dir)["last_height"], plain_state["last_height"])

    def test_final_block_past_stop_height_is_never_processed(self):
        stop = START + 6
        server = Server(None, missing=None, throttled=None, finals=[START + 3, stop + 1])
        fetcher = _prefetch(server, stop)
        try:
            state, walked = _run(
                fetcher,
                self.prefetch_dir,
                100,
                stopped=lambda: fetcher.at_boundary() or len(server.log) > 50,
            )
        finally:
            fetcher.close()
        self.assertEqual(state["last_height"], stop)
        self.assertEqual(walked, 7)
        self.assertEqual(fetcher.walked, 7)

    def test_held_lock_refuses_and_is_kept(self):
        self.state_dir.mkdir()
        lock = self.state_dir / prefetch_follow.LOCK_NAME
        lock.write_text("1\n", encoding="utf-8")
        server = Server(START + 200)
        code, out, err = self._main(
            ["--max-blocks", "5", "--stop-height", str(START + 9)], server=server
        )
        self.assertEqual((code, out, err), (1, "", prefetch_follow.LOCK_HELD + "\n"))
        self.assertEqual(server.log, [])
        self.assertTrue(lock.exists())

    def test_concurrent_backfill_on_one_dir_is_refused(self):
        server = Server(START + 200)
        second = []

        def start_second_run():
            if not second:
                second.append(None)
                with contextlib.redirect_stderr(io.StringIO()) as captured:
                    second[0] = (
                        prefetch_follow.main(
                            ["--max-blocks", "1", "--stop-height", str(START + 9)]
                        ),
                        captured.getvalue(),
                    )

        server.on_final = start_second_run
        code, out, err = self._main(
            ["--max-blocks", "100", "--stop-height", str(START + 9)], server=server
        )
        self.assertEqual(second[0], (1, prefetch_follow.LOCK_HELD + "\n"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(_state(self.state_dir)["last_height"], START + 9)
        self.assertFalse((self.state_dir / prefetch_follow.LOCK_NAME).exists())

    def test_changed_cursor_is_refused_before_a_request(self):
        self.prefetch_dir.mkdir()
        state = feed.new_state()
        state["last_height"] = START + 4
        (self.prefetch_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        server = Server(START + 200)
        fetcher = _prefetch(server, START + 100)
        try:
            with self.assertRaises(feed.FeedError):
                _run(fetcher, self.prefetch_dir, 10)
        finally:
            fetcher.close()
        self.assertEqual(server.block_heights(), [])

    def test_stopped_direct_read_counts_only_started_requests(self):
        for during_acquire in (False, True):
            with self.subTest(during_acquire=during_acquire):
                server = Server(START + 200)
                limiter = mock.Mock()
                fetcher = _prefetch(server, START + 100, limiter=limiter)
                if during_acquire:
                    limiter.acquire.side_effect = fetcher.stop_event.set
                else:
                    fetcher.stop_event.set()
                try:
                    with self.assertRaises(prefetch_follow.Stopped):
                        fetcher.get(prefetch_follow.FINAL_PATH)
                finally:
                    fetcher.close()
                    for thread in fetcher.threads:
                        thread.join(timeout=2)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(limiter.acquire.call_count, int(during_acquire))
                self.assertEqual(server.log, [])
                self.assertEqual(fetcher.requests, 0)
                self.assertEqual(fetcher.unused_requests(), 0)

    @_isolated_sigint
    def test_sigint_during_an_active_download(self):
        release = threading.Event()
        active = threading.Event()
        waiting = threading.Condition()
        sigint_at, download = [], []

        def blocked(height):
            download.append(threading.current_thread())
            with waiting:
                active.set()
                if not waiting.wait_for(release.is_set, timeout=3):
                    raise rpc.HttpError(500, "rescue")

        def interrupt():
            active.wait()
            with waiting:
                if not release.is_set():
                    sigint_at.append(time.monotonic())
                    signal.raise_signal(signal.SIGINT)

        server = Server(START + 200, missing=None, throttled=None, persistent={START + 4})
        server.on_persistent = blocked
        fetcher_class = functools.partial(
            prefetch_follow.PrefetchFetcher, window=1, backoff_wait=0.05
        )
        previous = signal.getsignal(signal.SIGINT)
        threads = set(threading.enumerate())
        sender = threading.Thread(target=interrupt, daemon=True)
        sender.start()
        try:
            code, out, err = self._main(
                ["--max-blocks", "100", "--stop-height", str(START + 50)],
                server=server,
                fetcher_class=fetcher_class,
            )
            returned_at = time.monotonic()
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(sigint_at), 1)
            self.assertLess(returned_at - sigint_at[0], 2.5)
            self.assertFalse(release.is_set())
            self.assertEqual(len(download), 1)
            self.assertTrue(download[0].is_alive())
            self.assertEqual(signal.getsignal(signal.SIGINT), previous)
            self.assertEqual(
                out,
                f"followed 4 heights to height {START + 3}, lag 197 blocks\n"
                "prefetch: 1 speculative requests not used\n",
            )
            self.assertEqual(server.block_heights(), list(range(START, START + 5)))
            state = _state(self.state_dir)
            self.assertEqual(
                (state["last_height"], state["requests"], state["lag_blocks"]),
                (START + 3, 5, 197),
            )
        finally:
            with waiting:
                release.set()
                active.set()
                waiting.notify_all()
            for thread in set(threading.enumerate()) - threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
        self.assertFalse(sender.is_alive())
        self.assertEqual(_state(self.state_dir), state)

    def test_request_deadline_abandons_a_trickling_response(self):
        client = rpc.Client(feed.NEARDATA_URL, "off", kind="http")
        limited = prefetch_follow.LimitedClient(
            client, RecordingLimiter(), threading.Event(), lambda: None
        )
        reply = mock.MagicMock()
        reply.__enter__.return_value = reply
        reply.read1.return_value = b" "
        with (
            mock.patch.object(prefetch_follow, "REQUEST_DEADLINE_S", 1),
            mock.patch.object(rpc, "_fetch", REAL_FETCH),
            mock.patch.object(rpc, "_open_with_deadline", return_value=reply) as opener,
            mock.patch.object(rpc.budget, "reserve", return_value=False),
            mock.patch.object(rpc.time, "monotonic", side_effect=[0, 0, 0, 2]),
        ):
            with self.assertRaises(TimeoutError):
                limited.get("/v0/block/1")
        opener.assert_called_once()
        reply.read1.assert_called_once_with(65536)

    def test_close_stops_every_worker(self):
        fetcher = _prefetch(Server(START + 200), START + 100, workers=5)
        fetcher.close()
        for thread in fetcher.threads:
            thread.join(timeout=2)
        self.assertEqual([thread.is_alive() for thread in fetcher.threads], [False] * 5)

    def test_close_returns_during_persistent_429(self):
        server = Server(START + 200, missing=None, throttled=None, persistent={START + 1})
        fetcher = _prefetch(server, START + 100, backoff_wait=0.05, window=2)
        fetcher.get(f"/v0/block/{START}")
        deadline = time.monotonic() + 5
        while START + 1 not in server.block_heights() and time.monotonic() < deadline:
            time.sleep(0.01)
        closer = threading.Thread(target=fetcher.close, daemon=True)
        began = time.monotonic()
        closer.start()
        closer.join(timeout=5)
        self.assertFalse(closer.is_alive())
        self.assertLess(time.monotonic() - began, 3)
        time.sleep(0.3)
        settled = len(server.block_heights())
        time.sleep(0.6)
        self.assertEqual(len(server.block_heights()), settled)

    @_isolated_sigint
    def test_sigint_stops_after_the_height_in_hand(self):
        sigint_at = []

        def on_persistent(height):
            if not sigint_at:
                sigint_at.append(time.monotonic())
                signal.raise_signal(signal.SIGINT)
            elif time.monotonic() - sigint_at[0] > 3:
                raise rpc.HttpError(500, "rescue")

        server = Server(START + 200, missing=None, throttled=None, persistent={START + 4})
        server.on_persistent = on_persistent
        fetcher_class = functools.partial(
            prefetch_follow.PrefetchFetcher, window=1, backoff_wait=0.05
        )
        previous = signal.getsignal(signal.SIGINT)
        threads = set(threading.enumerate())
        try:
            code, out, err = self._main(
                ["--max-blocks", "100", "--stop-height", str(START + 50)],
                server=server,
                fetcher_class=fetcher_class,
            )
            returned_at = time.monotonic()
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(len(sigint_at), 1)
            self.assertLess(returned_at - sigint_at[0], 3)
            self.assertEqual(signal.getsignal(signal.SIGINT), previous)
            self.assertRegex(
                out,
                rf"\Afollowed 4 heights to height {START + 3}, lag 197 blocks\n"
                r"prefetch: [0-9]+ speculative requests not used\n\Z",
            )
            state = _state(self.state_dir)
            self.assertEqual(
                (state["last_height"], state["requests"], state["lag_blocks"]),
                (START + 3, 5, 197),
            )
        finally:
            for thread in set(threading.enumerate()) - threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(_state(self.state_dir), state)
        heights = server.block_heights()
        self.assertEqual(heights[:4], list(range(START, START + 4)))
        self.assertEqual(set(heights[4:]), {START + 4})
        # A stopped retry may finish before the caller observes the stop event.
        unused = int(out.splitlines()[1].split()[1])
        self.assertIn(unused, (0, len(heights) - 4))

    @_isolated_sigint
    def test_sigint_with_a_completed_retry_future(self):
        real_wait = prefetch_follow.wait
        stopped = []

        def completed_wait(futures, timeout):
            while not futures[0].done():
                time.sleep(0.01)
            if isinstance(futures[0].result()[2], prefetch_follow.Stopped):
                stopped.append(futures[0])
            return real_wait(futures, timeout=timeout)

        with mock.patch.object(prefetch_follow, "wait", side_effect=completed_wait):
            self.test_sigint_stops_after_the_height_in_hand.__wrapped__(self)
        self.assertEqual(len(stopped), 1)

    @_isolated_sigint
    def test_sigint_during_a_retry_limiter_wait(self):
        real_raise = signal.raise_signal
        real_signal = signal.signal
        real_acquire = prefetch_follow.Limiter.acquire
        previous = signal.getsignal(signal.SIGINT)
        retry_entered, release_retry = threading.Event(), threading.Event()
        handled, cancelled = threading.Event(), threading.Event()
        acquisitions, senders = [], []

        def acquire(limiter):
            acquisitions.append(None)
            if len(acquisitions) == 7:
                retry_entered.set()
                release_retry.wait()
            else:
                real_acquire(limiter)

        def install(signum, handler):
            if signum == signal.SIGINT and callable(handler) and handler is not previous:
                def observed(signum, frame):
                    handler(signum, frame)
                    handled.set()

                return real_signal(signum, observed)
            return real_signal(signum, handler)

        def defer_signal(signum):
            def send():
                retry_entered.wait()
                if cancelled.is_set():
                    return
                real_raise(signum)
                handled.wait()
                release_retry.set()

            sender = threading.Thread(target=send, daemon=True)
            senders.append(sender)
            sender.start()

        try:
            with (
                mock.patch.object(prefetch_follow.Limiter, "acquire", new=acquire),
                mock.patch.object(signal, "signal", side_effect=install),
                mock.patch.object(signal, "raise_signal", side_effect=defer_signal),
            ):
                self.test_sigint_with_a_completed_retry_future.__wrapped__(self)
            self.assertEqual(len(acquisitions), 7)
            self.assertEqual(len(senders), 1)
            self.assertTrue(handled.is_set())
        finally:
            cancelled.set()
            release_retry.set()
            retry_entered.set()
            handled.set()
            for sender in senders:
                sender.join(timeout=2)
                self.assertFalse(sender.is_alive())

    def test_request_path_errors_never_print_the_header(self):
        def factory():
            client = mock.Mock()
            client.get.side_effect = ValueError("invalid header: synthetic-header-value")
            return client

        code, out, err = self._main(
            ["--max-blocks", "5", "--stop-height", str(START + 9)], factory=factory
        )
        self.assertEqual((code, out, err), (1, "", "stopped: ValueError\n"))

    def test_limiter_spacing_with_a_fake_clock(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        limiter = prefetch_follow.Limiter(clock=lambda: now[0], sleep=sleep)
        starts = []
        for _ in range(1000):
            limiter.acquire()
            starts.append(now[0])
        for index in range(len(starts) - 150):
            self.assertGreaterEqual(starts[index + 150] - starts[index], 60 - 1e-9)

    def test_key_and_budget_environment_never_authorize_requests(self):
        dummy_file = Path(self.temp.name) / "dummy-provider.txt"
        dummy_file.write_text("dummy-provider-value", encoding="utf-8")
        database = Path(self.temp.name) / "budget.sqlite3"
        server = Server(START + 200, missing=None, throttled=None)
        captured = []
        read_text = Path.read_text
        key_reads = []

        def read(path, *args, **kwargs):
            if path == dummy_file:
                key_reads.append(path)
                return "dummy-provider-value"
            return read_text(path, *args, **kwargs)

        def fetch(url, body, extra_headers=None, record_http_errors=()):
            captured.append(dict(extra_headers or {}))
            return server.get(url.removeprefix(feed.NEARDATA_URL))

        with (
            mock.patch.dict(os.environ, {
                "FASTNEAR_API_KEY_FILE": str(dummy_file),
                "SIYAB_PROVIDER_BUDGET_DB": str(database),
            }),
            mock.patch.object(Path, "read_text", autospec=True, side_effect=read),
            mock.patch.object(rpc, "_fetch", side_effect=fetch),
        ):
            code, out, err = self._main(
                ["--max-blocks", "3", "--stop-height", str(START + 2)],
                factory=prefetch_follow.make_client,
            )
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(database.exists())
        self.assertGreaterEqual(len(captured), 4)
        self.assertTrue(all(
            name.lower() != "authorization" for headers in captured for name in headers
        ), captured)
        self.assertEqual(key_reads, [])

    def test_rate_above_150_is_refused(self):
        error = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(self.state_dir)}),
            contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as caught,
        ):
            prefetch_follow.main([
                "--max-blocks", "1", "--stop-height", "1", "--per-minute", "151",
            ])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--per-minute must be at most 150", error.getvalue())

    def test_limiter_holds_across_threads(self):
        limiter = prefetch_follow.Limiter(per_minute=150)
        starts, lock = [], threading.Lock()

        def worker():
            for _ in range(2):
                limiter.acquire()
                with lock:
                    starts.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        starts.sort()
        self.assertGreaterEqual(starts[-1] - starts[0], 7 * 0.4 * 0.8)

    def test_main_clamps_to_stop_height(self):
        server = Server(START + 200)
        code, out, err = self._main(
            ["--max-blocks", "100", "--stop-height", str(START + 9)],
            server=server,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            f"followed 10 heights to height {START + 9}, lag 191 blocks\n"
            "prefetch: 0 speculative requests not used\n",
        )
        self.assertLessEqual(max(server.block_heights()), START + 9)
        self.assertEqual(_state(self.state_dir)["requests"], len(server.log))

    def test_workers_and_rate_flags_reach_the_fetcher(self):
        seen = {}
        real = prefetch_follow.PrefetchFetcher

        class Recording(real):
            def __init__(self, *a, **k):
                seen["workers"] = k.get("workers")
                seen["interval"] = k["limiter"].interval
                super().__init__(*a, **k)

        server = Server(START + 50)
        code, out, err = self._main(
            ["--max-blocks", "100", "--stop-height", str(START + 4), "--workers", "8", "--per-minute", "120"],
            server=server,
            fetcher_class=Recording,
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, {"workers": 8, "interval": 60 / 120})
        self.assertLessEqual(max(server.block_heights()), START + 4)

    def test_zero_workers_or_rate_is_refused(self):
        for flag in ("--workers", "--per-minute"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(io.StringIO()):
                    prefetch_follow.main(["--max-blocks", "1", "--stop-height", "1", flag, "0"])
            self.assertEqual(caught.exception.code, 2)

    def test_make_client_is_keyless_and_explicitly_off(self):
        with (
            mock.patch.dict(os.environ, {
                "FASTNEAR_API_KEY_FILE": str(Path(self.temp.name) / "provider.txt"),
                "GATE_FIXTURES_MODE": "replay",
            }),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("the key was read")) as reads,
        ):
            client = prefetch_follow.make_client()
        reads.assert_not_called()
        self.assertEqual(client.url, feed.NEARDATA_URL)
        self.assertEqual(client.mode, "off")
        self.assertEqual(client.kind, "http")
        self.assertEqual(client.headers, {})

    def test_sleep_skips_intervals_and_keeps_final_wait(self):
        began = time.monotonic()
        prefetch_follow.follow_sleep(0.1)
        self.assertLess(time.monotonic() - began, 0.05)
        began = time.monotonic()
        prefetch_follow.follow_sleep(1)
        self.assertGreaterEqual(time.monotonic() - began, 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
