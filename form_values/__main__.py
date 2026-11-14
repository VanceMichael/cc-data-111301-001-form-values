DESCRIPTION = '调查 PDF 表单的字段身份与填写值采集'
SAMPLE = 'fixtures/survey-r1.pdf'

import argparse
import io
import json
from pathlib import Path
import sys
import tempfile

from .archive import archive_file
from .core import inspect_file
from .extract import extract_values
from .intake import (
    IntakeError,
    IntakeStore,
    STATUS_PENDING,
)


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _expand_paths(paths):
    files = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob('*') if p.is_file()
                                and p.suffix.lower() == '.pdf'))
        else:
            files.append(path)
    return files


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command', choices=[
        'inspect', 'capture', 'receive', 'batch', 'pending', 'confirm',
        'reject', 'questionnaire', 'answer', 'overview', 'demo'])
    parser.add_argument('path', nargs='*', default=None,
                        help='文件路径；batch 可给多个文件或目录')
    parser.add_argument('--archive', default='.work/raw')
    parser.add_argument('--store', default='.work/intake')
    parser.add_argument('--note', default='')
    parser.add_argument('--reason', default='')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    command = args.command
    if command == 'inspect':
        _print(inspect_file(Path(args.path[0] if args.path else SAMPLE)))
        return 0
    if command == 'capture':
        _print(archive_file(Path(args.path[0] if args.path else SAMPLE),
                            Path(args.archive)))
        return 0
    if command == 'demo':
        _print(run_demo())
        return 0

    store = IntakeStore(Path(args.store))
    if command == 'receive':
        if not args.path:
            raise SystemExit('receive 需要一个 PDF 文件路径')
        _print(store.submission_view(store.receive_path(Path(args.path[0]))['submission_id']))
        return 0
    if command == 'batch':
        paths = _expand_paths(args.path or [])
        if not paths:
            raise SystemExit('batch 至少需要一个 PDF 文件或目录')
        receipts = [store.receive_path(path) for path in paths]
        _print({'received': len(receipts),
                'results': [store.submission_view(r['submission_id']) for r in receipts],
                'overview': store.overview()})
        # 全部损坏才算命令失败；个别争议文件不阻塞批次
        return 0 if any(r['status'] != STATUS_PENDING or r.get('routing_reason') != 'unreadable_pdf'
                        for r in receipts) else 1
    if command == 'pending':
        _print({'pending': store.list_pending(), 'diff': {
            r['submission_id']: store.diff_pending(r['submission_id'])
            for r in store.list_pending()}})
        return 0
    if command == 'confirm':
        if not args.path:
            raise SystemExit('confirm 需要提交编号')
        record = store.confirm(args.path[0], note=args.note)
        _print({'confirmed': store.submission_view(record['submission_id']),
                'questionnaire': store.questionnaire_view(record['questionnaire_id'])})
        return 0
    if command == 'reject':
        if not args.path:
            raise SystemExit('reject 需要提交编号')
        if not args.reason:
            raise SystemExit('reject 必须用 --reason 说明拒绝原因')
        record = store.reject(args.path[0], reason=args.reason)
        _print({'rejected': store.submission_view(record['submission_id'])})
        return 0
    if command == 'questionnaire':
        if not args.path:
            raise SystemExit('questionnaire 需要 QuestionnaireID')
        _print(store.questionnaire_view(args.path[0]))
        return 0
    if command == 'answer':
        if len(args.path or []) < 2:
            raise SystemExit('answer 需要 QuestionnaireID 与限定字段名')
        _print(store.field_detail(args.path[0], args.path[1]))
        return 0
    if command == 'overview':
        _print(store.overview())
        return 0
    return 1


