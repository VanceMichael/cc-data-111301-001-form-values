from pathlib import Path
from pypdf import PdfReader


def inspect_file(path):
    reader = PdfReader(Path(path))
    form = reader.trailer['/Root'].get('/AcroForm')
    fields = form.get_object().get('/Fields', []) if form else []
    return {'pages': len(reader.pages), 'top_fields': [str(field.get_object().get('/T', '')) for field in fields],
            'questionnaire': str(reader.metadata.get('/QuestionnaireID', '')),
            'revision': str(reader.metadata.get('/Revision', ''))}
