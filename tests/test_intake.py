import io
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from form_values.archive import read_object
from form_values.intake import (
    IntakeError,
    IntakeStore,
    STATUS_ADOPTED,
    STATUS_DUPLICATE,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    UNREADABLE_REASON,
)
from form_values.pdfbuild import FormBuilder


class Clock:
    def __init__(self):
        self.tick = 0

    def __call__(self):
        self.tick += 1
        return f'2026-01-01T00:00:{self.tick:02d}+00:00'


class IntakeTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = IntakeStore(self.root / 'store', clock=Clock())

    def tearDown(self):
        self.temp.cleanup()

    def receive(self, builder, filename='survey.pdf'):
        return self.store.receive_bytes(filename, builder.build_bytes())

    def builder(self, qid='survey-A', revision='1', **kwargs):
        return FormBuilder(qid, revision, **kwargs)


class RoutingTests(IntakeTestBase):
    def test_first_submission_is_adopted(self):
        builder = self.builder()
        builder.text_field('dealer.name', '虚构经销商甲', pages_with_widgets=(1,))
        record = self.receive(builder)
        self.assertEqual(record['status'], STATUS_ADOPTED)
        self.assertEqual(self.store.respondent_count(), 1)

    def test_identical_redelivery_is_duplicate_and_adds_no_respondent(self):
        builder = self.builder()
        builder.text_field('dealer.name', '甲', pages_with_widgets=(1,))
        data = builder.build_bytes()
        first = self.store.receive_bytes('a.pdf', data)
        again = self.store.receive_bytes('another-name.pdf', data)
        self.assertEqual(again['status'], STATUS_DUPLICATE)
        self.assertEqual(again['duplicate_of'], first['submission_id'])
        self.assertEqual(self.store.respondent_count(), 1)
        # 内容寻址归档只有一份原始字节
        raw_dir = self.root / 'store' / 'raw'
        self.assertEqual(len(list(raw_dir.iterdir())), 1)

    def test_same_values_different_bytes_is_duplicate_version(self):
        b1 = self.builder(revision='1')
        b1.text_field('dealer.name', '甲', pages_with_widgets=(1,))
        first = self.receive(b1, 'r1.pdf')
        b2 = self.builder(revision='2')
        b2.text_field('dealer.name', '甲', pages_with_widgets=(1,))
        second = self.receive(b2, 'r2.pdf')
        self.assertEqual(second['status'], STATUS_DUPLICATE)
        self.assertEqual(second['routing_reason'], 'same_values_redelivery')
        self.assertEqual(self.store.respondent_count(), 1)
        # 两份字节各自归档，证据不覆盖
        raw_dir = self.root / 'store' / 'raw'
        self.assertEqual(len(list(raw_dir.iterdir())), 2)

    def test_value_revision_goes_to_pending_without_stopping_batch(self):
        b1 = self.builder(revision='1')
        b1.text_field('dealer.name', '甲', pages_with_widgets=(1,))
        self.receive(b1)
        b2 = self.builder(revision='2')
        b2.text_field('dealer.name', '乙', pages_with_widgets=(1,))
        pending = self.receive(b2)
        self.assertEqual(pending['status'], STATUS_PENDING)
        self.assertEqual(pending['routing_reason'], 'value_revision')
        # 待确认期间查询仍说明采用的是首版
        view = self.store.questionnaire_view('survey-A')
        self.assertEqual(view['adopted_submission_id'],
                         self.store.overview()['questionnaires'][0]['adopted_submission_id'])
        self.assertEqual(self.store.field_detail('survey-A', 'dealer.name')
                         ['adopted_value'], '甲')
        # 另一位经销商的文件在同批中照常接收，不被争议件阻塞
        other = self.builder(qid='survey-B')
        other.text_field('dealer.name', '丙', pages_with_widgets=(1,))
        other_record = self.receive(other)
        self.assertEqual(other_record['status'], STATUS_ADOPTED)
        self.assertEqual(self.store.respondent_count(), 2)

    def test_redelivery_of_pending_file_stays_duplicate_and_keeps_one_review(self):
        b1 = self.builder(revision='1')
        b1.text_field('dealer.name', '甲', pages_with_widgets=(1,))
        self.receive(b1)
        b2 = self.builder(revision='2')
        b2.text_field('dealer.name', '乙', pages_with_widgets=(1,))
        data = b2.build_bytes()
        pending = self.store.receive_bytes('rev.pdf', data)
        again = self.store.receive_bytes('rev-copy.pdf', data)
        self.assertEqual(pending['status'], STATUS_PENDING)
        self.assertEqual(again['status'], STATUS_DUPLICATE)
        self.assertEqual(again['duplicate_of'], pending['submission_id'])
        # 待确认区始终只有一张待审单，受访者数量不变
        self.assertEqual([s['submission_id'] for s in self.store.list_pending()],
                         [pending['submission_id']])
        self.assertEqual(self.store.respondent_count(), 1)
        view = self.store.questionnaire_view('survey-A')
        self.assertIn(again['submission_id'],
                      [v['submission_id'] for v in view['versions']])

    def test_same_filename_different_identity_is_separate_respondent(self):
        b1 = self.builder(qid='survey-A')
        b1.text_field('name', '甲', pages_with_widgets=(1,))
        b2 = self.builder(qid='survey-B')
        b2.text_field('name', '乙', pages_with_widgets=(1,))
        self.store.receive_bytes('same-name.pdf', b1.build_bytes())
        second = self.store.receive_bytes('same-name.pdf', b2.build_bytes())
        self.assertEqual(second['status'], STATUS_ADOPTED)
        self.assertEqual(second['questionnaire_id'], 'survey-B')
        self.assertEqual(self.store.respondent_count(), 2)


