# invoice_lib

中国电子发票解析核心库(纯代码,**不含任何票样数据**)。

由 `reimbursement_helper` 与 `invoice_processor` 两个项目共享,消除复制分叉。

## 能力

- 双引擎明细解析:文本行匹配 + 坐标切列,按勾稽校验择优
- 勾稽校验:号码格式、价税合计自洽、明细汇总、数量×单价
- 图片 OCR 路径(可选 Tesseract)+ GLM 外部识别由宿主实现
- PDF 页渲染为 PNG(打印排版用,上限10页)
- 人民币小写 → 财务大写换算

## 安装与测试

```bash
pip install -e .[ocr]   # ocr为可选
python tests/test_lib.py
```
