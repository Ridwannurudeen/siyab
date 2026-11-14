import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intel import feed, labels, render


ACCOUNT = "existing.near"
NEW_KEY = "ed25519:new-key"
OLD_KEY = "ed25519:old-key"
SOURCE = {"url": "fixture", "mode": "replay"}


def reduced_block(height, timestamp_ms, receipts):
    return {"height": height, "hash": f"h{height}", "timestamp_ns": str(timestamp_ms * 1_000_000),
            "action_reducer_version": feed.ACTION_REDUCER_VERSION, "receipts": receipts}


def reduced_receipt(receiver, actions, predecessor="system", ok=True, receipt_id="r"):
    return {"receipt_id": receipt_id, "tx_hash": f"t-{receipt_id}", "receiver_id": receiver,
            "predecessor_id": predecessor, "ok": ok, "actions": actions}


def key_add(account=ACCOUNT, key=NEW_KEY):
    return reduced_receipt(account, [{"type": "AddKey", "public_key": key, "permission": "FullAccess"}],
                           receipt_id=f"key-{key}")


def transfer(account=ACCOUNT, destination="new-wallet.near", amount=None):
    amount = feed.MIN_OUTFLOW_YOCTO if amount is None else amount
    return reduced_receipt(destination, [{"type": "Transfer", "deposit": str(amount)}],
                           predecessor=account, receipt_id=f"transfer-{destination}")


def create_account(account, height=1):
    return reduced_receipt(account, [{"type": "CreateAccount"}, {"type": "Transfer", "deposit": "0"}],
                           predecessor="funder.near", receipt_id=f"create-{height}-{account}")


