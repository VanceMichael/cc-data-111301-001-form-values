"""批次收件入口：归档原始字节、采集填写值、按问卷身份路由修订。

路由规则（不替管理员猜答案）：
- 首次收到某 QuestionnaireID：正常接收为采用版本；
- 同一字节再次交付：记为 duplicate，不新增受访者、不产生待确认；
- 同问卷的新填写值（含同文件名的另一份文件）：进入 pending 待确认区，
  整批其余文件继续处理，争议修订不阻塞批次；
- 没有可读取字段（仅 XFA / 无 AcroForm / 无法解析）：进入 pending 并注明原因，
  不能当成空问卷接收。

证据不可变：原始字节按 SHA-256 内容寻址归档，同一对象只存一份；
状态事件只追加，确认/拒绝不删除任何已接收证据，只记录采用了哪个版本。
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .archive import archive_file
from .extract import (
    ANOMALY_NO_FORM,
    ANOMALY_XFA_ONLY,
    extract_values,
)

STATE_NAME = 'state.json'
RAW_DIR = 'raw'
UNKNOWN_PREFIX = 'unknown:'

STATUS_ADOPTED = 'adopted'
STATUS_PENDING = 'pending'
STATUS_DUPLICATE = 'duplicate'
STATUS_SUPERSEDED = 'superseded'
STATUS_REJECTED = 'rejected'

UNREADABLE_REASON = 'unreadable_pdf'
NO_FIELDS_REASON = 'no_readable_fields'


class IntakeError(Exception):
    """收件流程中的业务错误（区别于文件系统错误）。"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def value_fingerprint(extraction):
    """对逻辑字段实际填写值做规范指纹，用于区分重复交付与真实修订。"""
    snapshot = {
        field['qualified_name']: [field['value_present'], field['value']]
        for field in extraction['fields']
    }
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _value_snapshot(extraction):
    return {
        field['qualified_name']: {
            'value_present': field['value_present'],
            'value': field['value'],
            'default_present': field['default_present'],
            'default_value': field['default_value'],
            'field_type': field['field_type'],
            'logical_field_id': field['logical_field_id'],
        }
        for field in extraction['fields']
    }


