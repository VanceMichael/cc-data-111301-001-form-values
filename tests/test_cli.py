import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CommandTests(unittest.TestCase):
    def test_demo_runs_intake_and_confirmation(self):
        process = subprocess.run([sys.executable, '-m', 'form_values', 'demo'],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        # 演示覆盖首次接收、修订待确认、重复交付、确认与拒绝全流程
        self.assertIn('修订交付', result)
        self.assertIn('确认结果', result)
        self.assertEqual(result['确认结果']['status'], 'adopted')
        # 原始证据始终保留且可核对摘要
        evidence = result['确认结果']['evidence']
        self.assertGreater(evidence['bytes'], 0)
        self.assertEqual(len(evidence['sha256']), 64)
        # 重复交付不增加受访者：3 位受访者、6 次提交（含 1 次重复、1 次拒绝）
        overview = result['汇总结论']
        self.assertEqual(overview['respondent_count'], 3)
        self.assertEqual(overview['submission_count'], 6)
        self.assertEqual(overview['status_counts']['duplicate'], 1)

    def test_receive_and_query_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / 'store'
            receive = subprocess.run(
                [sys.executable, '-m', 'form_values', 'receive',
                 'fixtures/survey-r1.pdf', '--store', str(store)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(receive.returncode, 0, receive.stderr)
            receipt = json.loads(receive.stdout)
            self.assertEqual(receipt['status'], 'adopted')
            answer = subprocess.run(
                [sys.executable, '-m', 'form_values', 'answer', 'survey-A',
                 'contact.name', '--store', str(store)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(answer.returncode, 0, answer.stderr)
            detail = json.loads(answer.stdout)
            self.assertEqual(detail['adopted_value'], '虚构经销商甲')
            self.assertEqual({w['page'] for w in detail['versions'][0]['widget_locations']},
                             {1, 2})