class FullAccessKeyOutflowTest(unittest.TestCase):
    def setUp(self):
        self.context = feed.Context(labels.load())
        self.now = 1_800_000_000_000

    def run_block(self, state, height, timestamp_ms, receipts):
        return feed.signals(reduced_block(height, timestamp_ms, receipts), state, self.context, SOURCE)

    def test_key_then_outflow_raises_unlabelled_event_with_recorded_destination_freshness(self):
        state = feed.new_state()
        self.assertEqual(self.run_block(state, 9, self.now, [create_account("fresh-destination.near")]), [])
        self.assertEqual(self.run_block(state, 10, self.now + 500, [key_add()]), [])
        events = self.run_block(state, 11, self.now + 1_000, [transfer(destination="fresh-destination.near")])
        [event] = events
        self.assertEqual(event["signal"], "full_access_key_then_outflow")
        self.assertEqual(event["labels"], [])
        self.assertEqual(event["source"]["mode"], "replay")
        self.assertEqual(event["destination"], "fresh_dust_funded")
        self.assertEqual(event["other_account_id"], "fresh-destination.near")
        self.assertEqual(event["drain_kind"], "transfer")
        self.assertEqual(event["amount_yocto"], str(feed.MIN_OUTFLOW_YOCTO))
        self.assertFalse(event["lockout"])
        self.assertIn(NEW_KEY, event["detail"])
        self.assertIn("fresh-destination.near", event["detail"])
        self.assertEqual(state["candidates"]["full_access_key_then_outflow"], 1)

    def test_created_destination_above_the_dust_threshold_is_other(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        created_and_funded = reduced_receipt("funded-destination.near", [
            {"type": "CreateAccount"}, {"type": "Transfer", "deposit": str(feed.MIN_OUTFLOW_YOCTO)}
        ], predecessor=ACCOUNT, receipt_id="create-and-drain")
        [event] = self.run_block(state, 11, self.now + 1_000, [created_and_funded])
        self.assertEqual(event["destination"], "other")

    def test_outflow_without_key_add_does_not_raise(self):
        self.assertEqual(self.run_block(feed.new_state(), 10, self.now, [transfer()]), [])

    def test_outflow_after_timestamp_window_does_not_raise_and_prunes_key(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        events = self.run_block(state, 11, self.now + feed.KEY_OUTFLOW_WINDOW_MS + 1, [transfer()])
        self.assertEqual(events, [])
        self.assertNotIn(ACCOUNT, state["pending_key_additions"])

    def test_key_add_on_fresh_account_does_not_raise_or_count_candidate(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [create_account(ACCOUNT)])
        self.assertEqual(self.run_block(state, 11, self.now + 1, [key_add()]), [])
        self.assertEqual(state["candidates"]["full_access_key_then_outflow"], 0)

    def test_small_transfer_does_not_raise_and_keeps_key_pending(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        self.assertEqual(self.run_block(state, 11, self.now + 1,
                                        [transfer(amount=feed.MIN_OUTFLOW_YOCTO - 1)]), [])
        self.assertIn(ACCOUNT, state["pending_key_additions"])

    def test_saved_state_mid_window_still_raises(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            resumed = feed.load_state(state_path)
        [event] = self.run_block(resumed, 11, self.now + 1_000, [transfer()])
        self.assertEqual(event["signal"], "full_access_key_then_outflow")

    def test_delete_account_drain_has_null_amount(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        deletion = reduced_receipt(ACCOUNT, [{"type": "DeleteAccount", "beneficiary_id": "beneficiary.near"}],
                                   predecessor=ACCOUNT, receipt_id="delete")
        [event] = self.run_block(state, 11, self.now + 1, [deletion])
        self.assertEqual(event["drain_kind"], "delete_account")
        self.assertIsNone(event["amount_yocto"])
        self.assertIn("beneficiary.near", event["detail"])
        self.assertEqual(event["other_account_id"], "beneficiary.near")

    def test_delete_key_of_another_key_marks_lockout(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        delete_key = reduced_receipt(ACCOUNT, [{"type": "DeleteKey", "public_key": OLD_KEY}],
                                     predecessor=ACCOUNT, receipt_id="lockout")
        [event] = self.run_block(state, 11, self.now + 1_000, [delete_key, transfer()])
        self.assertTrue(event["lockout"])

    def test_implicit_destination_is_recorded(self):
        state = feed.new_state()
        self.run_block(state, 10, self.now, [key_add()])
        [event] = self.run_block(state, 11, self.now + 1, [transfer(destination="a" * 64)])
        self.assertEqual(event["destination"], "implicit")

    def test_old_reduced_fixtures_cannot_raise_the_new_signal(self):
        state = feed.new_state()
        old_block = reduced_block(10, self.now, [key_add()])
        old_block.pop("action_reducer_version")
        self.assertEqual(feed.signals(old_block, state, self.context, SOURCE), [])
        old_block = reduced_block(11, self.now + 1, [transfer()])
        old_block.pop("action_reducer_version")
        self.assertEqual(feed.signals(old_block, state, self.context, SOURCE), [])

    def test_old_state_drops_created_accounts_on_the_next_write(self):
        state = feed.new_state()
        state["created_accounts"] = {"legacy.near": {"created_ms": self.now, "height": 9}}
        for field in ("pending_key_additions", "receipts", "candidates"):
            state.pop(field)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            loaded = feed.load_state(state_path)
            feed.apply(reduced_block(10, self.now, [key_add()]), loaded, self.context, SOURCE,
                       events_dir=Path(tmp) / "events")
            feed._write_json(state_path, loaded, indent=2)
            written = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("created_accounts", loaded)
        self.assertNotIn("created_accounts", written)
        self.assertEqual(loaded["receipts"], 1)
        self.assertIn("candidates", loaded)
        self.assertIn("full_access_key_then_outflow", loaded["events"])
        self.assertIn("pending_key_additions", loaded)

    def test_action_reducer_retains_delete_key_and_delete_account(self):
        self.assertEqual(feed._action({"DeleteKey": {"public_key": OLD_KEY}}),
                         {"type": "DeleteKey", "public_key": OLD_KEY})
        self.assertEqual(feed._action({"DeleteAccount": {"beneficiary_id": "beneficiary.near"}}),
                         {"type": "DeleteAccount", "beneficiary_id": "beneficiary.near"})

    def test_run_marks_source_as_replay_and_apply_does_not_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocks = root / "blocks"
            blocks.mkdir()
            block = reduced_block(10, self.now, [key_add(), transfer()])
            (blocks / "10.json").write_text(json.dumps(block), encoding="utf-8")
            with mock.patch("intel.feed.alerts.send") as send:
                state, paths = feed.run(10, 1, False, blocks_dir=blocks, state_path=root / "state.json",
                                        events_dir=root / "events", store=labels.load())
                send.assert_not_called()
            [event] = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        self.assertEqual(event["source"]["mode"], "replay")
        self.assertEqual(event["labels"], [])
        self.assertEqual(state["events"]["full_access_key_then_outflow"], 1)

    def test_follow_marks_source_as_follow(self):
        raw = {"block": {"header": {"height": 10, "hash": "h10", "timestamp_nanosec": str(self.now * 1_000_000)}},
               "shards": [{"receipt_execution_outcomes": [
                   {"receipt": {"receipt_id": "key", "predecessor_id": "system", "receiver_id": ACCOUNT,
                                "receipt": {"Action": {"actions": [{"AddKey": {"public_key": NEW_KEY,
                                    "access_key": {"permission": "FullAccess"}}}]}}},
                    "tx_hash": "key-tx", "execution_outcome": {"outcome": {"status": {"SuccessValue": ""}}}},
                   {"receipt": {"receipt_id": "transfer", "predecessor_id": ACCOUNT,
                                "receiver_id": "dest.near", "receipt": {"Action": {"actions": [
                                    {"Transfer": {"deposit": str(feed.MIN_OUTFLOW_YOCTO)}}]}}},
                    "tx_hash": "transfer-tx", "execution_outcome": {"outcome": {"status": {"SuccessValue": ""}}}},
               ]}]}

        class FetcherStub:
            requests = 0

            def final_block(self):
                return raw

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("intel.feed.rpc.Client.post", return_value={"transfers": [], "resume_token": None}), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("the test opened the network")):
            root = Path(tmp)
            state, _ = feed.follow(start=10, fetcher=FetcherStub(), store=labels.load(),
                                   state_path=root / "state.json", events_dir=root / "events",
                                   event_blocks_dir=root / "event-blocks", max_blocks=1, sleep=lambda _: None,
                                   clock=lambda: self.now / 1000)
            event_paths = list((root / "events").glob("*.json"))
            [event] = [json.loads(path.read_text(encoding="utf-8")) for path in event_paths]
        self.assertEqual(state["mode"], "follow")
        self.assertEqual(event["source"]["mode"], "follow")


class StoredEventCounterpartyTest(unittest.TestCase):
    def test_old_outflow_detail_supplies_counterparty_without_changing_evidence(self):
        for drain_kind, drain in (("transfer", "transfer of 1000000000000000000000000 yoctoNEAR"),
                                  ("delete_account", "DeleteAccount")):
            with self.subTest(drain_kind=drain_kind):
                event = {"signal": "full_access_key_then_outflow", "subject": ACCOUNT,
                         "detail": f"full access key {NEW_KEY} added to {ACCOUNT}; {drain} to "
                                   "beneficiary.near within 1000 ms", "drain_kind": drain_kind,
                         "receipt_id": "stored-receipt", "tx_hash": "stored-transaction",
                         "observed": {"block_height": 11, "timestamp_ms": 1800000001000},
                         "source": SOURCE}
                before = feed.evidence.canonical_bytes(event)
                event_id = feed.evidence.evidence_hash(event)
                with tempfile.TemporaryDirectory() as tmp:
                    path = feed.evidence.write(event, Path(tmp))
                    original = path.read_bytes()
                    document = json.loads(original)
                    panel = render._feed_panel([(document, event_id)])
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(path.stem, event_id)
                self.assertIn('<q class="data">beneficiary.near</q>', panel)
                self.assertIn('evidence/' + event_id, panel)
                self.assertEqual(feed.evidence.canonical_bytes(document), before)
                self.assertEqual(feed.evidence.evidence_hash(document), event_id)
                self.assertNotIn("other_account_id", document)

    def test_missing_or_ambiguous_details_do_not_guess_a_counterparty(self):
        prefix = f"full access key {NEW_KEY} added to {ACCOUNT}; "
        for detail in (None, "beneficiary.near", prefix + "DeleteAccount to a.near or b.near within 1 ms",
                       prefix + "DeleteAccount to a.near within 1 ms; DeleteAccount to b.near within 2 ms",
                       prefix.replace(ACCOUNT, "another.near") + "DeleteAccount to a.near within 1 ms"):
            with self.subTest(detail=detail):
                event = {"signal": "full_access_key_then_outflow", "subject": ACCOUNT,
                         "detail": detail, "drain_kind": "delete_account"}
                self.assertIsNone(feed.event_counterparty(event))
        event = {"signal": "another_signal", "subject": ACCOUNT, "drain_kind": "delete_account",
                 "detail": prefix + "DeleteAccount to a.near within 1 ms"}
        self.assertIsNone(feed.event_counterparty(event))

    def test_recorded_counterparty_remains_authoritative(self):
        event = {"other_account_id": "recorded.near", "signal": "full_access_key_then_outflow",
                 "subject": ACCOUNT, "drain_kind": "delete_account",
                 "detail": f"full access key {NEW_KEY} added to {ACCOUNT}; DeleteAccount to old.near within 1 ms"}
        self.assertEqual(feed.event_counterparty(event), "recorded.near")


class FeedFPRateTest(unittest.TestCase):
    def test_review_csv_is_keyed_by_event_hash_and_reruns_keep_filled_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / "events"
            events.mkdir()
            event = {"version": "1", "kind": "feed_event", "signal": "full_access_key_then_outflow",
                     "subject": ACCOUNT, "detail": "key then transfer", "labels": [], "receipt_id": "r",
                     "tx_hash": "t", "observed": {"block_height": 12, "timestamp_ms": 10},
                     "source": {"mode": "replay"}, "lockout": True, "drain_kind": "transfer",
                     "amount_yocto": str(feed.MIN_OUTFLOW_YOCTO), "destination": "fresh_dust_funded"}
            event_path = feed.evidence.write(event, events)
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"blocks": 4, "skipped_heights": [2], "receipts": 3,
                                             "candidates": {"full_access_key_then_outflow": 1}}), encoding="utf-8")
            review_path = root / "review.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                feed.fprate(events, state_path, review_path=review_path)
            with review_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["event_sha256"], event_path.stem)
            self.assertEqual(rows[0]["review"], "")
            rows[0]["review"] = "false_positive"
            with review_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                feed.fprate(events, state_path, review_path=review_path)
            with review_path.open(encoding="utf-8", newline="") as handle:
                rerun_rows = list(csv.DictReader(handle))
        self.assertEqual(rerun_rows[0]["review"], "false_positive")
        self.assertIn("heights walked: 5", output.getvalue())
        self.assertIn("receipts seen: 3", output.getvalue())
        self.assertIn("false_positive_rate total: lower 1/1, upper 1/1", output.getvalue())
        self.assertIn("false_positive_rate destination=fresh_dust_funded: lower 1/1, upper 1/1",
                      output.getvalue())


if __name__ == "__main__":
    unittest.main()