class IntakeStore:
    def __init__(self, root, clock=_now):
        self.root = Path(root)
        self.raw_dir = self.root / RAW_DIR
        self.state_path = self.root / STATE_NAME
        self.clock = clock
        self.state = self._load()

    # ---------- 持久化 ----------
    def _load(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        return {'counter': 0, 'submissions': {}, 'questionnaires': {},
                'unidentified': [], 'events': []}

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding='utf-8')
        tmp.replace(self.state_path)

    def _append_event(self, event):
        event = dict(event)
        event['at'] = self.clock()
        self.state['events'].append(event)
        return event

    def _next_id(self):
        self.state['counter'] += 1
        return f'sub-{self.state["counter"]:06d}'

    # ---------- 收件 ----------
    def receive_path(self, path):
        """接收单个文件，返回提交记录；无法解析也返回 pending 记录而不是空成功。"""
        path = Path(path)
        archived = archive_file(path, self.raw_dir)
        return self._register(path.name, archived)

    def receive_bytes(self, filename, data):
        """测试/演示入口：先落临时文件再走与真实文件完全相同的归档路径。"""
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f'.staging-{hashlib.sha256(data).hexdigest()[:12]}'
        staging.write_bytes(data)
        try:
            archived = archive_file(staging, self.raw_dir)
            return self._register(filename, archived)
        finally:
            staging.unlink(missing_ok=True)

    def _register(self, filename, archived):
        sha = archived['sha256']
        # 同一字节重复交付：无论原件处于哪种状态，都记为 duplicate，
        # 不新增受访者、不新增待确认单，并继承原件的问卷身份
        original = next((sub for sub in self.state['submissions'].values()
                         if sub['sha256'] == sha), None)
        if original is not None:
            duplicate = {
                'submission_id': self._next_id(),
                'status': STATUS_DUPLICATE,
                'received_at': self.clock(),
                'filename': filename,
                'sha256': sha,
                'bytes': archived['bytes'],
                'questionnaire_id': original['questionnaire_id'],
                'revision': original['revision'],
                'duplicate_of': original['submission_id'],
                'anomalies': [],
                'routing_reason': 'identical_redelivery',
            }
            self.state['submissions'][duplicate['submission_id']] = duplicate
            qid = original['questionnaire_id']
            if qid in self.state['questionnaires']:
                self.state['questionnaires'][qid]['history'].append(
                    duplicate['submission_id'])
            self._append_event({'type': 'received', **{
                k: duplicate[k] for k in
                ('submission_id', 'status', 'questionnaire_id', 'filename', 'duplicate_of')}})
            self._save()
            return duplicate

        extraction, error = self._safe_extract(archived)
        submission_id = self._next_id()
        if extraction is None:
            record = {
                'submission_id': submission_id,
                'status': STATUS_PENDING,
                'received_at': self.clock(),
                'filename': filename,
                'sha256': sha,
                'bytes': archived['bytes'],
                'questionnaire_id': UNKNOWN_PREFIX + sha[:12],
                'revision': '',
                'anomalies': [],
                'routing_reason': UNREADABLE_REASON,
                'error': error,
                'fields': [],
                'widgets': [],
                'value_fingerprint': None,
                'value_snapshot': {},
            }
            self.state['submissions'][submission_id] = record
            self.state['unidentified'].append(submission_id)
            self._append_event({'type': 'received', 'submission_id': submission_id,
                                'status': record['status'], 'filename': filename,
                                'reason': UNREADABLE_REASON})
            self._save()
            return record

        qid = extraction['questionnaire_id'] or (UNKNOWN_PREFIX + sha[:12])
        no_fields = any(a['code'] in (ANOMALY_XFA_ONLY, ANOMALY_NO_FORM)
                        for a in extraction['anomalies'])
        identity_missing = not extraction['questionnaire_id']
        record = {
            'submission_id': submission_id,
            'status': STATUS_PENDING,
            'received_at': self.clock(),
            'filename': filename,
            'sha256': sha,
            'bytes': archived['bytes'],
            'questionnaire_id': qid,
            'revision': extraction['revision'],
            'pages': extraction['pages'],
            'has_xfa': extraction['has_xfa'],
            'anomalies': extraction['anomalies'],
            'fields': extraction['fields'],
            'widgets': extraction['widgets'],
            'value_fingerprint': value_fingerprint(extraction),
            'value_snapshot': _value_snapshot(extraction),
            'routing_reason': (NO_FIELDS_REASON if no_fields
                               else 'missing_questionnaire_identity' if identity_missing
                               else None),
            'error': None,
        }
        if not qid.startswith(UNKNOWN_PREFIX):
            questionnaire = self.state['questionnaires'].setdefault(qid, {
                'adopted_id': None, 'pending_ids': [], 'history': []})
            questionnaire['history'].append(submission_id)
            if questionnaire['adopted_id'] is None:
                if no_fields:
                    questionnaire['pending_ids'].append(submission_id)
                else:
                    record['status'] = STATUS_ADOPTED
                    record['routing_reason'] = 'first_submission'
                    questionnaire['adopted_id'] = submission_id
            else:
                adopted = self.state['submissions'][questionnaire['adopted_id']]
                if adopted['value_fingerprint'] == record['value_fingerprint']:
                    # 字节不同但填写值完全一致：补交副本，不进待确认，不新增版本
                    record['status'] = STATUS_DUPLICATE
                    record['routing_reason'] = 'same_values_redelivery'
                    record['duplicate_of'] = adopted['submission_id']
                else:
                    questionnaire['pending_ids'].append(submission_id)
                    record['routing_reason'] = record['routing_reason'] or 'value_revision'
        else:
            self.state['unidentified'].append(submission_id)

        self.state['submissions'][submission_id] = record
        self._append_event({'type': 'received', 'submission_id': submission_id,
                            'status': record['status'],
                            'questionnaire_id': record['questionnaire_id'],
                            'filename': filename, 'reason': record['routing_reason']})
        self._save()
        return record

    def _safe_extract(self, archived):
        import io
        from pypdf import PdfReader
        from .archive import read_object
        data = read_object(self.raw_dir, archived['sha256'])
        try:
            return extract_values(PdfReader(io.BytesIO(data))), None
        except Exception as exc:  # 不可解析不产生空成功，进入待确认区
            return None, f'{type(exc).__name__}: {exc}'

    # ---------- 确认/拒绝 ----------
    def confirm(self, submission_id, note=''):
        record = self._require_pending(submission_id)
        qid = record['questionnaire_id']
        if qid.startswith(UNKNOWN_PREFIX):
            raise IntakeError('缺少问卷身份的提交不能直接确认，请先核对来源')
        questionnaire = self.state['questionnaires'][qid]
        previous_id = questionnaire['adopted_id']
        if previous_id:
            previous = self.state['submissions'][previous_id]
            previous['status'] = STATUS_SUPERSEDED
            previous['superseded_by'] = submission_id
        record['status'] = STATUS_ADOPTED
        record['routing_reason'] = record['routing_reason'] or 'confirmed_revision'
        record['confirmed_at'] = self.clock()
        record['confirmation_note'] = note
        questionnaire['adopted_id'] = submission_id
        questionnaire['pending_ids'] = [x for x in questionnaire['pending_ids']
                                        if x != submission_id]
        self._append_event({'type': 'confirmed', 'submission_id': submission_id,
                            'questionnaire_id': qid, 'previous_adopted': previous_id,
                            'note': note})
        self._save()
        return record

    def reject(self, submission_id, reason):
        record = self._require_pending(submission_id)
        record['status'] = STATUS_REJECTED
        record['rejected_at'] = self.clock()
        record['rejection_reason'] = reason
        qid = record['questionnaire_id']
        if qid in self.state['questionnaires']:
            self.state['questionnaires'][qid]['pending_ids'] = [
                x for x in self.state['questionnaires'][qid]['pending_ids']
                if x != submission_id]
        else:
            self.state['unidentified'] = [x for x in self.state['unidentified']
                                          if x != submission_id]
        self._append_event({'type': 'rejected', 'submission_id': submission_id,
                            'questionnaire_id': qid, 'reason': reason})
        self._save()
        return record

    def _require_pending(self, submission_id):
        record = self.state['submissions'].get(submission_id)
        if record is None:
            raise IntakeError(f'提交 {submission_id} 不存在')
        if record['status'] != STATUS_PENDING:
            raise IntakeError(f'提交 {submission_id} 当前状态为 {record["status"]}，不在待确认区')
        return record

    # ---------- 查询 ----------
    def list_pending(self):
        return [dict(self.state['submissions'][sid])
                for sid in sorted(self.state['submissions'])
                if self.state['submissions'][sid]['status'] == STATUS_PENDING]

    def respondent_count(self):
        """受访者数量 = 拥有采用版本的不同 QuestionnaireID 数量；重复交付不增加。"""
        return sum(1 for q in self.state['questionnaires'].values() if q['adopted_id'])

    def overview(self):
        questionnaires = []
        for qid, q in sorted(self.state['questionnaires'].items()):
            adopted = self.state['submissions'][q['adopted_id']] if q['adopted_id'] else None
            questionnaires.append({
                'questionnaire_id': qid,
                'adopted_submission_id': q['adopted_id'],
                'adopted_revision': adopted['revision'] if adopted else None,
                'pending_submission_ids': list(q['pending_ids']),
                'history': list(q['history']),
            })
        counts = {status: sum(1 for s in self.state['submissions'].values()
                              if s['status'] == status)
                  for status in (STATUS_ADOPTED, STATUS_PENDING, STATUS_DUPLICATE,
                                 STATUS_SUPERSEDED, STATUS_REJECTED)}
        return {
            'respondent_count': self.respondent_count(),
            'submission_count': len(self.state['submissions']),
            'status_counts': counts,
            'questionnaires': questionnaires,
            'unidentified_submission_ids': list(self.state['unidentified']),
        }

    def questionnaire_view(self, qid):
        """某问卷的完整收件视图：明确当前采用哪个版本、各版本证据与疑点。"""
        q = self.state['questionnaires'].get(qid)
        if q is None:
            raise IntakeError(f'问卷 {qid} 不存在')
        versions = []
        for sid in q['history']:
            sub = self.state['submissions'][sid]
            versions.append(self._submission_view(sub))
        return {
            'questionnaire_id': qid,
            'adopted_submission_id': q['adopted_id'],
            'pending_submission_ids': list(q['pending_ids']),
            'versions': versions,
        }

    def field_detail(self, qid, qualified_name):
        """点开某个答案：逻辑字段、每个版本的值/默认值、显示页面与原始文件证据。"""
        view = self.questionnaire_view(qid)
        result = {'questionnaire_id': qid, 'qualified_name': qualified_name, 'versions': []}
        for version in view['versions']:
            field = next((f for f in version['fields']
                          if f['qualified_name'] == qualified_name), None)
            widgets = [w for w in version['widgets']
                       if w['qualified_name'] == qualified_name]
            result['versions'].append({
                'submission_id': version['submission_id'],
                'status': version['status'],
                'revision': version['revision'],
                'field': field,
                'widget_locations': [
                    {'widget_object_id': w['object_id'], 'page': w['page'],
                     'rect': w['rect'], 'appearance_state': w['appearance_state'],
                     'on_states': w['on_states']}
                    for w in widgets],
                'evidence': version['evidence'],
            })
        adopted_id = view['adopted_submission_id']
        adopted_version = next((v for v in result['versions']
                                if v['submission_id'] == adopted_id), None)
        result['adopted_submission_id'] = adopted_id
        result['adopted_value'] = None
        result['adopted_value_present'] = None
        if adopted_version and adopted_version['field']:
            result['adopted_value'] = adopted_version['field']['value']
            result['adopted_value_present'] = adopted_version['field']['value_present']
        return result

    def submission_view(self, submission_id):
        sub = self.state['submissions'].get(submission_id)
        if sub is None:
            raise IntakeError(f'提交 {submission_id} 不存在')
        return self._submission_view(sub)

    def _submission_view(self, sub):
        return {
            'submission_id': sub['submission_id'],
            'status': sub['status'],
            'questionnaire_id': sub['questionnaire_id'],
            'revision': sub['revision'],
            'filename': sub['filename'],
            'received_at': sub['received_at'],
            'routing_reason': sub.get('routing_reason'),
            'anomalies': sub.get('anomalies', []),
            'fields': sub.get('fields', []),
            'widgets': sub.get('widgets', []),
            'evidence': {
                'sha256': sub['sha256'],
                'bytes': sub['bytes'],
                'archive_object': str(self.raw_dir / sub['sha256']),
            },
        }

    def diff_pending(self, submission_id):
        """待确认修订与当前采用版本逐字段对比，空字符串与未提交分别呈现。"""
        record = self._require_pending(submission_id)
        qid = record['questionnaire_id']
        q = self.state['questionnaires'].get(qid)
        if not q or not q['adopted_id']:
            return {'submission_id': submission_id, 'changes': [],
                    'message': '该问卷尚无采用版本，确认后即成为首版'}
        adopted = self.state['submissions'][q['adopted_id']]
        names = sorted(set(adopted['value_snapshot']) | set(record['value_snapshot']))
        changes = []
        for name in names:
            old = adopted['value_snapshot'].get(name)
            new = record['value_snapshot'].get(name)
            if old != new:
                changes.append({
                    'qualified_name': name,
                    'adopted': None if old is None else {
                        'value_present': old['value_present'], 'value': old['value']},
                    'pending': None if new is None else {
                        'value_present': new['value_present'], 'value': new['value']},
                })
        return {'submission_id': submission_id,
                'adopted_submission_id': q['adopted_id'], 'changes': changes}

    def events(self):
        return list(self.state['events'])


def receive_batch(store, paths):
    """批量收件：单份争议或损坏不影响其余文件，汇总每份的路由结果。"""
    results = []
    for path in paths:
        results.append(store.receive_path(Path(path)))
    return results
