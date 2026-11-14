"""从已填写 PDF 中采集逻辑字段、显示位置、实际填写值与默认值。

设计原则（见 docs/data.md）：
- 不做 OCR，只读取 AcroForm/XFA 结构中真正保存的字段值与外观状态；
- 逻辑字段、widget 显示位置、实际填写值 (/V)、默认值 (/DV) 各自保留；
- 空字符串是已提交的值，与“字段没有 /V 键”严格区分；
- 单选/多选只保留原始导出值，显示文字另列，绝不互相替换；
- 发现疑点只如实记录 anomaly，软件不替使用者猜测答案。
"""
from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
    TextStringObject,
)

# 字段标志位（PDF 1.7 表 8.69/8.70/8.72/8.77）
FLAG_RADIO = 1 << 15       # /Btn 单选组（否则为复选框/按钮）
FLAG_PUSHBUTTON = 1 << 16  # /Btn 下压按钮（不持有值）
FLAG_COMBO = 1 << 17       # /Ch 组合框
FLAG_MULTISELECT = 1 << 21  # /Ch 多选

INHERITABLE = ('/FT', '/Ff', '/V', '/DV', '/Opt')

ANOMALY_APPEARANCE_MISMATCH = 'appearance_value_mismatch'
ANOMALY_APPEARANCE_ONLY = 'appearance_only_change'
ANOMALY_SAME_NAME_CONFLICT = 'same_name_conflict'
ANOMALY_WIDGET_VALUE_CONFLICT = 'widget_value_conflict'
ANOMALY_UNKNOWN_APPEARANCE_STATE = 'unknown_appearance_state'
ANOMALY_XFA_ONLY = 'xfa_only'
ANOMALY_NO_FORM = 'no_form'


def _ref_id(obj):
    """返回稳定的对象引用标识；直接对象没有间接引用编号。"""
    if isinstance(obj, IndirectObject):
        return f'{obj.idnum}:{obj.generation}'
    return 'direct'


def _inherited(field, key):
    """沿 /Parent 链解析可继承属性；属性不存在与值为 null 都如实返回。"""
    node = field
    while node is not None:
        node = node.get_object() if isinstance(node, IndirectObject) else node
        if key in node:
            return node[key], True
        node = node.get('/Parent')
    return None, False


def _decode_value(value):
    """把 PDF 通用对象转成 JSON 安全的 Python 值，保留原始语义。"""
    if isinstance(value, IndirectObject):
        return _decode_value(value.get_object())
    if isinstance(value, NullObject):
        return None
    if isinstance(value, BooleanObject):
        return bool(value)
    if isinstance(value, (NumberObject, FloatObject)):
        return value.as_numeric() if hasattr(value, 'as_numeric') else float(value)
    if isinstance(value, NameObject):
        # 名称是 PDF 内部 token（如 /Agree、/Off），保留原文不做翻译
        return str(value)
    if isinstance(value, TextStringObject):
        return str(value)
    if isinstance(value, ArrayObject):
        return [_decode_value(item) for item in value]
    if isinstance(value, DictionaryObject):
        return {str(key): _decode_value(item) for key, item in value.items()}
    if value is None:
        return None
    return str(value)


def _has_value(field):
    """字段是否真正保存了 /V 键（空字符串也算已保存）。"""
    node = field
    while node is not None:
        node = node.get_object() if isinstance(node, IndirectObject) else node
        if '/V' in node:
            return True
        node = node.get('/Parent')
    return False


def _on_states(widget):
    """取 widget 外观字典 /AP /N 中除 /Off 外的外观状态名（原始导出值）。"""
    appearance = widget.get('/AP')
    if not appearance:
        return []
    appearance = appearance.get_object() if isinstance(appearance, IndirectObject) else appearance
    normal = appearance.get('/N')
    if not normal:
        return []
    normal = normal.get_object() if isinstance(normal, IndirectObject) else normal
    if not isinstance(normal, DictionaryObject):
        return []
    return [str(name) for name in normal.keys() if str(name) != '/Off']


def _options(field):
    """选择字段 /Opt 同时保留导出值与显示文字。"""
    raw, present = _inherited(field, '/Opt')
    if not present:
        return None
    raw = raw.get_object() if isinstance(raw, IndirectObject) else raw
    options = []
    for entry in raw:
        entry = entry.get_object() if isinstance(entry, IndirectObject) else entry
        if isinstance(entry, ArrayObject) and len(entry) == 2:
            options.append({'export_value': _decode_value(entry[0]),
                            'display_text': _decode_value(entry[1])})
        else:
            options.append({'export_value': _decode_value(entry), 'display_text': None})
    return options


