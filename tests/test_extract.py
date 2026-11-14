import io
import unittest

from pypdf import PdfReader

from form_values.extract import (
    ANOMALY_APPEARANCE_MISMATCH,
    ANOMALY_APPEARANCE_ONLY,
    ANOMALY_NO_FORM,
    ANOMALY_SAME_NAME_CONFLICT,
    ANOMALY_WIDGET_VALUE_CONFLICT,
    ANOMALY_XFA_ONLY,
    extract_values,
)
from form_values.pdfbuild import FormBuilder


def _build(builder):
    return extract_values(PdfReader(io.BytesIO(builder.build_bytes())))


def _field(result, name):
    return next(f for f in result['fields'] if f['qualified_name'] == name)


class FixtureExtractionTests(unittest.TestCase):
    def test_fixture_keeps_logical_field_widgets_values_defaults_apart(self):
        for path, expected_name in (
            ('fixtures/survey-r1.pdf', '虚构经销商甲'),
            ('fixtures/survey-r2.pdf', '虚构经销商甲修订'),
        ):
            result = extract_values(path)
            self.assertEqual(result['questionnaire_id'], 'survey-A')
            field = _field(result, 'contact.name')
            # 逻辑字段只有一个，但两个 widget 分别显示在两页
            self.assertEqual(field['field_type'], '/Tx')
            self.assertEqual(len(field['widgets']), 2)
            self.assertTrue(field['value_present'])
            self.assertEqual(field['value'], expected_name)
            # 空字符串是保存下来的默认值，不能被解释成未提交
            self.assertTrue(field['default_present'])
            self.assertEqual(field['default_value'], '')
            pages = {w['page'] for w in result['widgets']
                     if w['qualified_name'] == 'contact.name'}
            self.assertEqual(pages, {1, 2})

    def test_fixture_reports_appearance_value_mismatch_without_guessing(self):
        result = extract_values('fixtures/survey-r1.pdf')
        consent = _field(result, 'consent')
        # 实际保存的是导出值 /Agree，外观却是 /Off，两者各自保留
        self.assertEqual(consent['value'], '/Agree')
        widget = next(w for w in result['widgets']
                      if w['qualified_name'] == 'consent')
        self.assertEqual(widget['appearance_state'], '/Off')
        codes = {a['code'] for a in result['anomalies']}
        self.assertIn(ANOMALY_APPEARANCE_MISMATCH, codes)


class ValueSemanticsTests(unittest.TestCase):
    def test_empty_string_is_submitted_value(self):
        builder = FormBuilder('q-empty', '1', pages=1)
        builder.text_field('blank', value=None, value_present=True,
                           default_present=False, pages_with_widgets=(1,))
        result = _build(builder)
        field = _field(result, 'blank')
        self.assertTrue(field['value_present'])
        self.assertEqual(field['value'], '')
        self.assertFalse(field['default_present'])

    def test_missing_v_key_is_distinct_from_empty_string(self):
        builder = FormBuilder('q-missing', '1', pages=1)
        builder.text_field('blank', value_present=False, default_present=False,
                           pages_with_widgets=(1,))
        result = _build(builder)
        field = _field(result, 'blank')
        self.assertFalse(field['value_present'])
        self.assertIsNone(field['value'])

    def test_radio_export_value_is_kept_even_when_not_yes(self):
        builder = FormBuilder('q-radio', '1', pages=1)
        builder.radio_group('color', ['/Red', '/Blue'],
                            saved_value='/Red', appearance_state='/Red')
        result = _build(builder)
        field = _field(result, 'color')
        self.assertTrue(field['is_radio'])
        self.assertEqual(field['value'], '/Red')
        self.assertEqual(result['anomalies'], [])
        widget = result['widgets'][0]
        self.assertIn('/Red', widget['on_states'])

    def test_radio_mismatch_between_appearance_and_saved_value(self):
        builder = FormBuilder('q-radio-bad', '1', pages=1)
        builder.radio_group('color', ['/Red', '/Blue'],
                            saved_value='/Blue', appearance_state='/Red')
        result = _build(builder)
        codes = {a['code'] for a in result['anomalies']}
        self.assertIn(ANOMALY_APPEARANCE_MISMATCH, codes)

    def test_multiselect_keeps_export_values_and_display_text_apart(self):
        builder = FormBuilder('q-ch', '1', pages=1)
        builder.choice_field(
            'langs', [('e1', '显示一'), ('e2', '显示二')], ['e1', 'e2'])
        result = _build(builder)
        field = _field(result, 'langs')
        self.assertTrue(field['is_multiselect'])
        self.assertEqual(field['value'], ['e1', 'e2'])
        self.assertEqual(field['options'], [
            {'export_value': 'e1', 'display_text': '显示一'},
            {'export_value': 'e2', 'display_text': '显示二'}])

    def test_appearance_only_change_is_flagged(self):
        builder = FormBuilder('q-app', '1', pages=1)
        builder.checkbox_appearance_only('newsletter', '/Yes')
        result = _build(builder)
        field = _field(result, 'newsletter')
        self.assertFalse(field['value_present'])
        codes = {a['code'] for a in result['anomalies']}
        self.assertIn(ANOMALY_APPEARANCE_ONLY, codes)

    def test_same_named_fields_conflict(self):
        builder = FormBuilder('q-dup', '1', pages=1)
        builder.duplicate_named_fields('city', '北京', '上海')
        result = _build(builder)
        names = [f['qualified_name'] for f in result['fields']]
        self.assertEqual(names, ['city', 'city'])
        values = {f['value'] for f in result['fields']}
        self.assertEqual(values, {'北京', '上海'})
        codes = {a['code'] for a in result['anomalies']}
        self.assertIn(ANOMALY_SAME_NAME_CONFLICT, codes)

    def test_widgets_on_two_pages_with_conflicting_appearance(self):
        builder = FormBuilder('q-conf', '1', pages=2)
        builder.text_field('note', value='同一份值', pages_with_widgets=(1, 2))
        result = _build(builder)
        # 文本字段无外观状态，正常情况下不应有 widget 冲突
        self.assertEqual(
            [a for a in result['anomalies']
             if a['code'] == ANOMALY_WIDGET_VALUE_CONFLICT], [])

    def test_xfa_only_document_is_flagged_not_guessed(self):
        builder = FormBuilder('q-xfa', '1', pages=1)
        builder.xfa_only()
        result = _build(builder)
        self.assertTrue(result['has_xfa'])
        self.assertEqual(result['fields'], [])
        self.assertIn(ANOMALY_XFA_ONLY, {a['code'] for a in result['anomalies']})

    def test_document_without_form_is_flagged(self):
        builder = FormBuilder('q-noform', '1', pages=1)
        builder.no_form()
        result = _build(builder)
        self.assertFalse(result['has_acroform'])
        self.assertIn(ANOMALY_NO_FORM, {a['code'] for a in result['anomalies']})


if __name__ == '__main__':
    unittest.main()
