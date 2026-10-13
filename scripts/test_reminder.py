"""离线回归测试：所有通知均被替换，不访问飞书或真实守护进程。"""
import html
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('reminder', ROOT / 'reminder-daemon.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(d, 'STATE_FILE', self.tmp.name + '/state.json'),
                        patch.object(d, 'LOG_FILE', self.tmp.name + '/daemon.log'),
                        patch.object(d, 'notify', return_value=True),
                        patch.object(d, 'notify_feishu', return_value=True)]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        d.pending = {}
        d.session_tasks = {}
        d.config = {}

    def create(self, **kw):
        payload = dict(event='permission_prompt', session_id='session-private', cwd='/Users/private/project')
        payload.update(kw)
        return d.create_pending(payload)[1]

    def test_chinese_title_and_generic_idle(self):
        self.create(message='CodeBuddy is waiting for your input')
        title, body = self.mocks[-1].call_args.args
        self.assertEqual(title, 'WorkBuddy｜需要权限确认')
        for text in ('任务：未获取原始任务名称', '未提供具体操作或问题', '等待时长：', '请返回 WorkBuddy 对应会话处理'):
            self.assertIn(text, body)
        for text in ('CodeBuddy', 'session-private', '/Users/private', '点击'):
            self.assertNotIn(text, body)
        self.assertEqual(len(body.splitlines()), 4)

    def test_idle_completion_and_unknown_events_never_notify(self):
        for event in ('idle_prompt', 'Stop', 'agent_completed', 'auth_success', 'PreToolUse', 'unknown'):
            self.assertEqual(d.create_pending({'event': event, 'session_id': 's'}), (None, None, False))
        self.assertFalse(d.pending)
        self.mocks[-1].assert_not_called()

    def test_call_identity_dedupe_and_parallel_resolve(self):
        a = self.create(event='PermissionRequest', tool_use_id='a', tool_name='Write', tool_input={'file_path': '/private/a', 'content': 'SECRET'})
        self.create(event='permission_prompt', call_id='a', message='不同提醒措辞')
        b = self.create(event='PermissionRequest', tool_use_id='b', tool_name='Edit', tool_input={'file_path': '/private/b'})
        self.assertEqual(self.mocks[-1].call_count, 2)
        self.assertEqual(d.resolve({'event': 'PostToolUse', 'session_id': 'session-private', 'tool_use_id': 'other'}), [])
        self.assertEqual(d.resolve({'event': 'PostToolUse', 'session_id': 'session-private'}), [])
        self.assertEqual(d.resolve({'event': 'PostToolUse', 'session_id': 'session-private', 'tool_use_id': 'a'}), [a['key']])
        self.assertIn(b['key'], d.pending)
        self.assertNotIn('SECRET', Path(d.STATE_FILE).read_text())

    def test_parameter_fingerprint_and_session_isolation(self):
        payload = dict(event='PermissionRequest', tool_name='Bash', tool_input={'command': 'rm /private/a', 'description': '删除旧文件'})
        a = self.create(**payload)
        self.create(**dict(payload, tool_input=dict(reversed(list(payload['tool_input'].items())))))
        b = self.create(**dict(payload, tool_input={'command': 'rm /private/b', 'description': '删除旧文件'}))
        self.assertEqual(self.mocks[-1].call_count, 2)
        self.assertNotEqual(a['key'], b['key'])
        self.assertEqual(d.resolve(dict(payload, event='PostToolUse', session_id='session-private', tool_use_id='later-id')), [a['key']])
        one = self.create(tool_use_id='same', session_id='one')
        two = self.create(tool_use_id='same', session_id='two')
        self.assertNotEqual(one['key'], two['key'])
        d.resolve({'event': 'PermissionDenied', 'session_id': 'one', 'tool_use_id': 'same'})
        self.assertIn(two['key'], d.pending)

    def test_exhausted_and_expired_duplicates_stay_silent(self):
        for status in ('ESCALATED', 'EXPIRED', 'ACKED'):
            rec = self.create(interaction_id=status)
            rec.update(status=status, next_reminder_at=None)
            count = self.mocks[-1].call_count
            self.create(interaction_id=status, message='新措辞')
            self.assertEqual(self.mocks[-1].call_count, count)
            self.assertEqual(rec['status'], status)

    def test_old_idle_state_migrates_without_touching_ack_tokens(self):
        d.pending = {'idle': {'event': 'idle_prompt', 'type': 'idle', 'status': 'WAITING', 'next_reminder_at': 1},
                     'ack': {'event': 'idle_prompt', 'type': 'idle', 'status': 'ACKED', 'next_reminder_at': None, 'generation': 'keep'}}
        d.save_state()
        d.load_state()
        self.assertEqual(d.pending['idle']['status'], 'IGNORED')
        self.assertIsNone(d.pending['idle']['next_reminder_at'])
        self.assertEqual(d.pending['ack']['status'], 'ACKED')
        self.assertEqual(d.pending['ack']['generation'], 'keep')
        self.mocks[-1].assert_not_called()

    def test_legacy_ack_key_migrates_without_resend(self):
        rec = self.create(interaction_id='legacy')
        d.pending.pop(rec['key'])
        rec.update(key='iid:legacy', status='ACKED')
        d.pending['iid:legacy'] = rec
        old_token = rec['reminder_token']
        count = self.mocks[-1].call_count
        updated = self.create(interaction_id='legacy')
        self.assertEqual(updated['reminder_token'], old_token)
        self.assertEqual(updated['status'], 'ACKED')
        self.assertEqual(self.mocks[-1].call_count, count)
        self.assertNotIn('iid:legacy', d.pending)

    def test_legacy_no_id_collision_never_swallows_new_permission(self):
        legacy_key = 'key:' + d.hashlib.md5(b's|PermissionRequest|').hexdigest()[:12]
        d.pending[legacy_key] = {'session_id': 's', 'event': 'PermissionRequest', 'type': 'permission', 'status': 'ACKED'}
        rec = self.create(event='PermissionRequest', session_id='s', tool_use_id='new', tool_name='Write', tool_input={'file_path': '/private/new'})
        self.assertEqual(rec['status'], 'WAITING')
        self.assertIn(legacy_key, d.pending)
        self.mocks[-1].assert_called_once()

    def test_generic_permission_only_once_including_restart(self):
        rec = self.create(message='needs permission')
        self.assertEqual(rec['reminder_count'], 1)
        self.assertIsNone(rec['next_reminder_at'])
        self.assertEqual(rec['status'], 'ESCALATED')
        d.load_state()
        self.create(message='needs permission')
        self.mocks[-1].assert_called_once()
        rec.update(status='WAITING', next_reminder_at=1)
        d.pending[rec['key']] = rec
        d.save_state()
        d.load_state()
        self.assertIsNone(d.pending[rec['key']]['next_reminder_at'])

    def test_ask_user_question_pretool_only_and_permission_deduplicated(self):
        rec = self.create(event='PreToolUse', tool_name='AskUserQuestion', tool_use_id='question',
                          tool_input={'questions': [{'question': '使用浅色还是深色？'}]})
        self.assertIn('待回答问题：使用浅色还是深色？', self.mocks[-1].call_args.args[1])
        self.create(event='PermissionRequest', tool_name='AskUserQuestion', tool_use_id='question')
        self.mocks[-1].assert_called_once()
        for tool in ('Write', 'Edit', 'Bash'):
            self.assertEqual(d.create_pending({'event': 'PreToolUse', 'tool_name': tool}), (None, None, False))
        self.assertEqual(d.resolve({'event': 'PostToolUse', 'session_id': 'session-private', 'tool_use_id': 'question'}), [rec['key']])

    def test_failure_resolves_only_same_call(self):
        a = self.create(event='PermissionRequest', tool_use_id='a')
        b = self.create(event='PermissionRequest', tool_use_id='b')
        self.assertIn('PostToolUseFailure', d.RESOLVE_EVENTS)
        self.assertEqual(d.resolve({'event': 'PostToolUseFailure', 'session_id': 'session-private', 'tool_use_id': 'a'}), [a['key']])
        self.assertIn(b['key'], d.pending)
        self.assertEqual(d.resolve({'event': 'PostToolUseFailure', 'session_id': 'session-private'}), [])

    def test_tool_progress_ends_only_generic_notification_window(self):
        for event in ('PostToolUse', 'PostToolUseFailure'):
            d.pending.clear()
            generic = self.create(message='needs permission')
            concrete = self.create(event='PermissionRequest', tool_use_id='waiting')
            fingerprint = self.create(event='permission_prompt', tool_name='Write', tool_input={'file_path': '/private/pending'})
            other = self.create(session_id='other', message='needs permission')
            removed = d.resolve({'event': event, 'session_id': 'session-private', 'tool_use_id': 'unrelated'})
            self.assertEqual(removed, [generic['key']])
            self.assertEqual(set(d.pending), {concrete['key'], fingerprint['key'], other['key']})
            count = self.mocks[-1].call_count
            new = self.create(message='needs permission')
            self.assertNotEqual(new['generation'], generic['generation'])
            self.assertEqual(self.mocks[-1].call_count, count + 1)

    def test_elicitation_id_exact_resolution(self):
        a = self.create(event='Elicitation', elicitation_id='a')
        b = self.create(event='Elicitation', elicitation_id='b')
        self.assertEqual(d.resolve({'event': 'ElicitationResult', 'session_id': 'session-private', 'elicitation_id': 'a'}), [a['key']])
        self.assertIn(b['key'], d.pending)

    def test_missing_session_ambiguous_identity_does_not_clear(self):
        a = self.create(session_id='a', tool_use_id='shared')
        b = self.create(session_id='b', tool_use_id='shared')
        self.assertEqual(d.resolve({'event': 'PostToolUse', 'tool_use_id': 'shared'}), [])
        self.assertEqual(set(d.pending), {a['key'], b['key']})
        d.resolve({'event': 'SessionEnd', 'session_id': 'a'})
        self.assertEqual(d.resolve({'event': 'PostToolUse', 'tool_use_id': 'shared'}), [b['key']])

    def test_file_operations_have_safe_summary(self):
        for tool, expected in [('Write', '写入文件'), ('Edit', '替换文件')]:
            self.create(event='PermissionRequest', tool_name=tool, tool_use_id=tool, tool_input={'file_path': '/private/path', 'content': 'SECRET'})
            body = self.mocks[-1].call_args.args[1]
            self.assertIn(expected, body)
            self.assertNotIn('/private', body)
            self.assertNotIn('SECRET', body)

    def test_tool_operation_does_not_send_command(self):
        self.create(event='PermissionRequest', task_title='修复构建', tool_name='Bash',
                    tool_input={'description': '运行单元测试', 'command': 'npm test --token TOP_SECRET'})
        body = self.mocks[-1].call_args.args[1]
        self.assertIn('修复构建', body)
        self.assertIn('Bash — 运行单元测试', body)
        self.assertNotIn('TOP_SECRET', body)
        self.assertNotIn('npm test', body)

    def test_actual_question(self):
        self.create(event='Elicitation', tool_name='AskUserQuestion',
                    tool_input={'questions': [{'question': '选择浅色还是深色主题？'}]})
        self.assertIn('待回答问题：选择浅色还是深色主题？', self.mocks[-1].call_args.args[1])

    def test_redaction(self):
        text = d.safe_summary('CodeBuddy token=TOP_SECRET password="HIDDEN PASS" Bearer abcdef curl https://example.com/?token=SECRET')
        for secret in ('TOP_SECRET', 'HIDDEN PASS', 'abcdef', 'https://', 'curl ', 'CodeBuddy'):
            self.assertNotIn(secret, text)
        self.assertIn('WorkBuddy', text)

    def test_short_prompts_never_become_task_title(self):
        for prompt in ('已点击', '继续', '好的', '继续执行。', 'OK!', '确认执行', '已处理'):
            d.session_tasks.clear()
            d.resolve({'event': 'UserPromptSubmit', 'session_id': 's', 'prompt': prompt})
            self.assertNotIn('s', d.session_tasks)
            self.assertIn('未获取原始任务名称', d.reminder_body({'task_title': prompt}, 0))
            self.assertEqual(d.context_fields({'task_title': prompt})['task_title'], '')
        d.session_tasks['s'] = '修复登录页面'
        d.resolve({'event': 'UserPromptSubmit', 'session_id': 's', 'task_title': '已点击'})
        self.assertEqual(d.session_tasks['s'], '修复登录页面')

    def test_address_placeholder_is_not_task_title(self):
        for text in ('[地址已隐藏]', '/Users/private/screenshot.png', 'https://example.invalid/image.png'):
            self.assertEqual(d.task_summary(text), '')
            self.assertIn('未获取原始任务名称', d.reminder_body({'task_title': text}, 0))
        self.assertEqual(d.task_summary('检查截图 /Users/private/screenshot.png'), '检查截图 [地址已隐藏]')

    def test_readonly_preview_does_not_create_or_resolve(self):
        rec = self.create(session_id='current-real-sid')
        before = json.dumps(d.pending, sort_keys=True)
        saved = Path(d.STATE_FILE).read_bytes()
        handler = object.__new__(d.Handler)
        handler._send = Mock()
        handler.path = '/preview-existing?session_id=current-real-sid'
        handler.do_GET()
        data = handler._send.call_args.args[1]
        self.assertFalse(data['created'])
        self.assertEqual(data['records'][0]['key'], rec['key'])
        self.assertEqual(data['records'][0]['link'], 'workbuddy://chat/current-real-sid')
        for field in ('reminder_token', 'generation', 'ack_operator_id'):
            self.assertNotIn(field, json.dumps(data))
        handler.path = '/preview-existing?session_id=absent'
        handler.do_GET()
        self.assertEqual(handler._send.call_args.args[1]['records'], [])
        self.assertEqual(json.dumps(d.pending, sort_keys=True), before)
        self.assertEqual(Path(d.STATE_FILE).read_bytes(), saved)

    def test_user_prompt_context_and_resolve(self):
        self.create(interaction_id='old')
        d.resolve({'event': 'UserPromptSubmit', 'session_id': 'session-private', 'prompt': '修复登录页面'})
        self.assertFalse(d.pending)
        self.create(interaction_id='new')
        self.assertIn('任务：修复登录页面', self.mocks[-1].call_args.args[1])
        d.resolve({'event': 'SessionEnd', 'session_id': 'session-private'})
        self.assertFalse(d.session_tasks)

    def test_resolve_iid_has_priority_and_clears_all_statuses(self):
        keys = {}
        for iid, status in [('a', 'WAITING'), ('b', 'EXPIRED'), ('c', 'ESCALATED')]:
            rec = self.create(session_id='s', interaction_id=iid)
            rec['status'] = status
            keys[iid] = rec['key']
        self.assertEqual(d.resolve({'interaction_id': 'a', 'session_id': 's'}), [keys['a']])
        self.assertEqual(set(d.pending), {keys['b'], keys['c']})
        self.assertEqual(d.resolve({'interaction_id': 'missing', 'session_id': 's'}), [])
        self.assertEqual(set(d.resolve({'event': 'SessionEnd', 'session_id': 's'})), {keys['b'], keys['c']})
        self.assertEqual(json.loads(Path(d.STATE_FILE).read_text()), {})

    def test_expired_persisted(self):
        d.pending['expired'] = {'status': 'WAITING', 'created_at': 1, 'next_reminder_at': 2}
        event = Mock()
        event.is_set.side_effect = [False, False, True]
        with patch.object(d, 'stop_evt', event), patch.object(d.time, 'sleep'):
            d.scheduler_loop()
        saved = json.loads(Path(d.STATE_FILE).read_text())
        self.assertEqual(saved['expired']['status'], 'EXPIRED')
        self.assertIsNone(saved['expired']['next_reminder_at'])
        self.mocks[-1].assert_not_called()

    def test_first_notification_schedule_is_not_due(self):
        rec = self.create(interaction_id='one')
        self.create(interaction_id='one')
        self.assertEqual(rec['reminder_count'], 1)
        self.assertGreater(rec['next_reminder_at'], d.now_ts())
        self.assertEqual(self.mocks[-1].call_count, 1)

    def test_daemon_failure_no_sensitive_log(self):
        self.patches[-1].stop()
        d.config = {'feishu': {'enabled': True, 'target_id': 'mock'}}
        with patch.object(d.subprocess, 'run', return_value=Mock(returncode=7, stdout=b'TOP_SECRET', stderr=b'TOP_SECRET')) as run:
            self.assertFalse(d.notify_feishu('标题', '正文'))
        self.assertEqual(run.call_args.kwargs['env']['ACTION_REMINDER_PYTHON'], sys.executable)
        self.assertNotIn('TOP_SECRET', Path(d.LOG_FILE).read_text())


    def test_session_id_forwarded_without_using_interaction_id(self):
        self.create(session_id='parent-session', interaction_id='child-interaction')
        self.assertEqual(self.mocks[-1].call_args.kwargs, {'session_id': 'parent-session'})

    def test_session_deeplink_encoding(self):
        self.assertEqual(d.session_deeplink("任务 /?#%&=+!'()*~._-"),
                         "workbuddy://chat/%E4%BB%BB%E5%8A%A1%20%2F%3F%23%25%26%3D%2B!'()*~._-")
        self.assertEqual(d.session_deeplink('abc%2Fdef'), 'workbuddy://chat/abc%252Fdef')

    def test_invalid_session_id_has_no_link(self):
        for sid in (None, '', ' ', 123, {}, [], '.', '..', 'a\n', 'a\x00', 'a\x7f', 'a\x85', '\ud800'):
            with self.subTest(sid=repr(sid)):
                self.assertEqual(d.session_deeplink(sid), '')

    def test_daemon_passes_link_and_empty_fallback(self):
        self.patches[-1].stop()
        d.config = {'feishu': {'enabled': True, 'target_id': 'mock'}}
        with patch.object(d.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'feishu ok=True\n')) as run:
            for sid in ('parent /任务', None):
                self.assertTrue(d.notify_feishu('标题', '正文', session_id=sid))
                self.assertEqual(run.call_args.args[0][-1], d.session_deeplink(sid))


