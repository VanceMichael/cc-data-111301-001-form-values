# 调查 PDF 表单的字段身份与填写值资料工具

这个 Python 项目用于业务来源资料的检查、原始字节归档与批次收件。`form_values/core.py`
处理单份资料的基础预览，`extract.py` 采集逻辑字段、显示位置、实际填写值与默认值，
`intake.py` 在内容寻址归档之上提供批次接收、待确认区与修订确认，`pdfbuild.py` 构造虚构
问卷用于测试与演示，`archive.py` 保存原文件，`fixtures/` 保存本地业务演练资料，
`tests/` 检查已有行为。工具只呈现来源事实与疑点，不替使用者猜测答案。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 单份资料

```sh
python3 -m form_values inspect fixtures/survey-r1.pdf
python3 -m form_values capture fixtures/survey-r1.pdf --archive .work/raw
```

归档返回 SHA-256、字节数及原文件名；文件名不参与内容身份。输入不可识别时命令失败，
不产生空的成功结果。

## 批次收件

```sh
# 接收单份；首次提交直接采用，同身份的修订自动进入待确认区
python3 -m form_values receive fixtures/survey-r1.pdf --store .work/intake
python3 -m form_values receive fixtures/survey-r2.pdf --store .work/intake

# 批量接收目录中的全部 PDF；单份争议或损坏不阻塞其余文件
python3 -m form_values batch inbox/ --store .work/intake

# 查看待确认区及与当前采用版本的逐字段差异
python3 -m form_values pending --store .work/intake

# 行政核对原文件、字段与页面后确认或拒绝
python3 -m form_values confirm sub-000002 --note "已与经销商核对" --store .work/intake
python3 -m form_values reject sub-000003 --reason "无法核实身份" --store .work/intake

# 查询：汇总结论、问卷版本视图、单个答案的逐版本溯源（含页码与归档摘要）
python3 -m form_values overview --store .work/intake
python3 -m form_values questionnaire survey-A --store .work/intake
python3 -m form_values answer survey-A contact.name --store .work/intake
```

收件库布局：`raw/` 为按 SHA-256 内容寻址的原始字节（重复交付只存一份），`state.json`
保存全部提交、问卷版本、只追加的事件流水。确认/拒绝不删除、不覆盖已接收证据。

采集与路由规则、疑点代码见 [`docs/data.md`](docs/data.md)。要点：空字符串是已提交值，
与没有 `/V` 键严格区分；单选/多选保留原始导出值，显示文字另列；外观与实际值不符、
同名字段冲突、仅 XFA 无可读字段等情形只登记疑点，不自动猜测。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q form_values tests
```

## 本地演示

```sh
python3 -m form_values demo
```

演示用虚构问卷走完整流程：两位经销商各交一份同名文件、同一经销商补交修订并进入待确认、
相同文件重复交付、只有勾选外观的疑点件、仅 XFA 文件被拒绝，最后确认修订并输出答案溯源、
受访者汇总与事件流水。使用临时目录并自动清理，所有示例身份与业务数值均为虚构，不需要
外部账号。
