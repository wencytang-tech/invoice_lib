# -*- coding: utf-8 -*-
"""
报销助手 解析核心:从PDF/图片发票中提取结构化数据。

与发票转CSV工具(invoice_processor)相互独立,本文件自带完整解析逻辑:
- 文本行解析 + 坐标切列双引擎,按勾稽校验择优
- 图片走 OCR(需 Tesseract + pytesseract,可选);无OCR时优雅降级
- PDF 页渲染为PNG(供打印材料排版)
- 人民币小写 → 财务大写转换
"""
import os
import re
import unicodedata
from typing import List, Dict, Any, Optional

import pdfplumber

# OCR 库:导入成功后一次性探测引擎是否真的存在
try:
    import pytesseract
    try:
        pytesseract.get_tesseract_version()
        OCR_AVAILABLE = True
    except Exception:
        OCR_AVAILABLE = False
except ImportError:
    pytesseract = None
    OCR_AVAILABLE = False

OCR_HINT = ('未安装OCR引擎(Tesseract),图片无法自动识别金额。'
            '发票仍会排版进打印材料,金额需手工补填。'
            '引擎下载: https://github.com/UB-Mannheim/tesseract/wiki')

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')
PDF_EXTS = ('.pdf',)

# 行尾数值区可接受的单元格:数字(含千分位/负数/百分号)、占位符、文字型税率
_VALUE_TOKEN_RE = re.compile(r'-?[\d,]+(?:\.\d+)?%?')
_VALUE_TEXT_TOKENS = ('***', '---', '－－－', '免税', '不征税')

_UPPER_DIGITS = '零壹贰叁肆伍陆柒捌玖'
_UPPER_UNITS = ('', '拾', '佰', '仟')
_UPPER_GROUPS = ('', '万', '亿')