class HookDispatchTests(unittest.TestCase):
    def test_ignored_notifications_never_contact_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'called'
            curl = Path(tmp) / 'curl'
            curl.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
            curl.chmod(0o700)
            env = dict(os.environ, PATH=tmp + ':' + os.environ['PATH'], HOME=tmp)
            for payload in ({'hook_event_name': 'Notification', 'notification_type': 'idle_prompt'},
                            {'hook_event_name': 'Notification', 'notification_type': 'agent_completed'},
                            {'hook_event_name': 'Stop'},
                            {'hook_event_name': 'PreToolUse', 'tool_name': 'Write'},
                            {'hook_event_name': 'PreToolUse', 'tool_name': 'Edit'},
                            {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash'}, []):
                result = subprocess.run(['/bin/bash', str(ROOT / 'hook-dispatch.sh')],
                                        input=json.dumps(payload), text=True, capture_output=True, env=env)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
            self.assertFalse(marker.exists())

    def test_permission_forwards_original_fields_and_no_approval_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'payload'
            curl = Path(tmp) / 'curl'
            curl.write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\n'
                            + 'Path(' + repr(str(output)) + ').write_text(sys.stdin.read())\n')
            curl.chmod(0o700)
            env = dict(os.environ, PATH=tmp + ':' + os.environ['PATH'], HOME=tmp)
            for tool in ('Bash', 'Write', 'Edit', 'AskUserQuestion'):
                payload = {'hook_event_name': 'PermissionRequest', 'session_id': 's',
                           'tool_name': tool, 'tool_use_id': 'call', 'tool_input': {'description': '待确认操作'}}
                result = subprocess.run(['/bin/bash', str(ROOT / 'hook-dispatch.sh')],
                                        input=json.dumps(payload), text=True, capture_output=True, env=env)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertEqual(json.loads(output.read_text()), dict(payload, event='PermissionRequest'))
            payload['hook_event_name'] = 'PreToolUse'
            result = subprocess.run(['/bin/bash', str(ROOT / 'hook-dispatch.sh')],
                                    input=json.dumps(payload), text=True, capture_output=True, env=env)
            self.assertEqual(result.stdout, '')
            self.assertEqual(json.loads(output.read_text()), dict(payload, event='PermissionRequest'))
            payload['hook_event_name'] = 'PostToolUseFailure'
            result = subprocess.run(['/bin/bash', str(ROOT / 'hook-dispatch.sh')],
                                    input=json.dumps(payload), text=True, capture_output=True, env=env)
            self.assertEqual(result.stdout, '')
            self.assertEqual(json.loads(output.read_text()), dict(payload, event='PostToolUseFailure'))


class AcknowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [('STATE_FILE', self.tmp.name + '/state.json'),
                            ('LOG_FILE', self.tmp.name + '/daemon.log'),
                            ('ACK_CALLBACK_READY', True),
                            ('callback_process', Mock(poll=Mock(return_value=None))),
                            ('stop_evt', threading.Event())]:
            p = patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ('notify', 'notify_feishu'):
            p = patch.object(d, name, return_value=True)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        d.pending = {}
        d.session_tasks = {}
        d.config = {'feishu': {'target_type': 'user', 'target_id': 'ou_target'}}
        self.payload = {'event': 'PermissionRequest', 'interaction_id': 'a', 'session_id': 's'}
        self.rec = d.create_pending(self.payload)[1]
        self.event = {'type': 'card.action.trigger', 'action_tag': 'button',
                      'operator_id': 'ou_target', 'action_value': json.dumps({
                          'action': 'ack_reminder', 'reminder_token': self.rec['reminder_token'],
                          'generation': self.rec['generation']})}

    def test_gate_disabled(self):
        with patch.object(d, 'ACK_CALLBACK_READY', False):
            self.assertFalse(d.acknowledge_card(self.event))
        self.assertEqual(self.rec['status'], 'WAITING')

    def test_token_unique_and_unpredictable_shape(self):
        other = d.create_pending(dict(self.payload, interaction_id='b'))[1]
        self.assertRegex(self.rec['reminder_token'], r'^[A-Za-z0-9_-]{43}$')
        self.assertNotEqual(self.rec['reminder_token'], other['reminder_token'])
        value = json.loads(self.event['action_value'])
        value['reminder_token'] = 'x' * 43
        self.assertFalse(d.acknowledge_card(dict(self.event, action_value=json.dumps(value))))

    def test_only_target_operator(self):
        for operator in ('ou_other', '', None):
            self.assertFalse(d.acknowledge_card(dict(self.event, operator_id=operator)))
        self.rec['ack_operator_id'] = ''
        self.assertFalse(d.acknowledge_card(self.event))

    def test_old_generation_rejected(self):
        self.rec['generation'] = '0' * 32
        self.assertFalse(d.acknowledge_card(self.event))
        self.assertEqual(self.rec['status'], 'WAITING')

    def test_idempotent_and_no_notifications_or_commands(self):
        self.notify.reset_mock()
        self.notify_feishu.reset_mock()
        with patch.object(d.subprocess, 'run') as run:
            self.assertTrue(d.acknowledge_card(self.event))
            saved = Path(d.STATE_FILE).read_bytes()
            self.assertTrue(d.acknowledge_card(self.event))
            d.send_reminder(self.rec['key'], self.rec)
            run.assert_not_called()
        self.assertEqual(Path(d.STATE_FILE).read_bytes(), saved)
        self.assertIsNone(self.rec['next_reminder_at'])
        self.notify.assert_not_called()
        self.notify_feishu.assert_not_called()

    def test_restart_persistence_and_duplicate_hook(self):
        self.assertTrue(d.acknowledge_card(self.event))
        d.pending = {}
        d.load_state()
        self.notify.reset_mock()
        self.notify_feishu.reset_mock()
        _, rec, created = d.create_pending(self.payload)
        self.assertFalse(created)
        self.assertEqual(rec['status'], 'ACKED')
        self.assertTrue(d.acknowledge_card(self.event))
        self.notify.assert_not_called()
        self.notify_feishu.assert_not_called()
        self.assertEqual(os.stat(d.STATE_FILE).st_mode & 0o777, 0o600)

    def test_other_record_and_new_occurrence_unaffected(self):
        other = d.create_pending(dict(self.payload, interaction_id='b'))[1]
        self.assertTrue(d.acknowledge_card(self.event))
        self.assertEqual(other['status'], 'WAITING')
        d.resolve({'interaction_id': 'a'})
        new = d.create_pending(self.payload)[1]
        self.assertNotEqual(new['generation'], self.rec['generation'])
        self.assertFalse(d.acknowledge_card(self.event))
        self.assertEqual(new['status'], 'WAITING')

    def test_same_fallback_key_until_lifecycle_boundary(self):
        payload = {'event': 'permission_prompt', 'session_id': 'fallback', 'message': '等待输入'}
        rec = d.create_pending(payload)[1]
        event = dict(self.event, action_value=json.dumps({
            'action': 'ack_reminder', 'reminder_token': rec['reminder_token'],
            'generation': rec['generation']}))
        self.assertTrue(d.acknowledge_card(event))
        self.assertFalse(d.create_pending(payload)[2])
        d.resolve({'event': 'UserPromptSubmit', 'session_id': 'fallback'})
        self.assertTrue(d.create_pending(payload)[2])
        self.assertFalse(d.acknowledge_card(event))

    def test_write_failure_not_acknowledged(self):
        with patch.object(d.os, 'replace', side_effect=OSError):
            self.assertFalse(d.acknowledge_card(self.event))
        self.assertEqual(self.rec['status'], 'WAITING')
        self.assertEqual(json.loads(Path(d.STATE_FILE).read_text())[self.rec['key']]['status'], 'WAITING')

    def test_malformed_and_approval_actions_rejected(self):
        for value in ('not-json', 'null', '[]', '{}', 7, json.dumps({
                'action': 'approve', 'reminder_token': self.rec['reminder_token'],
                'generation': self.rec['generation']}), json.dumps({
                'action': 'ack_reminder', 'reminder_token': '中文', 'generation': 'x'})):
            self.assertFalse(d.acknowledge_card(dict(self.event, action_value=value)))
        self.assertFalse(d.acknowledge_card(dict(self.event, action_tag='input')))
        self.assertFalse(d.acknowledge_card(dict(self.event, type='other')))


class OpenCallbackTests(unittest.TestCase):
    setUp = AcknowledgeTests.setUp

    def open_event(self, **extra):
        value = dict(json.loads(self.event['action_value']), action='open_reminder', **extra)
        return dict(self.event, action_value=json.dumps(value))

    def test_open_no_shell_no_ack_no_other_changes(self):
        other = d.create_pending(dict(self.payload, interaction_id='b'))[1]
        self.rec['session_id'] = "实际 /任务;$(touch bad)"
        before = json.dumps(d.pending, sort_keys=True)
        saved = Path(d.STATE_FILE).read_bytes()
        self.notify.reset_mock()
        self.notify_feishu.reset_mock()
        with patch.object(d.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertTrue(d.acknowledge_card(self.open_event()))
            run.assert_called_once_with(['/usr/bin/open', d.session_deeplink(self.rec['session_id'])],
                                        shell=False, capture_output=True, timeout=10)
        self.assertEqual(json.dumps(d.pending, sort_keys=True), before)
        self.assertEqual(Path(d.STATE_FILE).read_bytes(), saved)
        self.assertEqual(other['status'], 'WAITING')
        self.notify.assert_not_called()
        self.notify_feishu.assert_not_called()

    def test_ack_then_open_does_not_restore_reminder(self):
        self.assertTrue(d.acknowledge_card(self.event))
        with patch.object(d.subprocess, 'run', return_value=Mock(returncode=0)):
            self.assertTrue(d.acknowledge_card(self.open_event()))
        self.assertEqual(self.rec['status'], 'ACKED')
        self.assertIsNone(self.rec['next_reminder_at'])

    def test_open_rejects_identity_old_token_and_generation(self):
        with patch.object(d.subprocess, 'run') as run:
            for operator in ('ou_other', '', None):
                self.assertFalse(d.acknowledge_card(dict(self.open_event(), operator_id=operator)))
            for field, value in [('reminder_token', 'z' * 43), ('generation', '0' * 32)]:
                event = self.open_event()
                data = json.loads(event['action_value']); data[field] = value
                self.assertFalse(d.acknowledge_card(dict(event, action_value=json.dumps(data))))
            d.config['feishu']['target_id'] = 'ou_changed'
            self.assertFalse(d.acknowledge_card(self.open_event()))
            run.assert_not_called()

    def test_open_rejects_payload_url_and_approval(self):
        with patch.object(d.subprocess, 'run') as run:
            for url in ('https://evil.invalid', 'file:///tmp/x', 'workbuddy://chat/other', '; touch bad'):
                self.assertFalse(d.acknowledge_card(self.open_event(url=url)))
            self.assertFalse(d.acknowledge_card(self.open_event(session_id='other')))
            run.assert_not_called()

    def test_open_after_resolve_and_missing_session_rejected(self):
        with patch.object(d.subprocess, 'run') as run:
            self.rec['session_id'] = None
            self.assertFalse(d.acknowledge_card(self.open_event()))
            self.rec['session_id'] = 's'
            event = self.open_event()
            d.resolve({'interaction_id': 'a'})
            d.create_pending(self.payload)
            self.assertFalse(d.acknowledge_card(event))
            run.assert_not_called()

    def test_open_group_requires_configured_operator(self):
        d.config['feishu'] = {'target_type': 'chat', 'target_id': 'oc_group'}
        with patch.object(d.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertFalse(d.acknowledge_card(self.open_event()))
            run.assert_not_called()
            d.config['feishu']['ack_operator_id'] = 'ou_target'
            self.assertTrue(d.acknowledge_card(self.open_event()))

    def test_open_failures_and_disconnected_gate(self):
        for result in (Mock(returncode=1), OSError(), subprocess.TimeoutExpired('open', 10)):
            with patch.object(d.subprocess, 'run', **({'side_effect': result} if isinstance(result, Exception)
                                                      else {'return_value': result})):
                self.assertFalse(d.acknowledge_card(self.open_event()))
        with patch.object(d, 'callback_process', None), patch.object(d.subprocess, 'run') as run:
            self.assertFalse(d.acknowledge_card(self.open_event()))
            run.assert_not_called()
        self.assertEqual(self.rec['status'], 'WAITING')


class FeishuScriptTests(unittest.TestCase):
    def run_script(self, response='{"ok": true}', exit_code=0, override=True,
                   link=None, body='任务：中文测试\n请返回 WorkBuddy', ack=None):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / 'mock-cli'
            output = Path(tmp) / 'args.json'
            cli.write_text('#!' + sys.executable + '\nimport json,sys\nfrom pathlib import Path\n'
                           + 'Path(' + repr(str(output)) + ').write_text(json.dumps(sys.argv, ensure_ascii=False))\n'
                           + 'print(' + repr(response) + ')\nsys.exit(' + str(exit_code) + ')\n')
            cli.chmod(0o700)
            cfg = Path(tmp) / 'config.json'
            cfg.write_text(json.dumps({'feishu': {'target_id': 'mock-target', 'cli_path': str(cli)}}))
            env = dict(os.environ, ACTION_REMINDER_CONFIG=str(cfg), ACTION_REMINDER_PYTHON=sys.executable)
            env.pop('ACTION_REMINDER_LARK_CLI', None)
            env.pop('ACTION_REMINDER_ACK_VALUE', None)
            if ack is not None:
                env['ACTION_REMINDER_ACK_VALUE'] = json.dumps(ack)
            if override:
                env['ACTION_REMINDER_LARK_CLI'] = str(cli)
            command = ['/bin/bash', str(ROOT / 'notify-feishu.sh'), 'WorkBuddy · 待操作提醒', body]
            if link is not None:
                command.append(link)
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            args = json.loads(output.read_text()) if output.exists() else None
            return result, args

    def test_title_and_config_cli(self):
        result, args = self.run_script(override=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args[args.index('--text') + 1], '【WorkBuddy · 待操作提醒】\n任务：中文测试\n请返回 WorkBuddy')

    def test_card_structure_and_encoded_link(self):
        link = d.session_deeplink("父会话 /?#%!'()")
        result, args = self.run_script(link=link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--text', args)
        self.assertEqual(args[args.index('--msg-type') + 1], 'interactive')
        card = json.loads(args[args.index('--content') + 1])
        self.assertEqual(card['schema'], '2.0')
        self.assertEqual(card['config']['width_mode'], 'default')
        self.assertEqual(card['header']['template'], 'blue')
        self.assertEqual(card['header']['title'], {'tag': 'plain_text', 'content': 'WorkBuddy · 待操作提醒'})
        elements = card['body']['elements']
        self.assertEqual([e['tag'] for e in elements], ['column_set', 'column_set', 'markdown'])
        self.assertEqual(elements[0]['columns'][0]['tag'], 'column')
        for column in elements[1]['columns']:
            button = column['elements'][0]
            self.assertTrue(button['disabled'])
            self.assertEqual(button['behaviors'], [])
        self.assertIn('按钮暂不可用', elements[2]['content'])

    def test_callback_buttons_equal_width(self):
        ack = {'action': 'ack_reminder', 'reminder_token': 'a' * 43, 'generation': 'b' * 32}
        result, args = self.run_script(link='workbuddy://chat/test', ack=ack)
        self.assertEqual(result.returncode, 0)
        elements = json.loads(args[args.index('--content') + 1])['body']['elements']
        row = elements[1]
        self.assertEqual(row['flex_mode'], 'none')
        self.assertEqual(len(row['columns']), 2)
        for column, label, action in zip(row['columns'], ['打开对应任务', '已处理'],
                                          ['open_reminder', 'ack_reminder']):
            self.assertEqual((column['width'], column['weight']), ('weighted', 1))
            button = column['elements'][0]
            self.assertEqual(button['width'], 'fill')
            self.assertEqual(button['text']['content'], label)
            self.assertEqual(button['behaviors'], [{'type': 'callback', 'value': dict(ack, action=action)}])
        self.assertNotIn('workbuddy://', json.dumps(elements))
        self.assertNotIn('ou_', json.dumps(elements))
        self.assertIn('手机点击也一样', elements[2]['content'])

    def test_bad_callback_value_rejected(self):
        for ack in ({}, [], {'action': 'approve'}, {'action': 'ack_reminder',
                    'reminder_token': 'a' * 43, 'generation': 'b' * 32, 'operator_id': 'ou_x'}):
            result, args = self.run_script(link='workbuddy://chat/test', ack=ack)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(args)

    def test_markdown_injection_escaped(self):
        body = '<at id=all></at> [点击](https://example.invalid) ![图](x) **粗** `代码`\n# 标题 &lt;at&gt; \\ :OK:'
        result, args = self.run_script(link='workbuddy://chat/test', body=body)
        self.assertEqual(result.returncode, 0, result.stderr)
        card = json.loads(args[args.index('--content') + 1])
        text = card['body']['elements'][0]['columns'][0]['elements'][0]['content']
        escaped = text.removeprefix('**任务详情**\n')
        self.assertEqual(html.unescape(escaped), body)
        for token in ('<at', '[点击]', 'https:', '**粗**', '`代码`', '# 标题', '&lt;', ':OK:'):
            self.assertNotIn(token, escaped)

    def test_invalid_urls_rejected_before_cli(self):
        for link in ('https://example.invalid', 'file:///tmp/test', 'javascript:alert(1)',
                     'workbuddy://task/id', 'WORKBUDDY://chat/id', 'workbuddy://chat/',
                     'workbuddy://chat/id/other', 'workbuddy://chat/id?x=1',
                     'workbuddy://chat/id#x', 'workbuddy://chat/id\n',
                     'workbuddy://chat/a b', 'workbuddy://chat/任务',
                     'workbuddy://chat/%', 'workbuddy://chat/%GG', 'workbuddy://chat/%FF',
                     'workbuddy://chat/%00', 'workbuddy://chat/%0A', 'workbuddy://chat/%C2%85',
                     'workbuddy://chat/.', 'workbuddy://chat/..', 'workbuddy://chat/%2E%2E',
                     'workbuddy://chat/%20', 'workbuddy://chat/%61', 'workbuddy://chat/%2f',
                     'workbuddy://chat@evil/id', 'workbuddy://chat:80/id'):
            with self.subTest(link=link):
                result, args = self.run_script(link=link)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(args)
                self.assertNotIn(link, result.stderr)

    def test_empty_link_falls_back_to_text(self):
        result, args = self.run_script(link='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--text', args)
        self.assertNotIn('--content', args)
        self.assertNotIn('--msg-type', args)
        self.assertNotIn('workbuddy://', ' '.join(args))

    def test_nonzero_preserved_and_output_hidden(self):
        result, _ = self.run_script('TOP_SECRET', 7)
        self.assertEqual(result.returncode, 7)
        self.assertNotIn('TOP_SECRET', result.stdout + result.stderr)

    def test_false_ok_is_failure(self):
        result, _ = self.run_script('{"ok":false,"error":"TOP_SECRET"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('TOP_SECRET', result.stdout + result.stderr)

    def test_non_json_is_failure(self):
        result, _ = self.run_script('TOP_SECRET')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('TOP_SECRET', result.stdout + result.stderr)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [('stop_evt', threading.Event()), ('ACK_CALLBACK_READY', False),
                            ('callback_process', None), ('LOG_FILE', self.tmp.name + '/log'),
                            ('STATE_FILE', self.tmp.name + '/state')]:
            p = patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        d.pending = {}
        d.config = {'feishu': {'enabled': True, 'as': 'bot', 'ack_callback_enabled': True,
                              'cli_path': sys.executable, 'target_id': 'ou_test'}}

    def wait_until(self, check):
        deadline = time.monotonic() + 4
        while not check() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertTrue(check())

    def start_consumer(self):
        # 实际子进程和管道模拟 CLI；stdin 保持打开，指令触发对应输出。
        proc = subprocess.Popen([sys.executable, '-u', '-c',
            'import sys\nfor line in sys.stdin:\n'
            ' if line.startswith("ERR "): print(line[4:].rstrip(), file=sys.stderr, flush=True)\n'
            ' else: print(line.rstrip(), flush=True)\n'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.proc = proc
        def cleanup():
            d.stop_evt.set()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=4)
            self.thread.join(timeout=4)
            proc.stdout.close()
            proc.stderr.close()
        self.addCleanup(cleanup)
        self.popen = patch.object(d.subprocess, 'Popen', return_value=proc)
        self.mock_popen = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.thread = threading.Thread(target=d.callback_listener_loop)
        self.thread.start()
        self.wait_until(lambda: d.callback_process is proc)

    def emit(self, text):
        self.proc.stdin.write((text + '\n').encode())
        self.proc.stdin.flush()

    def ready(self):
        self.emit('ERR ' + d.CALLBACK_READY_MARKER.decode())
        self.wait_until(d.callback_ready)

    def test_real_pipes_ready_schema_ack_duplicate_restart(self):
        with patch.object(d, 'notify'), patch.object(d, 'notify_feishu'):
            payload = {'event': 'PermissionRequest', 'interaction_id': 'test', 'session_id': 's'}
            rec = d.create_pending(payload)[1]
        self.start_consumer()
        self.assertFalse(d.callback_ready())
        self.assertFalse(self.proc.stdin.closed)
        self.assertEqual(self.mock_popen.call_args.args[0][1:],
                         ['event', 'consume', 'card.action.trigger', '--as', 'bot'])
        self.assertEqual(self.mock_popen.call_args.kwargs['stdin'], subprocess.PIPE)
        self.ready()
        value = {'action': 'ack_reminder', 'reminder_token': rec['reminder_token'],
                 'generation': rec['generation']}
        event = {'type': 'card.action.trigger', 'event_id': 'test-event',
                 'timestamp': '1791500000000', 'operator_id': 'ou_test',
                 'message_id': 'om_test', 'chat_id': 'oc_test', 'host': 'im_message',
                 'token': 'UNUSED_DELAY_TOKEN', 'action_tag': 'button',
                 'action_value': json.dumps(value), 'action_name': '', 'form_value': '',
                 'card_content': ''}
        self.emit('ERR Event received: type=card.action.trigger id=PRIVATE_EVENT_ID')
        self.emit('ERR [event] logs: /private/log')
        self.emit('not-json')
        self.emit(json.dumps({'event': event}))
        self.emit(json.dumps(dict(event, operator_id='ou_other')))
        self.emit(json.dumps(event))
        self.wait_until(lambda: rec['status'] == 'ACKED')
        self.emit(json.dumps(event))
        d.stop_evt.set()
        self.thread.join(timeout=4)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(d.ACK_CALLBACK_READY)
        self.assertIsNotNone(self.proc.poll())
        d.pending = {}
        d.load_state()
        with patch.object(d, 'notify') as notify, patch.object(d, 'notify_feishu') as send:
            self.assertFalse(d.create_pending(payload)[2])
            notify.assert_not_called()
            send.assert_not_called()
        self.assertNotIn('UNUSED_DELAY_TOKEN', Path(d.LOG_FILE).read_text())
        self.assertNotIn('ou_test', Path(d.LOG_FILE).read_text())
        self.assertNotIn('PRIVATE_EVENT_ID', Path(d.LOG_FILE).read_text())

    def test_connected_diagnostic_keeps_same_consumer_alive(self):
        self.start_consumer()
        self.ready()
        with patch.object(d, 'acknowledge_card') as ack:
            self.emit('ERR [source] feishu-websocket: connected')
            self.emit('{"type":"probe"}')
            self.wait_until(lambda: ack.called)
            self.assertTrue(d.callback_ready())
            self.assertIsNone(self.proc.poll())
            self.assertFalse(self.proc.stdin.closed)
            self.assertEqual(self.mock_popen.call_count, 1)

    def test_connected_without_ready_cannot_open_gate(self):
        self.start_consumer()
        consumed = threading.Event()
        real_read = os.read
        def observe_read(fd, size):
            chunk = real_read(fd, size)
            if b'[source] feishu-websocket: connected' in chunk:
                consumed.set()
            return chunk
        with patch.object(d.os, 'read', side_effect=observe_read):
            self.emit('ERR [source] feishu-websocket: connected')
            self.assertTrue(consumed.wait(4))
            self.assertFalse(d.callback_ready())
        self.ready()
        self.assertEqual(Path(d.LOG_FILE).read_text().count('ready event_key='), 1)

    def test_source_disconnect_closes_gate_while_process_alive(self):
        self.start_consumer()
        self.ready()
        self.assertIsNone(self.proc.poll())
        self.emit('ERR [source] feishu-websocket: disconnected (TOP_SECRET)')
        self.wait_until(lambda: not d.ACK_CALLBACK_READY)
        d.stop_evt.set()
        self.thread.join(timeout=4)
        self.assertIsNotNone(self.proc.poll())
        self.assertNotIn('TOP_SECRET', Path(d.LOG_FILE).read_text())

    def test_unknown_diagnostic_still_closes_gate(self):
        self.start_consumer()
        self.ready()
        self.emit('ERR unknown TOP_SECRET')
        self.wait_until(lambda: not d.ACK_CALLBACK_READY)
        d.stop_evt.set()
        self.thread.join(timeout=4)
        self.assertNotIn('TOP_SECRET', Path(d.LOG_FILE).read_text())

    def test_stdin_eof_closes_gate(self):
        self.start_consumer()
        self.ready()
        self.proc.stdin.close()
        self.wait_until(lambda: not d.callback_ready())
        d.stop_evt.set()
        self.thread.join(timeout=4)
        self.assertFalse(d.ACK_CALLBACK_READY)

    def test_diagnostic_disconnect_closes_gate(self):
        self.start_consumer()
        self.ready()
        self.emit('ERR {"ok":false,"error":{"type":"network","subtype":"disconnected"}}')
        self.wait_until(lambda: not d.ACK_CALLBACK_READY)
        d.stop_evt.set()
        self.thread.join(timeout=4)
        self.assertIsNotNone(self.proc.poll())

    def test_process_exit_closes_gate(self):
        self.start_consumer()
        self.ready()
        self.proc.terminate()
        self.wait_until(lambda: not d.callback_ready())
        d.stop_evt.set()
        self.thread.join(timeout=4)
        self.assertFalse(d.ACK_CALLBACK_READY)

    def test_config_alone_cannot_enable_button_and_gate_forwarding(self):
        with patch.object(d, 'notify'), patch.object(d, 'notify_feishu') as send:
            rec = d.create_pending({'event': 'permission_prompt', 'session_id': 's'})[1]
            self.assertNotIn('ack_value', send.call_args.kwargs)
            with patch.object(d, 'callback_ready', return_value=True):
                d.send_reminder(rec['key'], rec)
            self.assertEqual(send.call_args.kwargs['ack_value']['generation'], rec['generation'])
        with patch.object(d.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'feishu ok=True')) as run:
            d.notify_feishu('标题', '正文', 's', {'action': 'ack_reminder'})
            self.assertNotIn('ACTION_REMINDER_ACK_VALUE', run.call_args.kwargs['env'])

    def test_callback_environment_isolated_and_configurable(self):
        for setting in (None, True, False):
            with self.subTest(setting=setting):
                if setting is None:
                    d.config['feishu'].pop('ack_callback_no_proxy', None)
                else:
                    d.config['feishu']['ack_callback_no_proxy'] = setting
                event = Mock()
                event.is_set.return_value = False
                event.wait.return_value = True
                inherited = {'HTTP_PROXY': 'http://127.0.0.1:51166',
                             'HTTPS_PROXY': 'http://127.0.0.1:51166',
                             'LARK_CLI_NO_PROXY': 'inherited',
                             'LARKSUITE_CLI_NO_UPDATE_NOTIFIER': '0',
                             'LARKSUITE_CLI_NO_SKILLS_NOTIFIER': '0',
                             'PATH': os.environ.get('PATH', '')}
                with patch.dict(os.environ, inherited, clear=True), \
                        patch.object(d, 'stop_evt', event), \
                        patch.object(d.subprocess, 'Popen', side_effect=OSError) as popen:
                    before = dict(os.environ)
                    d.callback_listener_loop()
                    env = popen.call_args.kwargs['env']
                    self.assertEqual(dict(os.environ), before)
                    self.assertIsNot(env, os.environ)
                    if setting is False:
                        self.assertNotIn('LARK_CLI_NO_PROXY', env)
                    else:
                        self.assertEqual(env['LARK_CLI_NO_PROXY'], '1')
                    for key in ('LARKSUITE_CLI_NO_UPDATE_NOTIFIER', 'LARKSUITE_CLI_NO_SKILLS_NOTIFIER'):
                        self.assertEqual(env[key], '1')
                    self.assertEqual(env['HTTP_PROXY'], inherited['HTTP_PROXY'])
                    self.assertEqual(env['HTTPS_PROXY'], inherited['HTTPS_PROXY'])
                    self.assertEqual(env['PATH'], inherited['PATH'])
                    self.assertEqual(popen.call_args.args[0][1:],
                                     ['event', 'consume', 'card.action.trigger', '--as', 'bot'])
                self.assertFalse(d.ACK_CALLBACK_READY)

    def test_reconnect_backoff_and_shutdown(self):
        event = Mock()
        event.is_set.return_value = False
        event.wait.side_effect = [False, False, True]
        with patch.object(d, 'stop_evt', event), patch.object(d.subprocess, 'Popen', side_effect=OSError):
            d.callback_listener_loop()
        self.assertEqual([c.args[0] for c in event.wait.call_args_list], [1, 2, 4])
        self.assertFalse(d.ACK_CALLBACK_READY)


if __name__ == '__main__':
    unittest.main()
