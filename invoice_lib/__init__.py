"""invoice_lib:中国电子发票解析核心(纯代码,不含任何票样数据)。"""
from .core import (InvoiceParser, number_to_chinese_upper,
                   OCR_AVAILABLE, OCR_HINT, IMAGE_EXTS, PDF_EXTS)

__version__ = "0.1.0"
__all__ = ['InvoiceParser', 'number_to_chinese_upper', 'OCR_AVAILABLE',
           'OCR_HINT', 'IMAGE_EXTS', 'PDF_EXTS', '__version__']