def number_to_chinese_upper(value) -> str:
    """人民币金额 → 财务大写。如 449.31 → 肆佰肆拾玖圆叁角壹分。

    非法输入返回空串;负数返回空串(报销场景不涉及)。
    """
    try:
        cents = int(round(float(str(value).replace(',', '')) * 100))
    except (TypeError, ValueError):
        return ''
    if cents < 0:
        return ''
    int_part, jiao, fen = cents // 100, (cents % 100) // 10, cents % 10
    if int_part == 0 and jiao == 0 and fen == 0:
        return '零圆整'

    def group4(n: int) -> str:
        """0 <= n < 10000 的四位组转大写(不含组名)。"""
        s = ''
        pending_zero = False
        for idx in range(3, -1, -1):
            d = (n // 10 ** idx) % 10
            if d == 0:
                if s:
                    pending_zero = True
            else:
                if pending_zero:
                    s += '零'
                    pending_zero = False
                s += _UPPER_DIGITS[d] + _UPPER_UNITS[idx]
        return s

    # 整数部分按四位分组(万/亿),高位到低位拼装
    groups = []
    n = int_part
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    int_str = ''
    need_zero = False
    for gi in range(len(groups) - 1, -1, -1):
        g = groups[gi]
        if g == 0:
            need_zero = need_zero or bool(int_str)
            continue
        if int_str and (g < 1000 or need_zero):
            int_str += '零'
        int_str += group4(g) + _UPPER_GROUPS[gi]
        need_zero = False
    if int_str:
        int_str += '圆'

    # 角/分
    tail = ''
    if jiao:
        tail += _UPPER_DIGITS[jiao] + '角'
    if fen:
        if jiao == 0 and int_str:
            tail += '零'
        tail += _UPPER_DIGITS[fen] + '分'
    if fen == 0:
        tail += '整'
    if not int_str:
        # 纯角分:0.5 → 伍角
        return tail
    return int_str + tail


class InvoiceParser:
    """发票解析器:PDF/图片 → 结构化数据 + 勾稽校验。"""

    def __init__(self):
        self.logger = None  # 由宿主按需注入 logging.Logger
        # 数电票字段正则
        self.patterns = {
            'invoice_number': r'发票号码[：:][\s]*([0-9]+)',
            'issue_date': r'开票日期[：:][\s]*([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)',
            'buyer_name': r'购[\s\n]*名称[：:]([^\n销]+?)(?:\s+销|\n)',
            'seller_name': r'销[\s\n]*名称[：:]([^\n]+?)(?:\n|统一社会)',
            'buyer_tax_id': r'购[\s\S]*?统一社会信用代码/纳税人识别号[：:]([0-9A-Za-z]+)',
            'seller_tax_id': None,
            'total_amount': r'[（(]小写[）)]\s*[¥￥]?\s*([0-9]+\.?[0-9]*)',
            'total_amount_text': r'价税合计[（(]大写[）)]\s*([^（(\n]+?)\s*[（(]小写',
            'amount_excl_tax': r'合\s*计\s*[¥￥]([0-9]+\.?[0-9]*)',
            'tax_amount': r'合\s*计\s*[¥￥][0-9.]+\s*[¥￥]([0-9]+\.?[0-9]*)',
            'issuer': r'开票人[：:]([^\n\s]+)',
            'remarks': r'备\s*注\s*\n([^开\n][^\n]*)',
        }

    def _log(self, level, msg, *args):
        if self.logger:
            self.logger.log(level, msg, *args)

    # ---------------- 文本提取 ----------------
    def extract_text_from_pdf(self, pdf_path: str) -> str:
        """提取PDF全文;文本缺失的页尝试OCR(如可用)。"""
        page_texts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if not page_text and OCR_AVAILABLE:
                    try:
                        page_text = pytesseract.image_to_string(
                            page.to_image().original, lang='chi_sim+eng')
                    except Exception as e:
                        self._log(30, 'OCR失败 %s: %s', pdf_path, e)
                page_texts.append(page_text or '')
        # NFKC归一化(康熙部首/全角变体),页间以换行衔接避免跨页拼行
        return '\n'.join(page_texts)

    def extract_text_from_image(self, image_path: str) -> str:
        """图片发票 OCR(需 Tesseract 引擎)。不可用时抛 RuntimeError。"""
        if not OCR_AVAILABLE:
            raise RuntimeError(OCR_HINT)
        from PIL import Image
        img = Image.open(image_path)
        # 手机照片/截图预处理:转灰度、放大到合理宽度提升识别率
        if img.mode not in ('L', 'RGB'):
            img = img.convert('RGB')
        if img.width < 1200:
            ratio = 1200 / max(img.width, 1)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
        text = pytesseract.image_to_string(img, lang='chi_sim+eng')
        return unicodedata.normalize('NFKC', text or '')

    # ---------------- 发票头字段 ----------------
    def parse_invoice_data(self, text: str) -> Dict[str, Any]:
        # NFKC归一化:统一全角数字/康熙部首等变体(如"电⼦"→"电子")
        text = unicodedata.normalize('NFKC', text)
        data = {}
        for key, pattern in self.patterns.items():
            if pattern is None:
                continue
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if m:
                data[key] = re.sub(r'\s+', ' ', m.group(1).strip())

        # 兜底:散落版式的号码/日期(如盖全市税务局章的位置)
        if not data.get('invoice_number'):
            m = re.search(r'国家税务总局\s*(\d{20})', text)
            if m:
                data['invoice_number'] = m.group(1)
            else:
                nums = re.findall(r'\b(\d{20})\b', text)
                if nums:
                    data['invoice_number'] = nums[0]
        if not data.get('issue_date'):
            m = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)', text)
            if m:
                data['issue_date'] = m.group(1)

        # 兜底:机构名启发式
        if not data.get('buyer_name') or not data.get('seller_name'):
            orgs = re.findall(r'([^\s\n]{2,}(?:大学|公司|机构|中心|店|厂|院|所|会))', text)
            exclude = ['税务局', '国家税务', '统一社会', '信用代码', '识别号', '下载']
            uniq = []
            for org in orgs:
                if len(org) >= 3 and org not in uniq \
                        and not any(kw in org for kw in exclude):
                    uniq.append(org)
            if uniq and not data.get('buyer_name'):
                data['buyer_name'] = uniq[0]
            if len(uniq) >= 2 and not data.get('seller_name'):
                data['seller_name'] = uniq[1]

        # 税号:仅排除20位纯数字(新版发票号码),全数字18位税号合法
        tax_ids = re.findall(r'\b([0-9A-Za-z]{15,20})\b', text)
        inv_no = data.get('invoice_number', '')
        tax_ids = [t for t in tax_ids
                   if t != inv_no and not (len(t) == 20 and t.isdigit())]
        seen = set()
        tax_ids = [t for t in tax_ids if not (t in seen or seen.add(t))]
        if not data.get('buyer_tax_id') and tax_ids:
            data['buyer_tax_id'] = tax_ids[0]
        if not data.get('seller_tax_id'):
            rest = [t for t in tax_ids if t != data.get('buyer_tax_id')]
            if rest:
                data['seller_tax_id'] = rest[0]

        # 备注:常规位置(标签之后)
        if not data.get('remarks'):
            m = re.search(r'备\s*注\s*\n?(.+?)(?:\n开票人|开票人[：:]|$)', text, re.DOTALL)
            if m:
                remarks = re.sub(r'\s+', ' ', m.group(1).strip())
                if remarks and not remarks.startswith('开票人'):
                    data['remarks'] = remarks
        # 备注:部分版式内容在标签之前(小写金额行与“备/注”之间)
        if not data.get('remarks'):
            m = re.search(r'[（(]小写[）)][^\n]*\n\s*(\S[^\n]*?)\s*\n\s*备\s*注', text)
            if m:
                data['remarks'] = re.sub(r'\s+', ' ', m.group(1).strip())
        return data

    # ---------------- 明细行:文本行解析 ----------------
    _SPEC_STOP_PREFIXES = ('*', '合', '价税合计', '备', '开票人',
                           '国家税务总局', '统一发票', '下载')

    def _is_spec_continuation(self, line: str) -> bool:
        if not line:
            return False
        if line.startswith(self._SPEC_STOP_PREFIXES):
            return False
        if '¥' in line or '￥' in line:
            return False
        if re.fullmatch(r'[-\d.,%\s]+', line):
            return False
        return len(line) <= 60

    @staticmethod
    def _is_value_token(token: str) -> bool:
        return bool(_VALUE_TOKEN_RE.fullmatch(token)) or token in _VALUE_TEXT_TOKENS

    @staticmethod
    def _clean_value(token: str) -> str:
        if token in ('***', '---', '－－－'):
            return ''
        if _VALUE_TOKEN_RE.fullmatch(token):
            return token.replace(',', '')
        return token

    @staticmethod
    def _to_money(value) -> Optional[float]:
        try:
            return float(str(value).replace(',', ''))
        except (TypeError, ValueError):
            return None

    def parse_line_items_from_text(self, text: str) -> List[Dict[str, Any]]:
        text = unicodedata.normalize('NFKC', text)
        unit_patterns = ['个', '套', '件', '台', '只', '张', '把', '块', '批', '项',
                         '次', '箱', '盒', '包', '瓶', '支', '条', '米', '千克',
                         'kg', 'g', 'm']
        items = []
        lines = text.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('*'):
                parts = line.split()
                if len(parts) >= 5:
                    numeric_count = 0
                    for j in range(len(parts) - 1, -1, -1):
                        if self._is_value_token(parts[j]):
                            numeric_count += 1
                        else:
                            break
                    if numeric_count in (5, 6):
                        text_end = len(parts) - numeric_count
                        text_parts, numeric_parts = \
                            parts[:text_end], parts[text_end:]
                        item_name = text_parts[0] if text_parts else ''
                        remaining = text_parts[1:] if len(text_parts) > 1 else []
                        model, unit = '', ''
                        if remaining:
                            last = remaining[-1]
                            if last in unit_patterns or (
                                    len(last) == 1
                                    and re.match(r'[\u4e00-\u9fff]', last)):
                                unit = last
                                model = ' '.join(remaining[:-1]) \
                                    if len(remaining) > 1 else ''
                            else:
                                model = ' '.join(remaining)
                        values = [self._clean_value(v) for v in numeric_parts]
                        if numeric_count == 6:
                            qty, price, discount, amount, rate, tax = values
                        else:
                            qty, price, amount, rate, tax = values
                            discount = ''
                        item = {'项目名称': item_name, '规格型号': model,
                                '单位': unit, '数量': qty, '单价': price,
                                '减优惠': discount, '金额': amount,
                                '税率/征收率': rate, '税额': tax}
                        j = i + 1
                        while j < len(lines) and self._is_spec_continuation(lines[j].strip()):
                            model += lines[j].strip()
                            j += 1
                        item['规格型号'] = model
                        items.append(item)
                        i = j
                        continue
            i += 1
        return items

    # ---------------- 明细行:坐标切列解析 ----------------
    _LAYOUT_HEADER_SPECS = [
        ('项目名称', (('项目名称',),)),
        ('规格型号', (('规格型号',),)),
        ('单位', (('单位',), ('单', '位'))),
        ('数量', (('数量',), ('数', '量'))),
        ('单价', (('单价',), ('单', '价'))),
        ('减优惠', (('减优惠',), ('减', '优惠'))),
        ('金额', (('金额',), ('金', '额'))),
        ('税率/征收率', (('税率/征收率',), ('税率',))),
        ('税额', (('税额',), ('税', '额'))),
    ]
    _OPTIONAL_LAYOUT_COLUMNS = {'减优惠'}
    _LAYOUT_NUMERIC_COLUMNS = {'数量', '单价', '减优惠', '金额', '税额'}

    def extract_line_items_from_layout(self, pdf_path: str) -> List[Dict[str, Any]]:
        items = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    try:
                        items.extend(self._items_from_page_words(page))
                    except Exception as e:
                        self._log(30, '坐标解析单页失败 %s: %s', pdf_path, e)
        except Exception as e:
            self._log(30, '坐标解析失败 %s: %s', pdf_path, e)
        return items

    def _items_from_page_words(self, page) -> List[Dict[str, Any]]:
        import bisect
        words = page.extract_words()
        if not words:
            return []
        norm = [{'text': unicodedata.normalize('NFKC', w['text']),
                 'x0': w['x0'], 'x1': w['x1'], 'top': w['top']}
                for w in words if unicodedata.normalize('NFKC', w['text'])]
        lines = self._cluster_words_into_lines(norm)
        header_bounds, header_top = None, None
        for line in lines:
            bounds = self._match_header_columns(line['words'])
            if bounds:
                header_bounds, header_top = bounds, line['top']
                break
        if header_bounds is None:
            return []
        n_cols = len(header_bounds)
        groups = []
        for line in lines:
            if line['top'] <= header_top:
                continue
            cells = self._assign_cells(line['words'], header_bounds)
            joined = ''.join(cells)
            if not joined:
                continue
            if (cells[0].startswith(('合', '备', '开票人'))
                    or '价税合计' in joined):
                break
            if cells[0].startswith('*'):
                groups.append(list(cells))
            elif groups and any(t for t in cells[1:]):
                for idx in range(1, n_cols):
                    groups[-1][idx] += cells[idx]
        items = []
        for group in groups:
            item = self._item_from_cells(group, n_cols)
            if item:
                items.append(item)
        return items

    @classmethod
    def _match_header_columns(cls, words) -> Optional[List[float]]:
        texts = [w['text'] for w in words]
        start = next((i for i, t in enumerate(texts)
                      if t.startswith('项目名称')), None)
        if start is None:
            return None
        bounds, i = [], start
        for name, variants in cls._LAYOUT_HEADER_SPECS:
            matched = False
            for tokens in variants:
                if (i + len(tokens) <= len(texts)
                        and all(texts[i + k] == tokens[k]
                                for k in range(len(tokens)))):
                    bounds.append(words[i]['x0'])
                    i += len(tokens)
                    matched = True
                    break
            if not matched:
                if name in cls._OPTIONAL_LAYOUT_COLUMNS:
                    continue
                return None
        return bounds

    @staticmethod
    def _cluster_words_into_lines(words, tol: float = 3.0):
        words = sorted(words, key=lambda w: (w['top'], w['x0']))
        lines = []
        for w in words:
            if lines and w['top'] - lines[-1]['top'] <= tol:
                lines[-1]['words'].append(w)
            else:
                lines.append({'top': w['top'], 'words': [w]})
        for line in lines:
            line['words'].sort(key=lambda w: w['x0'])
        return lines

    @staticmethod
    def _assign_cells(words, bounds: List[float]) -> List[str]:
        import bisect
        n_cols = len(bounds)
        cells = [''] * n_cols
        mids = [(bounds[i] + bounds[i + 1]) / 2.0 for i in range(n_cols - 1)]
        for w in sorted(words, key=lambda w: w['x0']):
            center = (w['x0'] + w['x1']) / 2.0
            k = bisect.bisect_right(mids, center)
            cells[k] += w['text']
        return cells

    def _item_from_cells(self, cells: List[str], n_cols: int) -> Optional[Dict[str, Any]]:
        keys = (['项目名称', '规格型号', '单位', '数量', '单价']
                + (['减优惠'] if n_cols == 9 else [])
                + ['金额', '税率/征收率', '税额'])
        item = {}
        for key, raw in zip(keys, cells):
            value = raw.strip()
            if key in self._LAYOUT_NUMERIC_COLUMNS:
                value = self._clean_value(value)
            item[key] = value
        item.setdefault('减优惠', '')
        if not item.get('项目名称', '').startswith('*'):
            return None
        return item

    # ---------------- 勾稽校验 ----------------
    def validate_invoice(self, invoice_data: Dict[str, Any],
                         line_items: List[Dict[str, Any]]) -> List[str]:
        issues = []
        number = invoice_data.get('invoice_number', '') or ''
        if not re.fullmatch(r'\d{20}|\d{8}', number):
            issues.append('发票号码格式异常')
        if not invoice_data.get('issue_date'):
            issues.append('未识别到开票日期')
        if not invoice_data.get('buyer_name'):
            issues.append('未识别到购买方名称')

        total = self._to_money(invoice_data.get('total_amount'))
        excl = self._to_money(invoice_data.get('amount_excl_tax'))
        tax = self._to_money(invoice_data.get('tax_amount'))
        if None not in (total, excl, tax) and abs(total - excl - tax) > 0.02:
            issues.append('价税合计≠合计金额+合计税额')

        if line_items:
            amounts = [self._to_money(i.get('金额')) for i in line_items]
            if excl is not None and all(a is not None for a in amounts):
                if abs(sum(amounts) - excl) > 0.02 + 0.01 * len(amounts):
                    issues.append(f'明细金额合计{sum(amounts):.2f}≠合计金额{excl:.2f}')
            taxes = [self._to_money(i.get('税额')) for i in line_items]
            if tax is not None and all(t is not None for t in taxes):
                if abs(sum(taxes) - tax) > 0.02 + 0.01 * len(taxes):
                    issues.append(f'明细税额合计{sum(taxes):.2f}≠合计税额{tax:.2f}')
            bad = 0
            for i in line_items:
                qty = self._to_money(i.get('数量'))
                price = self._to_money(i.get('单价'))
                amount = self._to_money(i.get('金额'))
                if None in (qty, price, amount):
                    continue
                discount = self._to_money(i.get('减优惠')) or 0.0
                if abs(qty * price + discount - amount) > 0.02 + abs(qty) * 0.01:
                    bad += 1
            if bad:
                issues.append(f'{bad}行明细“数量×单价(含减优惠)≠金额”')
        return issues

    # ---------------- 统一入口 ----------------
    def analyze_pdf(self, pdf_path: str) -> Dict[str, Any]:
        text = self.extract_text_from_pdf(pdf_path)
        invoice_data = self.parse_invoice_data(text)
        candidates = []
        text_items = self.parse_line_items_from_text(text)
        if text_items:
            candidates.append(text_items)
        layout_items = self.extract_line_items_from_layout(pdf_path)
        if layout_items:
            candidates.append(layout_items)
        if candidates:
            best_items, best_issues = None, None
            for items in candidates:
                issues = self.validate_invoice(invoice_data, items)
                if best_issues is None or len(issues) < len(best_issues):
                    best_items, best_issues = items, issues
        else:
            best_items = []
            best_issues = self.validate_invoice(invoice_data, [])
        return {'invoice_data': invoice_data, 'line_items': best_items,
                'issues': best_issues}

    def analyze_image(self, image_path: str) -> Dict[str, Any]:
        try:
            text = self.extract_text_from_image(image_path)
        except RuntimeError as e:
            return {'invoice_data': {}, 'line_items': [], 'issues': [str(e)]}
        except Exception as e:
            return {'invoice_data': {}, 'line_items': [],
                    'issues': [f'图片读取失败: {e}']}
        invoice_data = self.parse_invoice_data(text)
        line_items = self.parse_line_items_from_text(text)
        issues = self.validate_invoice(invoice_data, line_items)
        return {'invoice_data': invoice_data, 'line_items': line_items,
                'issues': issues}

    def analyze_file(self, path: str) -> Dict[str, Any]:
        ext = os.path.splitext(path)[1].lower()
        if ext in PDF_EXTS:
            return self.analyze_pdf(path)
        if ext in IMAGE_EXTS:
            return self.analyze_image(path)
        raise ValueError(f'不支持的文件类型: {ext}')

    # ---------------- 打印辅助 ----------------
    def render_pdf_pages(self, pdf_path: str, out_dir: str,
                         resolution: int = 150,
                         max_pages: int = 10) -> List[str]:
        """把PDF每页渲染为PNG(供报销材料排版),返回图片路径列表。

        超过 max_pages 的只渲染前 max_pages 页(异常大PDF的保护)。
        """
        os.makedirs(out_dir, exist_ok=True)
        stem = re.sub(r'[^\w\u4e00-\u9fff-]', '_',
                      os.path.splitext(os.path.basename(pdf_path))[0]) or 'pdf'
        outs = []
        with pdfplumber.open(pdf_path) as pdf:
            for idx, page in enumerate(pdf.pages[:max_pages], 1):
                img = page.to_image(resolution=resolution).original
                out = os.path.join(out_dir, f'{stem}_p{idx}.png')
                img.save(out)
                outs.append(out)
        return outs
