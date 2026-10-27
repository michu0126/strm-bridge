import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.server import State
from bridge.progress import ExecutionProgress


class LargeJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / 'data')

    def tearDown(self):
        self.state.watcher.close()
        self.state.pool.shutdown(wait=True)
        self.state.db.close()
        self.temp.cleanup()

    def test_large_plan_is_stored_once_and_progress_stays_small(self):
        plan = {'actions': [{'type': 'strm', 'source': str(i), 'target': str(i)+'.strm'} for i in range(20000)]}
        job = {'id': 'large', 'status': 'running', 'plan': plan}
        self.state.put_job(job)
        with patch('bridge.server.json.dumps', wraps=json.dumps) as dumps:
            for i in range(10):
                job['done'] = i
                self.state.put_job(job)
            self.assertTrue(all('actions' not in c.args[0] and 'plan' not in c.args[0] for c in dumps.call_args_list))
        stored = self.state.db.execute("SELECT value FROM jobs WHERE id='large'").fetchone()[0]
        self.assertLess(len(stored), 200)
        self.assertEqual(self.state.job('large')['plan'], plan)
        preview = self.state.job_preview('large', 19500)
        self.assertEqual(len(preview['actions']), 500)
        self.assertEqual(preview['actions'][0]['source'], '19500')
        self.assertEqual(preview['action_total'], 20000)
        self.assertNotIn('plan', self.state.jobs()[0])

    def test_old_inline_plans_remain_readable_and_can_resume(self):
        job = {'id': 'legacy', 'status': 'preview', 'plan': {'actions': [{'source': 'old'}]}}
        self.state.db.execute('INSERT INTO jobs VALUES(?,?)', ('legacy', json.dumps(job)))
        self.state.db.commit()
        self.assertEqual(self.state.job('legacy'), job)
        self.assertEqual(self.state.job_preview('legacy')['actions'][0]['source'], 'old')
        self.state.put_job(dict(job, status='running'))
        self.assertEqual(self.state.job('legacy')['plan'], job['plan'])

    def test_generation_progress_is_throttled_and_final_count_is_flushed(self):
        job = {'id': 'job', 'status': 'running'}
        progress = ExecutionProgress(self.state, job)
        with patch('bridge.progress.time.monotonic', return_value=100), patch.object(self.state, 'put_job') as put:
            for i in range(10000): progress(i+1, 10000, str(i))
            self.assertEqual(put.call_count, 2)
        self.assertEqual(job['done'], 10000)
