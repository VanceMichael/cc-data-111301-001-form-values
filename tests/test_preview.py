from pathlib import Path
import tempfile
import unittest
import pypdf.errors
from form_values.core import inspect_file


class PreviewTests(unittest.TestCase):
    def test_source_preview(self):
        result = inspect_file(Path('fixtures/survey-r1.pdf'))
        self.assertEqual(result['pages'], 2)
        self.assertEqual(result['top_fields'], ['contact', 'consent'])

    def test_invalid_source_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'invalid'
            path.write_bytes(b'not a valid source')
            with self.assertRaises(pypdf.errors.PdfReadError):
                inspect_file(path)

    def test_missing_source_is_not_empty_success(self):
        with self.assertRaises(FileNotFoundError):
            inspect_file(Path('fixtures/does-not-exist'))
