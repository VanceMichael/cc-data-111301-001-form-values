# 来源资料约定

## 资料形态

来源为 PDF AcroForm，不做 OCR，只读取结构中真正保存的字段值、默认值与 widget 外观状态。
survey-r1 与 survey-r2 的 QuestionnaireID 相同，Revision 不同。contact.name 由父字段和子
字段组成，同一字段的两个 widget 分别出现在两页。consent 的值与外观状态有意不同，文件保留
了这种来源事实。这里不使用扫描文字作为字段数据。

## 四类信息各自保留

同一份填写结果拆成四个独立维度，任何一层都不用另一层替代：

1. 逻辑字段（`fields`）：按 AcroForm 字段树解析出的限定名（如 `contact.name`）、字段类型
   （`/Tx`、`/Btn`、`/Ch`）、标志位、实际填写值 `/V`、默认值 `/DV`、选择项 `/Opt`。
   父节点上的名称、`/FT`、`/Ff`、`/V`、`/DV`、`/Opt` 按 PDF 继承规则向子节点解析。
2. 显示位置（`widgets`）：每个 widget 的对象编号、所在页码、`/Rect`、外观状态 `/AS`、
   外观字典 `/AP /N` 允许的状态名、widget 自带的 `/V`。同一逻辑字段可以在多页各有一个
   widget，分别记录。
3. 实际填写值：`value_present` 表示字段树上是否真正存在 `/V` 键；`value` 是解码后的值。
   空字符串 `''` 是已提交的值（`value_present=true, value=''`），与没有 `/V` 键
   （`value_present=false, value=null`）严格区分，空字符串不会被自动解释成未提交。
4. 默认值：`default_present` 与 `default_value` 单独保存，不与填写值混用。

## 导出值与显示文字

- 单选组（`/Btn` + Radio 标志）保存的是外观状态名形式的导出值（如 `/Red`、`/Agree`），
  不一定是 `Yes`；提取结果保留原始 token，不做翻译。widget 的 `/AP /N` 中除 `/Off` 外的
  状态名列入 `on_states`。
- 多选/选择字段（`/Ch` + Multiselect 标志）的 `/Opt` 同时保留 `export_value` 与
  `display_text`；`/V` 中保存的是导出值数组，显示文字只作对照，不替换导出值。
- 名称对象（NameObject）保留 `/` 前缀原文，文本对象按字符串解码。

## 疑点代码（只记录，不猜答案）

| 代码 | 含义 |
| --- | --- |
| `appearance_value_mismatch` | widget 外观状态与字段实际保存值不一致（两个方向都报：外观勾选但保存值是 `/Off` 语义，或外观 `/Off` 但保存了激活值；单选组还会做组级整体比对） |
| `appearance_only_change` | widget 只有勾选外观，字段上没有任何 `/V`，无法确认实际填写值 |
| `same_name_conflict` | 同一限定名对应多个字段对象，各自的值都保留并标记冲突 |
| `widget_value_conflict` | widget 自带 `/V` 与逻辑字段 `/V` 不一致，或同字段多个 widget 的激活外观互相冲突 |
| `unknown_appearance_state` | widget 的 `/AS` 不在其外观字典允许的状态内 |
| `xfa_only` | 文件只有 XFA 流，没有可读取的 AcroForm 字段 |
| `no_form` | 文件没有 AcroForm |

疑点随提交原样保留，管理员看到的是事实而不是软件推断的答案。

## 收件身份与批次路由

- 问卷身份取元数据 `QuestionnaireID`，文件名不参与身份；同文件名可以是不同受访者。
- 首次收到某身份：接收为采用版本（`adopted`）。
- 同一字节再次交付：内容寻址归档只存一份，登记为 `duplicate`，不增加受访者。
- 字节不同但填写值指纹相同：登记为 `same_values_redelivery` 副本，不进待确认区。
- 同身份出现不同填写值：进入待确认区（`pending`，原因 `value_revision`），批次内其余
  文件继续处理，争议件不阻塞整批。
- 无法解析、无身份、仅 XFA/无字段：进入待确认区并注明 `unreadable_pdf`、
  `missing_questionnaire_identity` 或 `no_readable_fields`，不会产生空的成功结果。
- 受访者数量 = 拥有采用版本的不同 QuestionnaireID 数量。

## 修订确认与证据保全

- `confirm` 后新提交成为采用版本，原采用版本转为 `superseded`；`reject` 后争议件转为
  `rejected`，原采用版本不变。两种操作都写入只追加的事件流水。
- 原始字节按 SHA-256 内容寻址归档于收件库 `raw/`，确认/拒绝不删除、不覆盖任何已接收
  证据；`state.json` 保存全部提交的字段、widget、疑点与归档摘要。
- 答案溯源（`answer`）按限定名列出每个版本的值、默认值、widget 页码与位置、对应归档
  摘要，并标明当前采用的是哪个版本；确认前后的查询都能说明采用版本。