class ConfirmationTests(IntakeTestBase):
    def _revision_pair(self):
        b1 = self.builder(revision='1')
        b1.text_field('dealer.name', '甲', pages_with_widgets=(1,))
        first = self.receive(b1)
        b2 = self.builder(revision='2')
        b2.text_field('dealer.name', '甲修订', pages_with_widgets=(1,))
        pending = self.receive(b2)
        return first, pending

    def test_confirm_adopts_revision_and_keeps_previous_evidence(self):
        first, pending = self._revision_pair()
        diff = self.store.diff_pending(pending['submission_id'])
        self.assertEqual(len(diff['changes']), 1)
        self.assertEqual(diff['changes'][0]['adopted']['value'], '甲')
        self.assertEqual(diff['changes'][0]['pending']['value'], '甲修订')
        self.store.confirm(pending['submission_id'], note='核对通过')
        # 首版变为 superseded 但证据仍可读取
        self.assertEqual(self.store.submission_view(first['submission_id'])
                         ['status'], STATUS_SUPERSEDED)
        detail = self.store.field_detail('survey-A', 'dealer.name')
        self.assertEqual(detail['adopted_submission_id'], pending['submission_id'])
        self.assertEqual(detail['adopted_value'], '甲修订')
        old_evidence = detail['versions'][0]['evidence']
        self.assertEqual(read_object(self.root / 'store' / 'raw',
                                     old_evidence['sha256'])[:5],
                         b'%PDF-')
        self.assertEqual([v['status'] for v in detail['versions']],
                         [STATUS_SUPERSEDED, STATUS_ADOPTED])

    def test_reject_keeps_original_adopted_version(self):
        first, pending = self._revision_pair()
        self.store.reject(pending['submission_id'], reason='无法核实身份')
        self.assertEqual(self.store.submission_view(pending['submission_id'])
                         ['status'], STATUS_REJECTED)
        view = self.store.questionnaire_view('survey-A')
        self.assertEqual(view['adopted_submission_id'], first['submission_id'])
        self.assertEqual(view['pending_submission_ids'], [])
        # 被拒绝的原始字节仍然归档保留
        evidence = self.store.submission_view(pending['submission_id'])['evidence']
        read_object(self.root / 'store' / 'raw', evidence['sha256'])

    def test_cannot_confirm_non_pending(self):
        first, pending = self._revision_pair()
        self.store.confirm(pending['submission_id'])
        with self.assertRaises(IntakeError):
            self.store.confirm(pending['submission_id'])
        with self.assertRaises(IntakeError):
            self.store.confirm(first['submission_id'])

    def test_diff_distinguishes_empty_string_from_missing_value(self):
        b1 = self.builder(revision='1')
        b1.text_field('blank', value=None, value_present=True,
                      default_present=False, pages_with_widgets=(1,))
        self.receive(b1)
        b2 = self.builder(revision='2')
        b2.text_field('blank', value_present=False, default_present=False,
                      pages_with_widgets=(1,))
        pending = self.receive(b2)
        diff = self.store.diff_pending(pending['submission_id'])
        change = diff['changes'][0]
        self.assertEqual(change['adopted'],
                         {'value_present': True, 'value': ''})
        self.assertEqual(change['pending'],
                         {'value_present': False, 'value': None})


