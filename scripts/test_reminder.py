"""离线回归测试：所有通知均被替换，不访问飞书或真实守护进程。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        payload = dict(event='idle_prompt', session_id='session-private', cwd='/Users/private/project')
        payload.update(kw)
        return d.create_pending(payload)[1]

    def test_chinese_title_and_generic_idle(self):
        self.create(message='CodeBuddy is waiting for your input')
        title, body = self.mocks[-1].call_args.args
        self.assertEqual(title, 'WorkBuddy｜等待你的输入')
        for text in ('任务：未提供任务名称', '未提供具体操作或问题', '等待时长：', '请返回 WorkBuddy 对应会话处理'):
            self.assertIn(text, body)
        for text in ('CodeBuddy', 'session-private', '/Users/private', '点击'):
            self.assertNotIn(text, body)
        self.assertEqual(len(body.splitlines()), 4)

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

    def test_user_prompt_context_and_resolve(self):
        self.create(interaction_id='old')
        d.resolve({'event': 'UserPromptSubmit', 'session_id': 'session-private', 'prompt': '修复登录页面'})
        self.assertFalse(d.pending)
        self.create(interaction_id='new')
        self.assertIn('任务：修复登录页面', self.mocks[-1].call_args.args[1])
        d.resolve({'event': 'SessionEnd', 'session_id': 'session-private'})
        self.assertFalse(d.session_tasks)

    def test_resolve_iid_has_priority_and_clears_all_statuses(self):
        for iid, status in [('a', 'WAITING'), ('b', 'EXPIRED'), ('c', 'ESCALATED')]:
            d.pending['iid:' + iid] = {'session_id': 's', 'status': status}
        self.assertEqual(d.resolve({'interaction_id': 'a', 'session_id': 's'}), ['iid:a'])
        self.assertEqual(set(d.pending), {'iid:b', 'iid:c'})
        self.assertEqual(d.resolve({'interaction_id': 'missing', 'session_id': 's'}), [])
        self.assertEqual(set(d.resolve({'session_id': 's'})), {'iid:b', 'iid:c'})
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


class FeishuScriptTests(unittest.TestCase):
    def run_script(self, response='{"ok": true}', exit_code=0, override=True):
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
            if override:
                env['ACTION_REMINDER_LARK_CLI'] = str(cli)
            result = subprocess.run(['/bin/bash', str(ROOT / 'notify-feishu.sh'), 'WorkBuddy · 待操作提醒', '任务：中文测试\n请返回 WorkBuddy'],
                                    env=env, capture_output=True, text=True)
            args = json.loads(output.read_text())
            return result, args

    def test_title_and_config_cli(self):
        result, args = self.run_script(override=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args[args.index('--text') + 1], '【WorkBuddy · 待操作提醒】\n任务：中文测试\n请返回 WorkBuddy')

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


if __name__ == '__main__':
    unittest.main()
