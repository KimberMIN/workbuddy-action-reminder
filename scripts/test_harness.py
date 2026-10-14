"""Offline Harness integration regressions; only ephemeral local HTTP is real.
Notifications, native open and Lark CLI are mocked. No old TestCase imports.
"""
import copy
import http.client
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('reminder_harness_tests', ROOT/'reminder-daemon.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
import harness_bridge as bridge


SNAPSHOT_SEQUENCES = itertools.count(1)


def snapshot(bridge_id='bridge-a', ids=('request-1',), host_url='http://127.0.0.1:19387', **kwargs):
    result = {'bridge_id': bridge_id, 'host_url': host_url,
              'boot_id': 'test-boot', 'sequence': next(SNAPSHOT_SEQUENCES),
              'pending': [{'session_id': 'same-session', 'interaction_id': iid,
                           'kind': 'permission', 'tool_name': 'Write',
                           'task_title': '修复测试项目'} for iid in ids]}
    result.update(kwargs)
    return result


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='harness-reminder-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (
            ('STATE_FILE', str(self.root/'state.json')),
            ('CONFIG_FILE', str(self.root/'config.json')),
            ('LOG_FILE', str(self.root/'daemon.log')),
            ('pending', {}), ('session_tasks', {}), ('harness_bridges', {}),
            ('stop_evt', threading.Event()), ('ACK_CALLBACK_READY', False),
            ('callback_process', None),
            ('config', {'harness': {'enabled': True}, 'feishu': {'enabled': True,
                        'target_type': 'user', 'target_id': 'ou_test'}}),
        ):
            p = patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(d, 'notify', return_value=True)
        self.local = p.start()
        self.addCleanup(p.stop)
        p = patch.object(d, 'notify_feishu', return_value=True)
        self.feishu = p.start()
        self.addCleanup(p.stop)

    def reconcile(self, payload=None):
        d.reconcile_harness(payload or snapshot())
        return next(r for r in d.pending.values() if r.get('source') == bridge.SOURCE)

    def callback(self, rec, action='ack_reminder', **extra):
        value = dict(action=action, reminder_token=rec['reminder_token'], generation=rec['generation'])
        value.update(extra)
        return dict(type='card.action.trigger', action_tag='button', operator_id='ou_test',
                    action_value=json.dumps(value))

    def test_disabled_default_and_explicit_enable_only(self):
        d.load_config()
        self.assertIs(d.config['harness']['enabled'], False)
        for enabled in (False, 'true', 1, None):
            d.config['harness'] = {'enabled': enabled}
            with self.assertRaises(ValueError):
                d.reconcile_harness(snapshot())
            self.assertFalse(d.pending)
            self.assertFalse(d.harness_bridges)

    def test_host_and_bridge_request_identity_isolation(self):
        wb = d.create_pending(dict(event='PermissionRequest', session_id='same-session',
                                   interaction_id='request-1'))[1]
        a = self.reconcile()
        d.reconcile_harness(snapshot('bridge-b', host_url='http://127.0.0.1:29387'))
        self.assertEqual(len(d.pending), 3)
        self.assertEqual(len({r['key'] for r in d.pending.values()}), 3)
        removed = d.resolve(dict(event='PostToolUse', session_id='same-session', interaction_id='request-1'))
        self.assertEqual(removed, [wb['key']])
        self.assertIn(a['key'], d.pending)
        d.reconcile_harness(snapshot('bridge-a', ids=()))
        self.assertEqual(len(d.pending), 1)
        self.assertEqual(next(iter(d.pending.values()))['bridge_id'], 'bridge-b')

    def test_snapshot_removal_exact_and_new_ids_independent(self):
        d.reconcile_harness(snapshot(ids=('one', 'two')))
        before = {r['request_identity'][-1]: r for r in d.pending.values()}
        d.reconcile_harness(snapshot(ids=('two', 'three')))
        after = {r['request_identity'][-1]: r for r in d.pending.values()}
        self.assertEqual(set(after), {'two', 'three'})
        self.assertIs(after['two'], before['two'])
        self.assertNotIn(before['one']['key'], d.pending)
        self.assertNotEqual(after['two']['reminder_token'], after['three']['reminder_token'])

    def test_snapshot_deferred_notification_and_duplicates(self):
        rec = self.reconcile()
        self.local.assert_not_called()
        self.feishu.assert_not_called()
        d.send_reminder(rec['key'], rec, first=True)
        self.assertEqual(rec['reminder_count'], 1)
        self.local.assert_called_once()
        self.feishu.assert_called_once()
        token, generation = rec['reminder_token'], rec['generation']
        self.reconcile()
        self.assertEqual((rec['reminder_token'], rec['generation']), (token, generation))
        self.assertEqual(self.feishu.call_count, 1)

    def test_ack_restart_dedupe_and_snapshot_disappearance(self):
        rec = self.reconcile()
        d.send_reminder(rec['key'], rec)
        with patch.object(d, 'callback_ready', return_value=True):
            self.assertTrue(d.acknowledge_card(self.callback(rec)))
        token, generation = rec['reminder_token'], rec['generation']
        d.pending = {}
        d.harness_bridges = {}
        d.load_state()
        rec = self.reconcile()
        self.assertEqual((rec['status'], rec['reminder_token'], rec['generation']),
                         ('ACKED', token, generation))
        d.send_reminder(rec['key'], rec)
        self.assertEqual(self.feishu.call_count, 1)
        d.reconcile_harness(snapshot(ids=()))
        rec = self.reconcile()
        self.assertEqual(rec['status'], 'WAITING')
        self.assertNotEqual(rec['generation'], generation)

    def test_lease_silence_pauses_without_resolving_and_recovers(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            rec = self.reconcile()
            d.send_reminder(rec['key'], rec)
        count, due = rec['reminder_count'], rec['next_reminder_at']
        with patch.object(d.time, 'monotonic', return_value=100 + bridge.LEASE_SECONDS):
            self.assertFalse(d.harness_record_live(rec))
            d.send_reminder(rec['key'], rec)
            self.assertEqual((rec['reminder_count'], rec['next_reminder_at']), (count, due))
            self.assertIn(rec['key'], d.pending)
            self.reconcile()
            self.assertTrue(d.harness_record_live(rec))
            d.send_reminder(rec['key'], rec)
        self.assertEqual(self.feishu.call_count, 2)

    def test_heartbeat_does_not_revive_another_bridge(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            a = self.reconcile()
            d.reconcile_harness(snapshot('bridge-b'))
            b = next(r for r in d.pending.values() if r['bridge_id'] == 'bridge-b')
        with patch.object(d.time, 'monotonic', return_value=120):
            self.reconcile()
            self.assertTrue(d.harness_record_live(a))
            self.assertFalse(d.harness_record_live(b))
            d.send_reminder(b['key'], b)
            self.feishu.assert_not_called()

    def test_scheduler_resumes_due_record_only_after_fresh_snapshot(self):
        rec = self.reconcile()
        rec['next_reminder_at'] = d.now_ts() - 1
        d.harness_bridges = {}
        def tick():
            stop = Mock()
            stop.is_set.side_effect = [False, False, True]
            with patch.object(d, 'stop_evt', stop), patch.object(d.time, 'sleep'):
                d.scheduler_loop()
        tick()
        self.feishu.assert_not_called()
        self.reconcile()
        tick()
        self.feishu.assert_called_once()
        self.assertEqual(rec['reminder_count'], 1)

    def test_restart_has_no_persisted_live_lease(self):
        rec = self.reconcile()
        d.save_state()
        d.pending = {}
        d.harness_bridges = {}
        d.load_state()
        rec = d.pending[rec['key']]
        self.assertFalse(d.harness_record_live(rec))
        d.send_reminder(rec['key'], rec)
        self.feishu.assert_not_called()
        self.reconcile()
        self.assertTrue(d.harness_record_live(rec))

    def test_invalid_snapshot_never_deletes_or_refreshes_lease(self):
        self.reconcile()
        state = copy.deepcopy(d.pending)
        leases = copy.deepcopy(d.harness_bridges)
        bad = [[], {}, snapshot(pending=None), snapshot(bridge_id='../bad'),
               snapshot(host_url='http://evil.invalid'), snapshot(ids=('x', 'x')),
               snapshot(ids=tuple(str(i) for i in range(513)))]
        for field, value in [('session_id', ''), ('interaction_id', 'bad\n'),
                             ('kind', 'approve'), ('task_title', 1), ('question', 'x'*4097)]:
            item = snapshot()['pending'][0]
            item[field] = value
            bad.append(snapshot(pending=[item]))
        # A valid first entry must not create/resolve before an invalid tail.
        bad.append(snapshot(pending=snapshot(ids=('new',))['pending'] + [None]))
        for payload in bad:
            with self.subTest(payload=str(payload)[:80]):
                with self.assertRaises(ValueError):
                    d.reconcile_harness(payload)
                self.assertEqual(d.pending, state)
                self.assertEqual(d.harness_bridges, leases)

    def assert_snapshot_rejected_unchanged(self, payload):
        pending, leases = copy.deepcopy(d.pending), copy.deepcopy(d.harness_bridges)
        with self.assertRaises(ValueError):
            d.reconcile_harness(payload)
        self.assertEqual(d.pending, pending, 'rejected snapshot changed pending state')
        self.assertEqual(d.harness_bridges, leases, 'rejected snapshot refreshed or changed lease')

    def test_required_boot_and_positive_safe_sequence_validation(self):
        self.reconcile()
        for field, values in (
            ('boot_id', ('', '../bad', 'with space', '中文', '\ud800', 'a'*65, None, 1)),
            ('sequence', (0, -1, True, False, 1.5, '1', None, 9007199254740992)),
        ):
            missing = snapshot(ids=())
            missing.pop(field)
            self.assert_snapshot_rejected_unchanged(missing)
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_snapshot_rejected_unchanged(snapshot(ids=(), **{field: value}))
        # Upper bound is inclusive and every legal boot character is accepted.
        with patch.object(d.time, 'monotonic', return_value=1000000000000):
            d.reconcile_harness(snapshot(boot_id='ABC_xyz-012', sequence=9007199254740991))
        self.assertEqual(d.harness_bridges['bridge-a']['sequence'], 9007199254740991)

    def test_out_of_order_duplicate_and_expired_replays_never_renew_or_clear(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            self.reconcile(snapshot(sequence=10))
        for clock in (105, 115, 130):
            with patch.object(d.time, 'monotonic', return_value=clock):
                for sequence in (9, 10):
                    self.assert_snapshot_rejected_unchanged(snapshot(ids=(), sequence=sequence))
        with patch.object(d.time, 'monotonic', return_value=131):
            # Same boot can recover an expired lease, but only with a newer seq.
            d.reconcile_harness(snapshot(ids=(), sequence=11))
        self.assertFalse(d.pending)
        self.assertEqual(d.harness_bridges['bridge-a']['last_seen'], 131)

    def test_new_boot_requires_expired_lease_and_retired_boot_never_returns(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            rec = self.reconcile(snapshot(boot_id='boot-a', sequence=7))
        token = rec['reminder_token']
        for clock in (100, 114.999):
            with patch.object(d.time, 'monotonic', return_value=clock):
                self.assert_snapshot_rejected_unchanged(snapshot(ids=(), boot_id='boot-b', sequence=1))
        with patch.object(d.time, 'monotonic', return_value=115):
            d.reconcile_harness(snapshot(boot_id='boot-b', sequence=1))
        self.assertEqual(rec['reminder_token'], token)
        self.assertEqual(d.harness_bridges['bridge-a']['boot_id'], 'boot-b')
        for clock in (116, 130, 1000):
            with patch.object(d.time, 'monotonic', return_value=clock):
                self.assert_snapshot_rejected_unchanged(snapshot(ids=(), boot_id='boot-a', sequence=999))
        # Retirement accumulates across multiple replacements.
        with patch.object(d.time, 'monotonic', return_value=131):
            d.reconcile_harness(snapshot(boot_id='boot-c', sequence=1))
        with patch.object(d.time, 'monotonic', return_value=200):
            for boot in ('boot-a', 'boot-b'):
                self.assert_snapshot_rejected_unchanged(snapshot(ids=(), boot_id=boot, sequence=1000))
        # The contract intentionally resets anti-replay memory on daemon restart.
        d.harness_bridges = {}
        with patch.object(d.time, 'monotonic', return_value=201):
            d.reconcile_harness(snapshot(boot_id='boot-a', sequence=1))
        self.assertEqual(rec['reminder_token'], token)

    def test_same_bridge_url_rebinding_rejected_even_after_expiry_or_new_boot(self):
        with patch.object(d.time, 'monotonic', return_value=100):
            self.reconcile(snapshot(sequence=1))
        for clock in (101, 115, 1000):
            with patch.object(d.time, 'monotonic', return_value=clock):
                for boot in ('test-boot', 'new-boot'):
                    self.assert_snapshot_rejected_unchanged(snapshot(
                        ids=(), host_url='http://127.0.0.1:29387', boot_id=boot, sequence=2))
        self.assertEqual(next(iter(d.pending.values()))['host_url'], 'http://127.0.0.1:19387')

    def test_http_replay_rejection_does_not_clear_or_extend_lease(self):
        port = self.http()
        with patch.object(d.time, 'monotonic', return_value=100):
            self.assertEqual(self.request(port, snapshot(sequence=10))[0], 200)
        pending, leases = copy.deepcopy(d.pending), copy.deepcopy(d.harness_bridges)
        with patch.object(d.time, 'monotonic', return_value=105):
            for payload in (snapshot(ids=(), sequence=9), snapshot(ids=(), sequence=10),
                            snapshot(ids=(), boot_id='new-boot', sequence=1),
                            snapshot(ids=(), host_url='http://127.0.0.1:29387', sequence=11)):
                self.assertEqual(self.request(port, payload)[0], 400)
                self.assertEqual(d.pending, pending)
                self.assertEqual(d.harness_bridges, leases)

    def test_invalid_unicode_is_rejected_before_any_reconciliation(self):
        self.reconcile()
        state, leases = copy.deepcopy(d.pending), copy.deepcopy(d.harness_bridges)
        for field in ('session_id', 'interaction_id', 'question', 'task_title'):
            payload = snapshot(ids=('new',))
            payload['pending'][0][field] = '\ud800'
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    d.reconcile_harness(payload)
                self.assertEqual(d.pending, state)
                self.assertEqual(d.harness_bridges, leases)

    def test_harness_notification_uses_brand_root_and_native_bundle(self):
        rec = self.reconcile()
        d.send_reminder(rec['key'], rec)
        self.assertEqual(self.local.call_args.kwargs['app_bundle'], 'com.deepseek.dsh')
        self.assertTrue(self.feishu.call_args.args[0].startswith('DeepSeek Harness｜'))
        self.assertEqual(self.feishu.call_args.kwargs['harness_url'], 'http://127.0.0.1:19387')
        self.assertNotIn('workbuddy://', d.reminder_body(rec, 1))
        self.assertEqual(d.record_link(rec), 'http://127.0.0.1:19387')

    def test_native_open_never_acks_resolves_or_approves(self):
        rec = self.reconcile()
        before = copy.deepcopy(rec)
        with patch.object(d, 'callback_ready', return_value=True), \
                patch.object(d.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertTrue(d.acknowledge_card(self.callback(rec, 'open_reminder')))
            run.assert_called_once_with(['/usr/bin/open', '-b', 'com.deepseek.dsh'],
                                        shell=False, capture_output=True, timeout=10)
            self.assertEqual(rec, before)
            for extra in ({'url': 'http://evil.invalid'}, {'decision': 'approve'}):
                self.assertFalse(d.acknowledge_card(self.callback(rec, 'open_reminder', **extra)))
            self.assertEqual(run.call_count, 1)
        self.assertIn(rec['key'], d.pending)

    def test_native_open_refuses_unsafe_stored_url_and_wrong_operator(self):
        rec = self.reconcile()
        with patch.object(d, 'callback_ready', return_value=True), patch.object(d.subprocess, 'run') as run:
            event = self.callback(rec, 'open_reminder')
            event['operator_id'] = 'ou_other'
            self.assertFalse(d.acknowledge_card(event))
            for url in ('file:///tmp/a', 'https://evil.invalid', 'http://127.0.0.1:19387/approve'):
                rec['host_url'] = url
                self.assertFalse(d.acknowledge_card(self.callback(rec, 'open_reminder')))
            run.assert_not_called()

    def http(self):
        server = d.ThreadingHTTPServer(('127.0.0.1', 0), d.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.addCleanup(cleanup)
        return server.server_port

    def request(self, port, payload, path='/harness/snapshot', headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            data = json.dumps(payload).encode() if raw is None else raw
            h = {'Content-Type': 'application/json'}
            h.update(headers or {})
            conn.request('POST', path, data, h)
            result = conn.getresponse()
            return result.status, json.loads(result.read())
        finally:
            conn.close()

    def test_http_snapshot_optional_token_and_no_synchronous_notification(self):
        port = self.http()
        self.assertEqual(self.request(port, snapshot())[0], 200)
        self.feishu.assert_not_called()
        self.local.assert_not_called()
        d.config['harness']['token'] = 'temporary-test-token'
        for auth in ('', 'Bearer wrong'):
            self.assertEqual(self.request(port, snapshot(ids=()), headers={'Authorization': auth})[0], 403)
            self.assertEqual(len(d.pending), 1)
        self.assertEqual(self.request(port, snapshot(ids=()),
                                     headers={'Authorization': 'Bearer temporary-test-token'})[0], 200)
        self.assertFalse(d.pending)

    def test_http_rejects_origin_size_content_type_and_invalid_json(self):
        port = self.http()
        self.request(port, snapshot())
        for headers, expected in (({'Origin': 'http://evil.invalid'}, 403),
                                  ({'Sec-Fetch-Site': 'cross-site'}, 403),
                                  ({'Content-Type': 'text/plain'}, 415),
                                  ({'Content-Length': '-1'}, 400),
                                  ({'Content-Length': str(1024*1024+1)}, 400)):
            self.assertEqual(self.request(port, snapshot(ids=()), headers=headers)[0], expected)
            self.assertEqual(len(d.pending), 1)
        for raw in (b'[]', b'null', b'{bad', b'\xff'):
            self.assertEqual(self.request(port, None, raw=raw)[0], 400)
            self.assertEqual(len(d.pending), 1)
        old_state = copy.deepcopy(d.pending)
        old_leases = copy.deepcopy(d.harness_bridges)
        invalid_tail = snapshot(pending=snapshot(ids=('new',))['pending'] + [None])
        self.assertEqual(self.request(port, invalid_tail)[0], 400)
        self.assertEqual(d.pending, old_state)
        self.assertEqual(d.harness_bridges, old_leases)
        d.config['harness']['enabled'] = False
        self.assertEqual(self.request(port, snapshot(ids=()))[0], 403)

    def test_http_cannot_inject_harness_via_event_or_ack_route(self):
        port = self.http()
        rec = self.reconcile()
        before = copy.deepcopy(rec)
        self.assertEqual(self.request(port, dict(event='SessionEnd', source=bridge.SOURCE,
                                               bridge_id='bridge-a', session_id='same-session'), path='/event')[0], 400)
        for path in ('/ack', '/approve', '/harness/approve'):
            self.assertEqual(self.request(port, self.callback(rec), path=path)[0], 404)
        self.assertEqual(rec, before)

    def test_http_invalid_event_type_is_bad_request_not_connection_drop(self):
        port = self.http()
        for event in ([], {}, ['PermissionRequest']):
            with self.subTest(event=event):
                self.assertIn(self.request(port, {'event': event}, path='/event')[0], (200, 400))

    def test_status_never_exposes_ack_credentials(self):
        rec = self.reconcile()
        port = self.http()
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        try:
            conn.request('GET', '/status')
            result = conn.getresponse()
            data = result.read().decode()
            self.assertEqual(result.status, 200)
            for secret in (rec['reminder_token'], rec['generation'], 'ack_operator_id'):
                self.assertNotIn(secret, data)
        finally:
            conn.close()


class HarnessCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='harness-card-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.capture = root/'capture.json'
        cli = root/'mock-lark'
        cli.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                       'with open(os.environ["MOCK_CAPTURE"], "w") as f: json.dump(sys.argv[1:], f)\n'
                       'print(json.dumps({"ok": True}))\n')
        cli.chmod(0o755)
        config = root/'config.json'
        config.write_text(json.dumps({'feishu': {'enabled': True, 'target_id': 'ou_test',
                                                'target_type': 'user', 'as': 'bot'}}))
        self.env = dict(HOME=str(root), PATH='/usr/bin:/bin', ACTION_REMINDER_PYTHON=sys.executable,
                        ACTION_REMINDER_CONFIG=str(config), ACTION_REMINDER_LARK_CLI=str(cli),
                        MOCK_CAPTURE=str(self.capture), ACTION_REMINDER_ACK_VALUE=json.dumps(
                            dict(action='ack_reminder', reminder_token='a'*43, generation='b'*32)))

    def card(self, url):
        return subprocess.run(['/bin/bash', str(ROOT/'notify-feishu.sh'), 'DeepSeek Harness', '需要确认', url],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_root_card_uses_callbacks_not_approval_or_unverified_session_route(self):
        result = self.card('http://127.0.0.1:19387')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        card = json.loads(args[args.index('--content') + 1])
        columns = card['body']['elements'][1]['columns']
        actions = [x['elements'][0]['behaviors'][0]['value']['action'] for x in columns]
        self.assertEqual(actions, ['open_reminder', 'ack_reminder'])
        serialized = json.dumps(card, ensure_ascii=False)
        self.assertIn('不批准', serialized)
        self.assertNotIn('workbuddy://', serialized)
        self.assertNotIn('http://127.0.0.1:19387/session', serialized)

    def test_root_url_allowlist(self):
        for url in ('http://127.0.0.1:1', 'http://127.0.0.1:65535'):
            self.assertTrue(bridge.valid_host_url(url))
        for url in ('http://localhost:19387', 'https://127.0.0.1:19387',
                    'http://127.0.0.1:19387/', 'http://127.0.0.1:19387/approve',
                    'http://127.0.0.1:19387?token=SECRET', 'http://127.0.0.1:19387#x',
                    'http://evil.invalid:19387', 'http://user@127.0.0.1:19387',
                    'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'file:///tmp/a'):
            with self.subTest(url=url):
                self.assertFalse(bridge.valid_host_url(url))
                self.assertNotEqual(self.card(url).returncode, 0)
        self.assertFalse(self.capture.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