class UnreadableTests(IntakeTestBase):
    def test_unreadable_pdf_goes_pending_and_batch_continues(self):
        bad = self.store.receive_bytes('broken.pdf', b'not a pdf at all')
        self.assertEqual(bad['status'], STATUS_PENDING)
        self.assertEqual(bad['routing_reason'], UNREADABLE_REASON)
        self.assertTrue(bad['questionnaire_id'].startswith('unknown:'))
        # 不允许把无法识别身份的提交直接确认
        with self.assertRaises(IntakeError):
            self.store.confirm(bad['submission_id'])
        # 同批后续正常文件仍可接收
        good_builder = self.builder(qid='survey-ok')
        good_builder.text_field('name', '甲', pages_with_widgets=(1,))
        good = self.receive(good_builder)
        self.assertEqual(good['status'], STATUS_ADOPTED)

    def test_xfa_only_is_pending_not_silent_empty_success(self):
        builder = self.builder(qid='survey-x')
        builder.xfa_only()
        record = self.receive(builder)
        self.assertEqual(record['status'], STATUS_PENDING)
        self.assertEqual(record['routing_reason'], 'no_readable_fields')
        self.assertEqual(self.store.respondent_count(), 0)

    def test_xfa_reject_then_resubmit_can_be_confirmed(self):
        builder = self.builder(qid='survey-x')
        builder.xfa_only()
        xfa = self.receive(builder)
        self.assertEqual(xfa['status'], STATUS_PENDING)
        self.store.reject(xfa['submission_id'], reason='要求重交 AcroForm')
        # 经销商重新提交可读版本，作为该问卷首版正常进入确认流程
        good = self.builder(qid='survey-x')
        good.text_field('dealer.name', '补甲', pages_with_widgets=(1,))
        resubmit = self.receive(good)
        self.assertEqual(resubmit['status'], STATUS_ADOPTED)
        self.assertEqual(self.store.respondent_count(), 1)
        view = self.store.questionnaire_view('survey-x')
        self.assertEqual(view['adopted_submission_id'], resubmit['submission_id'])
        # 被拒绝的 XFA 原始字节没有被覆盖或删除
        evidence = self.store.submission_view(xfa['submission_id'])['evidence']
        read_object(self.root / 'store' / 'raw', evidence['sha256'])

    def test_anomalies_do_not_block_first_submission(self):
        builder = self.builder(qid='survey-app')
        builder.checkbox_appearance_only('consent', '/Yes')
        record = self.receive(builder)
        self.assertEqual(record['status'], STATUS_ADOPTED)
        self.assertTrue(record['anomalies'])
        self.assertEqual(self.store.respondent_count(), 1)


class PersistenceTests(IntakeTestBase):
    def test_state_reopens_across_store_instances(self):
        builder = self.builder()
        builder.text_field('name', '甲', pages_with_widgets=(1,))
        record = self.store.receive_bytes('a.pdf', builder.build_bytes())
        reopened = IntakeStore(self.root / 'store', clock=Clock())
        self.assertEqual(reopened.respondent_count(), 1)
        self.assertEqual(
            reopened.submission_view(record['submission_id'])['status'],
            STATUS_ADOPTED)

    def test_events_are_append_only_audit_trail(self):
        b1 = self.builder(revision='1')
        b1.text_field('name', '甲', pages_with_widgets=(1,))
        first = self.receive(b1)
        b2 = self.builder(revision='2')
        b2.text_field('name', '乙', pages_with_widgets=(1,))
        pending = self.receive(b2)
        self.store.confirm(pending['submission_id'], note='ok')
        events = self.store.events()
        self.assertEqual([e['type'] for e in events],
                         ['received', 'received', 'confirmed'])
        self.assertEqual(events[-1]['previous_adopted'], first['submission_id'])


if __name__ == '__main__':
    unittest.main()
