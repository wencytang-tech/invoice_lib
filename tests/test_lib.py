# -*- coding: utf-8 -*-
"""invoice_lib 单元测试(离线可跑)。"""
import os
import unittest

from invoice_lib import (InvoiceParser, number_to_chinese_upper,
                         OCR_AVAILABLE)

_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PDF = os.path.join(_DIR, 'fixtures', 'synthetic_invoice.pdf')

NORMAL_TEXT = """电子发票（普通发票）
发票号码：21442000000770468063
开票日期：2025年12月05日
购 名称：汕头大学 销 名称：广州明誉网络科技有限公司
买 售
方 方
信 统一社会信用代码/纳税人识别号：1244000045594645X9 信 统一社会信用代码/纳税人识别号：91440105MABUG3U190
息 息
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
*玩具*益智玩具 个 1 24.3564356435644 24.36 1% 0.24
合 计 ¥24.36 ¥0.24
价税合计（大写） 贰拾肆圆陆角整 （小写）¥24.60
备
注
开票人：某某
某某"""

SCATTERED_TEXT = """电⼦发票（普通发票）
发票号码：
开票日期：
购 销
买 名称： 售 名称：
方 方
信 信 统一社会信用代码/纳税人识别号： 统一社会信用代码/纳税人识别号：
息 息
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
下载次数：1
国
统一发票监
制 21952000000260605490
全 章
国家税务总局 2025年12月04日
深 圳市税务局
汕头大学 深圳市元创时代科技有限公司
1244000045594645X9 914403006911728120
*配电控制设备*排插 ORICO-PDC15 1 31.8211769911504 31.82 13% 4.14
-4A2U2C-PU-1
8-EP
*日用杂品*理线器 ORICO-SCS1-P 1 1.1 1.10 13% 0.14
U-EP
合 计 ¥32.92 ¥4.28
价税合计（大写） 叁拾柒圆贰角整 （小写） ¥ 37.20
251202-076588071201321;拼多多;SZ-拼多多-ORICO旗舰店
备
注
开票人：某某"""

FULLWIDTH_TEXT = """电子发票（普通发票）
发票号码：２１４４２００００００７７０４６８０６３
开票日期：２０２５年１２月０５日
购 名称：汕头大学 销 名称：广州明誉网络科技有限公司
买 售
方 方
信 统一社会信用代码/纳税人识别号：1244000045594645X9 信 统一社会信用代码/纳税人识别号：91440105MABUG3U190
息 息
项目名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额
*玩具*益智玩具 个 1 24.3564356435644 24.36 1% 0.24
合 计 ¥24.36 ¥0.24
价税合计（大写） 贰拾肆圆陆角整 （小写）¥24.60
备
注
开票人：某某
某某"""


class UpperAmountTest(unittest.TestCase):
    def test_standard_cases(self):
        cases = {
            '24.60': '贰拾肆圆陆角整',
            '5.58': '伍圆伍角捌分',
            '449.31': '肆佰肆拾玖圆叁角壹分',
            '32.30': '叁拾贰圆叁角整',
            '0.05': '伍分',
            '0': '零圆整',
            '10': '壹拾圆整',
            '100000': '壹拾万圆整',
            '1.05': '壹圆零伍分',
            '20,000.00': '贰万圆整',
            '100000001.00': '壹亿零壹圆整',
        }
        for value, expect in cases.items():
            self.assertEqual(number_to_chinese_upper(value), expect,
                             f'{value} 应为 {expect}')

    def test_invalid(self):
        self.assertEqual(number_to_chinese_upper('abc'), '')
        self.assertEqual(number_to_chinese_upper(None), '')
        self.assertEqual(number_to_chinese_upper('-5'), '')


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.p = InvoiceParser()

    def test_normal_fields(self):
        d = self.p.parse_invoice_data(NORMAL_TEXT)
        self.assertEqual(d['invoice_number'], '21442000000770468063')
        self.assertEqual(d['buyer_name'], '汕头大学')
        self.assertEqual(d['seller_tax_id'], '91440105MABUG3U190')
        self.assertEqual(d['total_amount'], '24.60')

    def test_scattered_layout(self):
        d = self.p.parse_invoice_data(SCATTERED_TEXT)
        self.assertEqual(d['invoice_number'], '21952000000260605490')
        self.assertEqual(d['seller_tax_id'], '914403006911728120')
        self.assertEqual(
            d['remarks'], '251202-076588071201321;拼多多;SZ-拼多多-ORICO旗舰店')
        items = self.p.parse_line_items_from_text(SCATTERED_TEXT)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['规格型号'], 'ORICO-PDC15-4A2U2C-PU-18-EP')

    def test_fullwidth_normalized(self):
        d = self.p.parse_invoice_data(FULLWIDTH_TEXT)
        self.assertEqual(d['invoice_number'], '21442000000770468063')
        self.assertEqual(d['issue_date'], '2025年12月05日')

    def test_validation(self):
        d = self.p.parse_invoice_data(NORMAL_TEXT)
        items = self.p.parse_line_items_from_text(NORMAL_TEXT)
        self.assertEqual(self.p.validate_invoice(d, items), [])
        d2 = dict(d)
        d2['total_amount'] = '99.99'
        self.assertTrue(any('价税合计' in i for i in
                            self.p.validate_invoice(d2, items)))

    def test_analyze_pdf_synthetic(self):
        """合成ASCII票样:管线可运行,字段为空并标记号码异常(无真实数据)。"""
        if not os.path.exists(FIXTURE_PDF):
            self.skipTest('缺合成样例PDF')
        result = self.p.analyze_pdf(FIXTURE_PDF)
        # 假20位号可被提取,买方/销方缺失触发issues
        self.assertEqual(result['invoice_data'].get('invoice_number', ''),
                         '00000000000000000001')
        self.assertEqual(result['line_items'], [])
        self.assertTrue(any('购买方' in i for i in result['issues']))

    def test_render_pdf_pages_capped(self):
        if not os.path.exists(FIXTURE_PDF):
            self.skipTest('缺样例PDF')
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pages = self.p.render_pdf_pages(FIXTURE_PDF, td, max_pages=10)
            self.assertEqual(len(pages), 1)


if __name__ == '__main__':
    unittest.main()
