#!/usr/bin/env python3
"""Isolated runtime/control tests: all HTTP, launchctl and daemon starts mocked.
Run: python3 scripts/test_runtime.py
"""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reminder-runtime-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home & space'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.scripts = self.root / 'scripts & space'
        self.scripts.mkdir()
        for name in ('runtime-config.sh', 'action-reminderctl.sh', 'hook-dispatch.sh'):
            shutil.copyfile(SCRIPTS / name, self.scripts / name)
        # A placeholder only; mocked nohup never executes this file.
        (self.scripts / 'reminder-daemon.py').write_text('raise AssertionError("must not run")')
        self.calls = self.root / 'calls.jsonl'
        self.env = {'HOME': str(self.home), 'PATH': str(self.bin) + ':/usr/bin:/bin',
                    'ACTION_REMINDER_PYTHON': sys.executable, 'MOCK_CALLS': str(self.calls),
                    'MOCK_ROOT': str(self.root)}
        self.mock('curl', '''
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['MOCK_ROOT'])
url = next((x for x in args if x.startswith('http://')), '')
if url.endswith('/health'):
    if os.environ.get('MOCK_OFFLINE') == '1' and not (root/'started').exists(): sys.exit(7)
    if (root/'stopped').exists(): sys.exit(7)
    print(os.environ.get('MOCK_HEALTH', '{"ok":true,"service":"agent-action-reminder"}'))
elif url.endswith('/status'):
    print(os.environ.get('MOCK_STATUS', '{"ok":true,"waiting":0,"pending":[],"ack_callback_ready":false}'))
elif url.endswith('/stop'):
    if os.environ.get('MOCK_STOP_FAIL') == '1': sys.exit(22)
    (root/'stopped').touch()
    print('{"ok":true}')
elif url.endswith('/event'):
    if os.environ.get('MOCK_OFFLINE') == '1' and not (root/'started').exists(): sys.exit(7)
    print('{"ok":true}')
''')
        self.mock('launchctl', '''
import os, sys
if sys.argv[1] == 'print':
    sys.exit(0 if sys.argv[-1].endswith('/'+os.environ.get('MOCK_LOADED', '!')) else 1)
sys.exit(int(os.environ.get('MOCK_LAUNCH_FAIL', '0')))
''')
        self.mock('nohup', '''
import os, pathlib
pathlib.Path(os.environ['MOCK_ROOT'], 'started').touch()
if os.environ.get('MOCK_START_FIFO'):
    fd = os.open(os.environ['MOCK_START_FIFO'], os.O_WRONLY)
    try: os.write(fd, b'1')
    finally: os.close(fd)
''')
        # Startup uses an explicit bounded cross-process handshake, not a timing
        # guess. The test keeps the FIFO open so an early signal is not lost.
        self.mock('sleep', '''
import os, select
if os.environ.get('MOCK_START_FIFO'):
    fd = os.open(os.environ['MOCK_START_FIFO'], os.O_RDONLY | os.O_NONBLOCK)
    try:
        ready, _, _ = select.select([fd], [], [], 5)
        if not ready or os.read(fd, 1) != b'1':
            raise RuntimeError('mock nohup did not signal startup')
    finally: os.close(fd)
''')
        self.mock('pkill', 'raise AssertionError("pkill forbidden")')

    def mock(self, name, body):
        program = '#!' + sys.executable + '\n'
        program += '''import json, os, sys
with open(os.environ['MOCK_CALLS'], 'a') as log:
    log.write(json.dumps({'cmd': os.path.basename(sys.argv[0]), 'args': sys.argv[1:],
                          'state': os.environ.get('ACTION_REMINDER_STATE_DIR'),
                          'python': os.environ.get('ACTION_REMINDER_PYTHON')})+'\\n')
'''
        program += body
        p = self.bin / name
        p.write_text(program)
        p.chmod(0o755)

    def run_shell(self, code, **env):
        return subprocess.run(['/bin/bash', '-c', code], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=15, cwd=self.root)

    def runtime(self, **env):
        return self.run_shell(
            '. ' + self.quote(self.scripts / 'runtime-config.sh') +
            '; reminder_runtime_init || exit $?; printf "%s\\n%s\\n%s\\n" '
            '"$ACTION_REMINDER_STATE_DIR" "$PORT" "$ACTION_REMINDER_PYTHON"', **env)

    @staticmethod
    def quote(path):
        import shlex
        return shlex.quote(str(path))

    def ctl(self, action, **env):
        return subprocess.run(['/bin/bash', str(self.scripts/'action-reminderctl.sh'), action],
                              env=dict(self.env, **env), capture_output=True, text=True,
                              timeout=15, cwd=self.root)

    def hook(self, payload, **env):
        return subprocess.run(['/bin/bash', str(self.scripts/'hook-dispatch.sh')],
                              input=payload, env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=15, cwd=self.root)

    def calls_read(self):
        return [json.loads(x) for x in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def config(self, value, directory=None):
        directory = directory or self.home/'.agent-action-reminder'
        directory.mkdir(parents=True, exist_ok=True)
        (directory/'config.json').write_text(json.dumps(value))
        return directory

    def test_new_and_legacy_and_explicit_state(self):
        self.assertEqual(self.runtime().stdout.splitlines()[0], str(self.home/'.agent-action-reminder'))
        legacy = self.config({'port': 19990}, self.home/'.workbuddy/action-reminder')
        self.assertEqual(self.runtime().stdout.splitlines()[:2], [str(legacy), '19990'])
        explicit = self.config({'port': 20990}, self.root/'explicit & space')
        result = self.runtime(ACTION_REMINDER_STATE_DIR=str(explicit))
        self.assertEqual(result.stdout.splitlines()[:2], [str(explicit), '20990'])

    def test_legacy_directory_without_config_does_not_win(self):
        (self.home/'.workbuddy/action-reminder').mkdir(parents=True)
        self.assertEqual(self.runtime().stdout.splitlines()[0], str(self.home/'.agent-action-reminder'))

    def test_relative_state_becomes_absolute(self):
        self.assertEqual(self.runtime(ACTION_REMINDER_STATE_DIR='relative path').stdout.splitlines()[0],
                         str(self.root/'relative path'))

    def test_port_boundaries_and_invalid(self):
        for value in (1, 65535):
            self.config({'port': value})
            self.assertEqual(self.runtime().stdout.splitlines()[1], str(value))
        for value in (0, 65536, True, False, 1.5, '18789', None, [], {}):
            with self.subTest(value=value):
                self.config({'port': value})
                self.assertNotEqual(self.runtime().returncode, 0)
                self.assertNotEqual(self.ctl('health').returncode, 0)
        self.assertEqual(self.calls_read(), [])

    def test_default_missing_port_and_malformed_config(self):
        self.config({})
        self.assertEqual(self.runtime().stdout.splitlines()[1], '18789')
        (self.home/'.agent-action-reminder/config.json').write_text('{bad')
        self.assertNotEqual(self.runtime().returncode, 0)

    def test_python_override_and_invalid(self):
        custom = self.bin/'custom python'
        custom.symlink_to(sys.executable)
        self.assertEqual(self.runtime(ACTION_REMINDER_PYTHON=str(custom)).stdout.splitlines()[2], str(custom))
        self.assertNotEqual(self.runtime(ACTION_REMINDER_PYTHON='/missing/python').returncode, 0)
        if Path('/usr/bin/python3').is_file():
            self.assertEqual(self.runtime(ACTION_REMINDER_PYTHON='').stdout.splitlines()[2], '/usr/bin/python3')

    def test_health_identity(self):
        self.assertEqual(self.ctl('health').returncode, 0)
        self.assertEqual(self.ctl('health', MOCK_HEALTH='{"ok":true}').returncode, 0)
        for health in ('<html>ok</html>', '{}', '{"ok":1}', '{"ok":true,"service":"other"}', '[]'):
            self.assertNotEqual(self.ctl('health', MOCK_HEALTH=health).returncode, 0)
        for status in ('{"ok":true}', '{"ok":true,"waiting":true,"pending":[],"ack_callback_ready":false}',
                       '{"ok":true,"waiting":1,"pending":[],"ack_callback_ready":false}'):
            self.assertNotEqual(self.ctl('health', MOCK_HEALTH='{"ok":true}', MOCK_STATUS=status).returncode, 0)

    def test_stop_does_not_post_to_unidentified_service(self):
        self.assertEqual(self.ctl('stop', MOCK_HEALTH='{"ok":true}', MOCK_STATUS='{}').returncode, 0)
        calls = self.calls_read()
        self.assertFalse(any('/stop' in str(x['args']) for x in calls))
        self.assertFalse(any(x['cmd'] == 'pkill' for x in calls))

    def test_stop_and_restart_failure_no_name_kill(self):
        self.assertEqual(self.ctl('stop').returncode, 0)
        self.assertFalse(any(x['cmd'] == 'pkill' for x in self.calls_read()))
        (self.root/'stopped').unlink()
        self.assertNotEqual(self.ctl('restart', MOCK_STOP_FAIL='1').returncode, 0)
        self.assertFalse(any(x['cmd'] == 'nohup' for x in self.calls_read()))

    def test_hook_fail_open_no_approval(self):
        for payload in ('', 'invalid', '[]', '{"hook_event_name":"Stop"}',
                        '{"hook_event_name":"Notification","notification_type":"idle_prompt"}'):
            result = self.hook(payload)
            self.assertEqual((result.returncode, result.stdout), (0, ''))
        result = self.hook('{"hook_event_name":"PermissionRequest"}', ACTION_REMINDER_PYTHON='/missing')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(self.calls_read(), [])

    def test_hook_mkdir_lazy_start_and_custom_port(self):
        state = self.root/'new state'
        fifo = self.root/'startup.fifo'
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        result = self.hook('{"hook_event_name":"PermissionRequest"}',
                           ACTION_REMINDER_STATE_DIR=str(state), MOCK_OFFLINE='1',
                           MOCK_START_FIFO=str(fifo))
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.assertTrue(state.is_dir())
        calls = self.calls_read()
        starts = [x for x in calls if x['cmd'] == 'nohup']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['state'], str(state))
        self.config({'port': 28789}, state)
        self.hook('{"hook_event_name":"PostToolUse"}', ACTION_REMINDER_STATE_DIR=str(state))
        self.assertTrue(any('28789/event' in str(x['args']) for x in self.calls_read()))

    def test_hook_mkdir_failure_and_bad_config_are_silent(self):
        blocked = self.root/'not a directory'
        blocked.write_text('file')
        result = self.hook('{"hook_event_name":"PermissionRequest"}',
                           ACTION_REMINDER_STATE_DIR=str(blocked/'state'), MOCK_OFFLINE='1')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.config({'port': False})
        result = self.hook('{"hook_event_name":"PermissionRequest"}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertFalse(any(x['cmd'] == 'nohup' for x in self.calls_read()))

    def test_launchd_failure_never_falls_back_to_manual_instance(self):
        result = self.ctl('install-launchd', MOCK_OFFLINE='1', MOCK_LAUNCH_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(x['cmd'] == 'nohup' for x in self.calls_read()))
        self.assertNotIn('已写入并加载', result.stdout)

    def test_launchd_plist_xml_environment_and_new_label(self):
        result = self.ctl('install-launchd', MOCK_OFFLINE='1',
                          ACTION_REMINDER_STATE_DIR=str(self.root/'state < & space'),
                          ACTION_REMINDER_LARK_CLI='/cli & space/lark-cli',
                          LARK_CLI_CONFIG_DIR='/config < & space', SECRET_MUST_NOT_PERSIST='private')
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.home/'Library/LaunchAgents/com.agent-action-reminder.plist'
        with path.open('rb') as f:
            data = plistlib.load(f)
        self.assertEqual(data['Label'], 'com.agent-action-reminder')
        self.assertEqual(data['ProgramArguments'], [sys.executable, str(self.scripts/'reminder-daemon.py')])
        env = data['EnvironmentVariables']
        self.assertEqual(env['ACTION_REMINDER_LARK_CLI'], '/cli & space/lark-cli')
        self.assertEqual(env['LARK_CLI_CONFIG_DIR'], '/config < & space')
        self.assertNotIn('SECRET_MUST_NOT_PERSIST', env)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(Path(env['ACTION_REMINDER_STATE_DIR']).is_dir())

    def test_launchd_legacy_reused_and_conflict_refused(self):
        folder = self.home/'Library/LaunchAgents'
        folder.mkdir(parents=True)
        old = folder/'com.tencent.workbuddy.action-reminder.plist'
        old.write_bytes(plistlib.dumps({'Label': 'com.tencent.workbuddy.action-reminder'}))
        self.assertEqual(self.ctl('install-launchd', MOCK_OFFLINE='1').returncode, 0)
        new = folder/'com.agent-action-reminder.plist'
        self.assertFalse(new.exists())
        with old.open('rb') as f:
            self.assertEqual(plistlib.load(f)['Label'], 'com.tencent.workbuddy.action-reminder')
        new.write_bytes(plistlib.dumps({'Label': 'com.agent-action-reminder'}))
        count = len(self.calls_read())
        self.assertNotEqual(self.ctl('install-launchd').returncode, 0)
        self.assertFalse(any(x['cmd'] == 'launchctl' and x['args'][0] != 'print'
                             for x in self.calls_read()[count:]))

    def test_launchd_loaded_legacy_without_plist_reused(self):
        result = self.ctl('install-launchd', MOCK_LOADED='com.tencent.workbuddy.action-reminder', MOCK_OFFLINE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home/'Library/LaunchAgents/com.tencent.workbuddy.action-reminder.plist').exists())
        self.assertFalse((self.home/'Library/LaunchAgents/com.agent-action-reminder.plist').exists())

    def test_start_with_existing_plist_never_nohup(self):
        folder = self.home/'Library/LaunchAgents'
        folder.mkdir(parents=True)
        (folder/'com.agent-action-reminder.plist').write_bytes(plistlib.dumps({}))
        self.ctl('start', MOCK_OFFLINE='1')
        calls = self.calls_read()
        self.assertFalse(any(x['cmd'] == 'nohup' for x in calls))
        self.assertTrue(any(x['cmd'] == 'launchctl' and x['args'][0] == 'bootstrap' for x in calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
