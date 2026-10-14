"""Memory-only regression tests: no sockets, files, native open, or notifications."""
import copy
import importlib.util
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('reminder_concurrency_tests', ROOT / 'reminder-daemon.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def snapshot(sequence=1, boot='boot-a', host='http://127.0.0.1:19387', ids=('h1',)):
    return dict(bridge_id='bridge-a', boot_id=boot, sequence=sequence,
                host_url=host, pending=[dict(session_id='hs', interaction_id=i,
                                            kind='permission') for i in ids])


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.errors = []
        self.threads = []
        self.releases = []
        for name, value in (
            ('lock', threading.RLock()), ('pending', {}), ('session_tasks', {}),
            ('harness_bridges', {}),
            ('config', {'harness': {'enabled': True}, 'feishu': {}}),
            ('save_state', Mock(return_value=True)), ('log', Mock()),
            ('notify', Mock(return_value=True)), ('notify_feishu', Mock(return_value=True)),
            ('callback_ready', Mock(return_value=False)),
            ('in_quiet_hours', Mock(return_value=False)), ('now_ts', Mock(return_value=1000)),
        ):
            p = patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        # Registered last: unblock/join all workers before restoring module globals.
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        for release in self.releases:
            release.set()
        for worker in self.threads:
            worker.join(3)
        self.assertFalse(any(worker.is_alive() for worker in self.threads), 'worker did not finish')
        self.assertEqual(self.errors, [], 'worker raised an exception')

    def worker(self, callback):
        done = threading.Event()
        def run():
            try:
                callback()
            except BaseException as error:
                self.errors.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=run, daemon=True)
        self.threads.append(worker)
        worker.start()
        return done

    def block_notification(self, name):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def block(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release blocked notification')
            return True
        getattr(d, name).side_effect = block
        return entered, release

    @staticmethod
    def wb_payload():
        return dict(event='PermissionRequest', session_id='wb', interaction_id='w1')

    def slow_channel_does_not_block_state(self, channel):
        entered, release = self.block_notification(channel)
        creator = self.worker(lambda: d.create_pending(self.wb_payload()))
        self.assertTrue(entered.wait(2), 'WorkBuddy did not reach notification I/O')
        outcomes = []
        def reconcile_and_resolve():
            outcomes.append(d.reconcile_harness(snapshot()))
            outcomes.append(d.resolve(dict(self.wb_payload(), event='PostToolUse')))
            outcomes.append(d.reconcile_harness(snapshot(sequence=2, ids=())))
        state_work = self.worker(reconcile_and_resolve)
        # No sleeps: notification stays blocked until all state work finishes.
        self.assertTrue(state_work.wait(2), 'slow WorkBuddy I/O blocked reconcile/resolve')
        self.assertEqual(self.errors, [])
        self.assertEqual(outcomes[0], 1)
        self.assertEqual(len(outcomes[1]), 1)
        self.assertEqual(outcomes[2], 0)
        self.assertEqual(d.pending, {})
        release.set()
        self.assertTrue(creator.wait(2))
        if channel == 'notify':
            d.notify_feishu.assert_not_called()

    def test_slow_workbuddy_local_notification_allows_harness_reconcile_and_resolve(self):
        self.slow_channel_does_not_block_state('notify')

    def test_slow_workbuddy_feishu_notification_allows_harness_reconcile_and_resolve(self):
        self.slow_channel_does_not_block_state('notify_feishu')

    def competing_claimants(self, winner, loser):
        key, rec, _ = d.create_pending(self.wb_payload(), defer_notification=True)
        entered, release = self.block_notification('notify')
        first_worker = self.worker(lambda: d.send_reminder(key, rec, **winner))
        self.assertTrue(entered.wait(2), 'winning claimant did not reach I/O')
        loser_worker = self.worker(lambda: d.send_reminder(key, rec, **loser))
        self.assertTrue(loser_worker.wait(2), 'losing claimant blocked behind notification I/O')
        self.assertEqual(self.errors, [])
        self.assertEqual(rec['reminder_count'], 1)
        self.assertEqual(rec['next_reminder_at'], 1000 + d.REPEAT_DELAYS[0])
        self.assertEqual(d.notify.call_count, 1)
        release.set()
        self.assertTrue(first_worker.wait(2))
        d.notify.assert_called_once()
        d.notify_feishu.assert_called_once()

    def test_first_claims_before_stale_scheduled_candidate_without_duplicate(self):
        self.competing_claimants({'first': True}, {'scheduled': True})

    def test_scheduled_claims_before_delayed_first_without_duplicate(self):
        self.competing_claimants({'scheduled': True}, {'first': True})

    def test_stale_duplicate_retired_boot_and_other_host_cannot_change_state_or_lease(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            d.reconcile_harness(snapshot(sequence=2))
        original = copy.deepcopy((d.pending, d.harness_bridges))
        for stale in (snapshot(sequence=1, ids=()), snapshot(sequence=2, ids=()),
                      snapshot(sequence=3, host='http://127.0.0.1:29387', ids=()),
                      snapshot(boot='boot-b', ids=())):
            with self.subTest(payload=stale), patch.object(d.time, 'monotonic', return_value=101):
                with self.assertRaises(ValueError):
                    d.reconcile_harness(stale)
                self.assertEqual((d.pending, d.harness_bridges), original)
        with patch.object(d.time, 'monotonic', return_value=100 + d.LEASE_SECONDS):
            d.reconcile_harness(snapshot(boot='boot-b', ids=()))
        replacement = copy.deepcopy((d.pending, d.harness_bridges))
        with patch.object(d.time, 'monotonic', return_value=100 + 2 * d.LEASE_SECONDS):
            with self.assertRaises(ValueError):
                d.reconcile_harness(snapshot(sequence=999, boot='boot-a'))
        self.assertEqual((d.pending, d.harness_bridges), replacement)
        self.assertEqual(d.pending, {})
        d.notify.assert_not_called()
        d.notify_feishu.assert_not_called()


if __name__ == '__main__':
    unittest.main()
