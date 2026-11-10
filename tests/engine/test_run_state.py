import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peerreviewagents.engine.run_state import inspect_state, tracked_stage, process_namespace, process_identity
from peerreviewagents.engine.storage import read, write


class RunStateTests(unittest.TestCase):
    def test_completed_and_interrupted_states_are_durable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            with tracked_stage(path, 'judge'):
                self.assertEqual(inspect_state(path)['status'], 'running')
            self.assertEqual(read(path)['status'], 'complete')
            with self.assertRaises(KeyboardInterrupt):
                with tracked_stage(path, 'judge'):
                    raise KeyboardInterrupt()
            self.assertEqual(read(path)['status'], 'interrupted')

    def test_stale_pid_and_overdue_owner_are_distinguished_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            original = {'status':'running','pid':123,'process_identity':'old','deadline_at':0,
                        'process_namespace':process_namespace()}
            write(path, original)
            with patch('peerreviewagents.engine.run_state.process_identity', return_value='new'):
                self.assertEqual(inspect_state(path)['status'], 'interrupted')
            with patch('peerreviewagents.engine.run_state.process_identity', return_value='old'):
                self.assertEqual(inspect_state(path)['status'], 'overdue')
            self.assertEqual(read(path), original)

    def test_other_namespace_does_not_falsely_report_a_dead_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            write(path, {'status':'running','pid':123,'process_namespace':'another-namespace'})
            self.assertEqual(inspect_state(path)['status'], 'unverified')

    def test_real_process_identity_is_stable_and_lost_when_the_process_exits(self):
        self.assertIsNotNone(process_namespace())
        self.assertIsNotNone(process_identity(os.getpid()))
        self.assertEqual(process_identity(os.getpid()), process_identity(os.getpid()))
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            identity = process_identity(child.pid)
            self.assertIsNotNone(identity)
            self.assertNotEqual(identity, process_identity(os.getpid()))
        finally:
            child.kill()
            child.wait()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            write(path, {'status': 'running', 'pid': child.pid, 'process_identity': identity,
                         'process_namespace': process_namespace(), 'deadline_at': float('inf')})
            self.assertEqual(inspect_state(path)['status'], 'interrupted')

    def test_zombie_process_has_no_identity(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        try:
            deadline = time.time() + 10
            while process_identity(child.pid) is not None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNone(process_identity(child.pid))
        finally:
            child.wait()

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS fallback check')
    def test_unavailable_macos_probe_falls_back_to_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            with patch('peerreviewagents.engine.run_state._darwin_boot_session', return_value=None):
                with tracked_stage(path, 'judge'):
                    self.assertIsNone(read(path)['process_namespace'])
                    self.assertEqual(inspect_state(path)['status'], 'unverified')
            with patch('peerreviewagents.engine.run_state.ctypes.CDLL', side_effect=OSError('missing library')):
                self.assertIsNone(process_namespace())
                self.assertIsNone(process_identity(os.getpid()))

    @unittest.skipUnless(sys.platform == 'darwin' and os.geteuid() != 0, 'macOS permission check')
    def test_uninspectable_process_is_unverified_not_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            write(path, {'status': 'running', 'pid': 1, 'process_identity': 'recorded',
                         'process_namespace': process_namespace(), 'deadline_at': float('inf')})
            self.assertEqual(inspect_state(path)['status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