def run_demo():
    """用项目虚构问卷走一次完整收件与修订确认（含同文件名、重复交付与疑点）。"""
    from .pdfbuild import FormBuilder
    clock = _DemoClock()
    with tempfile.TemporaryDirectory() as directory:
        store = IntakeStore(Path(directory) / 'intake', clock=clock.next)

        def stage(name, data, workdir):
            target = Path(workdir) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return target

        with tempfile.TemporaryDirectory() as inbox:
            inbox = Path(inbox)
            # 1. 两位不同经销商各交一份，其中一份与现有文件同名，验证文件名不参与身份
            r1 = stage('survey.pdf', Path(SAMPLE).read_bytes(), inbox)
            r2_pdf = FormBuilder('survey-B', '1', pages=1)
            r2_pdf.text_field('dealer.name', '虚构经销商乙', pages_with_widgets=(1,))
            r2 = stage('survey.pdf', r2_pdf.build_bytes(), inbox / 'second')
            first = store.receive_path(r1)
            second = store.receive_path(r2)

            # 2. 同一经销商补交修订版（fixture r2），字段值发生变化 -> 待确认
            revision = stage('survey.pdf',
                             Path('fixtures/survey-r2.pdf').read_bytes(),
                             inbox / 'revision')
            pending_rev = store.receive_path(revision)

            # 3. 完全相同的文件再交一遍 -> duplicate，受访者数量不变
            redelivery = store.receive_path(revision)

            # 4. 只有勾选外观、没有保存值的文件 -> 疑点随件保留
            appearance_only = FormBuilder('survey-C', '1', pages=1)
            appearance_only.checkbox_appearance_only('consent', '/Yes')
            ao_path = stage('appearance-only.pdf', appearance_only.build_bytes(), inbox)
            ao_record = store.receive_path(ao_path)

            # 5. 只有 XFA 的文件 -> 待确认，不被当成空问卷接收
            xfa = FormBuilder('survey-D', '1', pages=1)
            xfa.xfa_only()
            xfa_path = stage('xfa-only.pdf', xfa.build_bytes(), inbox)
            xfa_record = store.receive_path(xfa_path)

            pending_before = store.list_pending()
            diff = store.diff_pending(pending_rev['submission_id'])
            # 行政同事核对原文件、字段与页面后确认修订
            confirmed = store.confirm(pending_rev['submission_id'],
                                      note='已与经销商电话核对，采用修订版')
            answer_trace = store.field_detail('survey-A', 'contact.name')
            rejected = store.reject(xfa_record['submission_id'],
                                    reason='仅含 XFA，无法读取字段，要求重新提交 AcroForm')
            overview = store.overview()

        return {
            '首次接收': {
                'survey-A': _brief(store.submission_view(first['submission_id'])),
                'survey-B': _brief(store.submission_view(second['submission_id'])),
            },
            '修订交付': _brief(store.submission_view(pending_rev['submission_id'])),
            '重复交付': _brief(store.submission_view(redelivery['submission_id'])),
            '外观疑点文件': _brief(store.submission_view(ao_record['submission_id'])),
            'XFA文件': _brief(store.submission_view(xfa_record['submission_id'])),
            '确认前待确认区': [r['submission_id'] for r in pending_before],
            '修订字段对比': diff,
            '确认结果': _brief(store.submission_view(confirmed['submission_id'])),
            '答案溯源_contact.name': answer_trace,
            '拒绝XFA': _brief(store.submission_view(rejected['submission_id'])),
            '汇总结论': overview,
            '事件流水': store.events(),
        }


class _DemoClock:
    def __init__(self):
        self.tick = 0

    def next(self):
        self.tick += 1
        return f'2026-11-14T0{min(self.tick, 9)}:00:00+00:00'


def _brief(view):
    return {key: view.get(key) for key in (
        'submission_id', 'status', 'questionnaire_id', 'revision', 'filename',
        'routing_reason', 'anomalies', 'evidence')}


if __name__ == '__main__':
    try:
        sys.exit(main())
    except IntakeError as exc:
        print(f'收件操作失败：{exc}', file=sys.stderr)
        sys.exit(1)
