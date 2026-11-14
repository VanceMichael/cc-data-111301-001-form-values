"""构造虚构 PDF 表单的测试/演示工厂。

仅用于本项目自己的测试与 demo，不读取任何外部资料。所有问卷身份与业务数值
均为虚构。用 pypdf 直接写字典，便于精确制造“只改外观不存值”“同名冲突”
“只有 XFA”等真实收件中会遇到、但现成 fixture 未覆盖的结构。
"""
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

FLAG_RADIO = 1 << 15
FLAG_PUSHBUTTON = 1 << 16
FLAG_MULTISELECT = 1 << 21


def _name(value):
    return NameObject(value)


def _rect(values):
    return ArrayObject([NumberObject(int(v)) if float(v).is_integer() else NumberObject(float(v))
                        for v in values])


class FormBuilder:
    def __init__(self, questionnaire_id, revision, pages=2):
        self.writer = PdfWriter()
        self.page_refs = [self.writer.add_blank_page(width=300, height=400)
                          for _ in range(pages)]
        self.top_fields = []
        self._no_form = False
        self.writer.add_metadata({'/QuestionnaireID': questionnaire_id,
                                  '/Revision': str(revision)})

    def _page_ref(self, page_number):
        return self.page_refs[page_number - 1].indirect_reference if hasattr(
            self.page_refs[page_number - 1], 'indirect_reference') else self.page_refs[page_number - 1]

    def text_field(self, name_path, value=None, value_present=True, default=None,
                   default_present=True, pages_with_widgets=(1, 2), rect=(10, 10, 200, 40)):
        """构造文本逻辑字段，可挂多个 widget 到不同页面。"""
        name_path = [name_path] if isinstance(name_path, str) else list(name_path)
        terminal = DictionaryObject({
            _name('/T'): TextStringObject(name_path[-1]),
            _name('/FT'): _name('/Tx'),
        })
        if value_present:
            terminal[_name('/V')] = TextStringObject('' if value is None else str(value))
        if default_present:
            terminal[_name('/DV')] = TextStringObject('' if default is None else str(default))
        terminal_ref = self.writer._add_object(terminal)
        kid_refs = []
        for page_number in pages_with_widgets:
            widget = DictionaryObject({
                _name('/Type'): _name('/Annot'),
                _name('/Subtype'): _name('/Widget'),
                _name('/Parent'): terminal_ref,
                _name('/P'): self._page_ref(page_number),
                _name('/Rect'): _rect(rect),
            })
            ref = self.writer._add_object(widget)
            kid_refs.append(ref)
        terminal[_name('/Kids')] = ArrayObject(kid_refs)
        self._attach(terminal_ref, name_path)
        return terminal_ref

    def radio_group(self, qualified_name, export_states, saved_value=None, value_present=True,
                    appearance_state=None, page=1):
        """单选组：导出值由调用方给定（不一定是 Yes），外观状态可与保存值故意不符。"""
        field = DictionaryObject({
            _name('/T'): TextStringObject(qualified_name),
            _name('/FT'): _name('/Btn'),
            _name('/Ff'): NumberObject(FLAG_RADIO),
        })
        if value_present:
            field[_name('/V')] = _name(saved_value)
        field_ref = self.writer._add_object(field)
        kid_refs = []
        wanted = appearance_state if appearance_state and str(appearance_state).startswith('/') \
            else ('/' + appearance_state if appearance_state else None)
        for state in export_states:
            appearance = DictionaryObject({
                _name('/N'): DictionaryObject({
                    _name('/Off'): self.writer._add_object(DictionaryObject()),
                    _name(state): self.writer._add_object(DictionaryObject()),
                })
            })
            widget_state = wanted if state == wanted else '/Off'
            widget = DictionaryObject({
                _name('/Type'): _name('/Annot'),
                _name('/Subtype'): _name('/Widget'),
                _name('/Parent'): field_ref,
                _name('/P'): self._page_ref(page),
                _name('/Rect'): _rect((20, 80, 40, 100)),
                _name('/AS'): _name(widget_state),
                _name('/AP'): appearance,
            })
            kid_refs.append(self.writer._add_object(widget))
        field[_name('/Kids')] = ArrayObject(kid_refs)
        self.top_fields.append(field_ref)
        return field_ref

    def checkbox_appearance_only(self, qualified_name, appearance_state, page=1):
        """复选框：只有勾选外观，字段上不保存任何 /V。"""
        appearance = DictionaryObject({
            _name('/N'): DictionaryObject({
                _name('/Off'): self.writer._add_object(DictionaryObject()),
                _name(appearance_state): self.writer._add_object(DictionaryObject()),
            })
        })
        field = DictionaryObject({
            _name('/T'): TextStringObject(qualified_name),
            _name('/FT'): _name('/Btn'),
            _name('/Subtype'): _name('/Widget'),
            _name('/P'): self._page_ref(page),
            _name('/Rect'): _rect((20, 80, 40, 100)),
            _name('/AS'): _name(appearance_state),
            _name('/AP'): appearance,
        })
        ref = self.writer._add_object(field)
        self.top_fields.append(ref)
        return ref

    def choice_field(self, qualified_name, options, saved_values, multiselect=True,
                     default='', default_present=True, page=1):
        """选择字段：options 为 (导出值, 显示文字) 元组，导出值与显示文字刻意不同。"""
        opt_array = ArrayObject([
            ArrayObject([TextStringObject(export_value), TextStringObject(display)])
            for export_value, display in options
        ])
        field = DictionaryObject({
            _name('/T'): TextStringObject(qualified_name),
            _name('/FT'): _name('/Ch'),
            _name('/Opt'): opt_array,
            _name('/Subtype'): _name('/Widget'),
            _name('/P'): self._page_ref(page),
            _name('/Rect'): _rect((10, 120, 220, 150)),
        })
        if multiselect:
            field[_name('/Ff')] = NumberObject(FLAG_MULTISELECT)
            field[_name('/V')] = ArrayObject([TextStringObject(v) for v in saved_values])
        else:
            field[_name('/V')] = TextStringObject(saved_values)
        if default_present:
            field[_name('/DV')] = TextStringObject(default)
        ref = self.writer._add_object(field)
        self.top_fields.append(ref)
        return ref

    def duplicate_named_fields(self, name, first_value, second_value, page=1):
        """两个同名顶层字段，制造同名字段冲突。"""
        refs = []
        for value in (first_value, second_value):
            field = DictionaryObject({
                _name('/T'): TextStringObject(name),
                _name('/FT'): _name('/Tx'),
                _name('/V'): TextStringObject(value),
                _name('/Subtype'): _name('/Widget'),
                _name('/P'): self._page_ref(page),
                _name('/Rect'): _rect((10, 10, 200, 40)),
            })
            refs.append(self.writer._add_object(field))
        self.top_fields.extend(refs)
        return refs

    def _attach(self, terminal_ref, name_path):
        if len(name_path) == 1:
            # 终端字典已自带 /T，直接作为顶层字段
            self.top_fields.append(terminal_ref)
            return
        parent = DictionaryObject({_name('/T'): TextStringObject(name_path[0])})
        parent_ref = self.writer._add_object(parent)
        terminal = terminal_ref.get_object()
        terminal[_name('/Parent')] = parent_ref
        parent[_name('/Kids')] = ArrayObject([terminal_ref])
        self.top_fields.append(parent_ref)

    def xfa_only(self):
        """移除 AcroForm 字段，仅保留 XFA 流。"""
        xfa_stream = DecodedStreamObject()
        xfa_stream.set_data(
            b'<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/">'
            b'<template/><xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/">'
            b'</xfa:datasets></xdp:xdp>'
        )
        xfa_ref = self.writer._add_object(xfa_stream)
        form = DictionaryObject({
            _name('/Fields'): ArrayObject(),
            _name('/XFA'): xfa_ref,
        })
        self.writer._root_object[_name('/AcroForm')] = self.writer._add_object(form)

    def no_form(self):
        """彻底不带 AcroForm。"""
        self._no_form = True
        if '/AcroForm' in self.writer._root_object:
            del self.writer._root_object[NameObject('/AcroForm')]

    def build_bytes(self):
        if not self._no_form and '/AcroForm' not in self.writer._root_object:
            form = DictionaryObject({_name('/Fields'): ArrayObject(self.top_fields)})
            self.writer._root_object[_name('/AcroForm')] = self.writer._add_object(form)
        import io
        buffer = io.BytesIO()
        self.writer.write(buffer)
        return buffer.getvalue()