def _field_type(field):
    ft, present = _inherited(field, '/FT')
    return str(ft) if present else None


def _flags(field):
    flags, present = _inherited(field, '/Ff')
    return int(flags) if present else 0


class _Extractor:
    def __init__(self, reader):
        self.reader = reader
        # reader.pages 返回包装对象，与 widget /P 解析出的字典不是同一 Python 对象；
        # 沿 /Pages 树按间接引用编号建立页码映射
        self.page_index = {}
        root_pages = reader.trailer['/Root'].get('/Pages')
        if root_pages is not None:
            counter = 0

            def visit(pages_ref):
                nonlocal counter
                pages = pages_ref.get_object() if isinstance(pages_ref, IndirectObject) else pages_ref
                for kid in pages.get('/Kids', []):
                    kid_obj = kid.get_object() if isinstance(kid, IndirectObject) else kid
                    if str(kid_obj.get('/Type', '')) == '/Pages':
                        visit(kid)
                    else:
                        counter += 1
                        if isinstance(kid, IndirectObject):
                            self.page_index[kid.idnum] = counter
                        self.page_index[id(kid_obj)] = counter

            visit(root_pages)
        self.fields = []
        self.widgets = []
        self.anomalies = []
        self._field_names = {}

    def _page_number(self, widget):
        page = widget.get('/P')
        if isinstance(page, IndirectObject):
            if page.idnum in self.page_index:
                return self.page_index[page.idnum]
            page_obj = page.get_object()
        else:
            page_obj = page
        if page_obj is None:
            return None
        return self.page_index.get(id(page_obj))

    def _record_widget(self, node, name_path, logical_id):
        widget = node.get_object() if isinstance(node, IndirectObject) else node
        ft = _field_type(widget)
        flags = _flags(widget)
        direct_v = '/V' in widget
        record = {
            'object_id': _ref_id(node),
            'logical_field_id': logical_id,
            'qualified_name': '.'.join(name_path) if name_path else None,
            'page': self._page_number(widget),
            'rect': [float(x) for x in widget['/Rect']] if '/Rect' in widget else None,
            'field_type': ft,
            'appearance_state': str(widget['/AS']) if '/AS' in widget else None,
            'on_states': _on_states(widget),
            'value_present_on_widget': direct_v,
            'widget_value': _decode_value(widget['/V']) if direct_v else None,
            'is_radio': bool(flags & FLAG_RADIO),
            'is_pushbutton': bool(flags & FLAG_PUSHBUTTON),
        }
        self.widgets.append(record)
        return record

    def _record_field(self, node, name_path):
        field = node.get_object() if isinstance(node, IndirectObject) else node
        qualified = '.'.join(name_path)
        ft = _field_type(field)
        flags = _flags(field)
        value_present = _has_value(field)
        raw_v, _ = _inherited(field, '/V')
        raw_dv, dv_present = _inherited(field, '/DV')
        logical_id = _ref_id(node)
        record = {
            'logical_field_id': logical_id,
            'qualified_name': qualified,
            'name_path': list(name_path),
            'field_type': ft,
            'flags': flags,
            'is_radio': bool(flags & FLAG_RADIO),
            'is_pushbutton': bool(flags & FLAG_PUSHBUTTON),
            'is_multiselect': bool(flags & FLAG_MULTISELECT),
            'is_combo': bool(flags & FLAG_COMBO),
            'value_present': value_present,
            # 实际填写值：没有 /V 键时为 None 且 value_present=False；
            # 有 /V 键但内容为空字符串时 value_present=True，value=''
            'value': _decode_value(raw_v) if value_present else None,
            'default_present': dv_present,
            'default_value': _decode_value(raw_dv) if dv_present else None,
            'options': _options(field),
            'widgets': [],
        }
        self.fields.append(record)
        existing = self._field_names.get(qualified)
        if existing is not None:
            self.anomalies.append({
                'code': ANOMALY_SAME_NAME_CONFLICT,
                'qualified_name': qualified,
                'field_object_ids': [existing, logical_id],
                'message': f'逻辑字段 {qualified} 在文件中出现多个同名字段对象，值可能互相冲突',
            })
        else:
            self._field_names[qualified] = logical_id
        return record

    def _walk(self, node, name_path):
        field = node.get_object() if isinstance(node, IndirectObject) else node
        # 纯 widget 注解：无 /T、/FT、/Kids，只有 /Subtype /Widget
        is_pure_widget = (
            str(field.get('/Subtype', '')) == '/Widget'
            and '/T' not in field and '/FT' not in field and '/Kids' not in field
        )
        if is_pure_widget:
            # 挂到最近的终端逻辑字段（由调用方保证 name_path 即该字段全名）
            logical_id = self._field_names.get('.'.join(name_path))
            widget_record = self._record_widget(node, name_path, logical_id)
            if logical_id:
                self.fields_by_id[logical_id]['widgets'].append(widget_record['object_id'])
            return

        path = name_path + [str(field['/T'])] if '/T' in field else list(name_path)
        kids = field.get('/Kids')
        has_field_kids = False
        if kids:
            for child in kids:
                child_obj = child.get_object() if isinstance(child, IndirectObject) else child
                if not (str(child_obj.get('/Subtype', '')) == '/Widget'
                        and '/T' not in child_obj and '/FT' not in child_obj and '/Kids' not in child_obj):
                    has_field_kids = True
                    break

        if has_field_kids:
            # 容器字段（可能只有名称与默认属性，供子节点继承）
            for child in kids:
                child_obj = child.get_object() if isinstance(child, IndirectObject) else child
                is_widget_child = (
                    str(child_obj.get('/Subtype', '')) == '/Widget'
                    and '/T' not in child_obj and '/FT' not in child_obj and '/Kids' not in child_obj
                )
                if is_widget_child:
                    logical_id = self._field_names.get('.'.join(path))
                    widget_record = self._record_widget(child, path, logical_id)
                    if logical_id:
                        self.fields_by_id[logical_id]['widgets'].append(widget_record['object_id'])
                else:
                    self._walk(child, path)
            return

        # 终端逻辑字段
        field_record = self._record_field(node, path)
        self.fields_by_id[field_record['logical_field_id']] = field_record
        if kids:
            for child in kids:
                child_obj = child.get_object() if isinstance(child, IndirectObject) else child
                if str(child_obj.get('/Subtype', '')) == '/Widget':
                    widget_record = self._record_widget(child, path, field_record['logical_field_id'])
                    field_record['widgets'].append(widget_record['object_id'])
        elif str(field.get('/Subtype', '')) == '/Widget':
            # 字段与 widget 合并为同一个字典
            widget_record = self._record_widget(node, path, field_record['logical_field_id'])
            field_record['widgets'].append(widget_record['object_id'])

    def check_anomalies(self):
        # 1. 外观状态 vs 实际保存值；只有勾选外观而无字段值
        for widget in self.widgets:
            as_state = widget['appearance_state']
            on_states = widget['on_states']
            logical = self.fields_by_id.get(widget['logical_field_id']) if widget['logical_field_id'] else None
            field_has_value = logical['value_present'] if logical else widget['value_present_on_widget']
            field_value = logical['value'] if logical else widget['widget_value']
            if as_state is not None and as_state != '/Off' and on_states and as_state not in on_states:
                self.anomalies.append({
                    'code': ANOMALY_UNKNOWN_APPEARANCE_STATE,
                    'qualified_name': widget['qualified_name'],
                    'widget_object_id': widget['object_id'],
                    'page': widget['page'],
                    'appearance_state': as_state,
                    'known_states': ['/Off'] + on_states,
                    'message': (f'第 {widget["page"]} 页 widget 的外观状态 {as_state} '
                                f'不在其外观字典允许的状态中'),
                })
            if widget['field_type'] == '/Btn' and as_state is not None:
                # 勾选类字段：两个方向的不符都要报——
                # 外观勾选但没有保存值；外观/Off 但保存了激活值；外观与保存值指向不同选项。
                # 单选组里未选中的 widget 本来就是 /Off，只对“当前承载激活外观”的
                # widget（或非单选复选框）做 /Off 与组值的比对。
                if as_state != '/Off' and not field_has_value:
                    self.anomalies.append({
                        'code': ANOMALY_APPEARANCE_ONLY,
                        'qualified_name': widget['qualified_name'],
                        'widget_object_id': widget['object_id'],
                        'page': widget['page'],
                        'appearance_state': as_state,
                        'message': (f'第 {widget["page"]} 页 widget 显示 {as_state} 勾选外观，'
                                    f'但字段没有保存任何 /V 值，无法确认实际填写值'),
                    })
                elif field_has_value and field_value is not None:
                    if as_state == '/Off':
                        if not widget['is_radio']:
                            self.anomalies.append({
                                'code': ANOMALY_APPEARANCE_MISMATCH,
                                'qualified_name': widget['qualified_name'],
                                'widget_object_id': widget['object_id'],
                                'page': widget['page'],
                                'appearance_state': as_state,
                                'saved_value': field_value,
                                'message': (f'第 {widget["page"]} 页 widget 外观为未勾选 {as_state}，'
                                            f'而字段实际保存了激活值 {field_value!r}'),
                            })
                    elif str(field_value) != as_state:
                        self.anomalies.append({
                            'code': ANOMALY_APPEARANCE_MISMATCH,
                            'qualified_name': widget['qualified_name'],
                            'widget_object_id': widget['object_id'],
                            'page': widget['page'],
                            'appearance_state': as_state,
                            'saved_value': field_value,
                            'message': (f'第 {widget["page"]} 页 widget 外观为 {as_state}，'
                                        f'而字段实际保存值为 {field_value!r}'),
                        })
            # widget 自带 /V 且与逻辑字段 /V 不一致（同名字段互相冲突的一种来源）
            if widget['value_present_on_widget'] and logical and logical['value_present']:
                if widget['widget_value'] != logical['value']:
                    self.anomalies.append({
                        'code': ANOMALY_WIDGET_VALUE_CONFLICT,
                        'qualified_name': widget['qualified_name'],
                        'widget_object_id': widget['object_id'],
                        'page': widget['page'],
                        'widget_value': widget['widget_value'],
                        'field_value': logical['value'],
                        'message': (f'第 {widget["page"]} 页 widget 自带值 {widget["widget_value"]!r} '
                                    f'与逻辑字段保存值 {logical["value"]!r} 不一致'),
                    })

        # 2. 同一逻辑字段的多个 widget 外观状态互相冲突；单选组外观与组值的整体比对
        for field in self.fields:
            own_widgets = [w for w in self.widgets if w['logical_field_id'] == field['logical_field_id']]
            active = sorted({w['appearance_state'] for w in own_widgets
                             if w['appearance_state'] and w['appearance_state'] != '/Off'})
            if len(active) > 1:
                self.anomalies.append({
                    'code': ANOMALY_WIDGET_VALUE_CONFLICT,
                    'qualified_name': field['qualified_name'],
                    'field_object_id': field['logical_field_id'],
                    'widget_object_ids': [w['object_id'] for w in own_widgets],
                    'appearance_states': active,
                    'message': (f'逻辑字段 {field["qualified_name"]} 的多个 widget '
                                f'分别显示 {active}，显示位置之间互相冲突'),
                })
            if field['is_radio'] and field['value_present'] and field['value'] is not None:
                widget_level = {a['code'] for a in self.anomalies
                                if a.get('qualified_name') == field['qualified_name']
                                and a['code'] == ANOMALY_APPEARANCE_MISMATCH}
                if not active:
                    self.anomalies.append({
                        'code': ANOMALY_APPEARANCE_MISMATCH,
                        'qualified_name': field['qualified_name'],
                        'field_object_id': field['logical_field_id'],
                        'appearance_state': '/Off',
                        'saved_value': field['value'],
                        'message': (f'单选组 {field["qualified_name"]} 所有 widget 外观均为未勾选，'
                                    f'而字段实际保存了激活值 {field["value"]!r}'),
                    })
                elif str(field['value']) not in active and not widget_level:
                    self.anomalies.append({
                        'code': ANOMALY_APPEARANCE_MISMATCH,
                        'qualified_name': field['qualified_name'],
                        'field_object_id': field['logical_field_id'],
                        'appearance_states': active,
                        'saved_value': field['value'],
                        'message': (f'单选组 {field["qualified_name"]} widget 激活外观为 {active}，'
                                    f'而字段实际保存值为 {field["value"]!r}'),
                    })

    def run(self):
        root = self.reader.trailer['/Root']
        form = root.get('/AcroForm')
        form = form.get_object() if isinstance(form, IndirectObject) else form
        has_xfa = bool(form and form.get('/XFA'))
        self.fields_by_id = {}
        if form:
            for top_field in form.get('/Fields', []):
                self._walk(top_field, [])
        self.check_anomalies()
        if not self.fields:
            self.anomalies.append({
                'code': ANOMALY_XFA_ONLY if has_xfa else ANOMALY_NO_FORM,
                'message': ('文件只包含 XFA，没有可读取的 AcroForm 字段' if has_xfa
                            else '文件没有可读取的 AcroForm 字段'),
            })
        return {
            'pages': len(self.reader.pages),
            'has_xfa': has_xfa,
            'has_acroform': form is not None,
            'questionnaire_id': str(self.reader.metadata.get('/QuestionnaireID', '')) if self.reader.metadata else '',
            'revision': str(self.reader.metadata.get('/Revision', '')) if self.reader.metadata else '',
            'fields': self.fields,
            'widgets': self.widgets,
            'anomalies': self.anomalies,
        }


def extract_values(reader_or_path):
    """采集一份已填写 PDF 的字段身份、位置、填写值、默认值与疑点。"""
    if isinstance(reader_or_path, PdfReader):
        reader = reader_or_path
    else:
        reader = PdfReader(reader_or_path)
    return _Extractor(reader).run()
