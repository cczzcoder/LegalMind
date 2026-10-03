"""第 1 层原生解析器：纯 CPU、零外发、许可宽松（设计 §2、§17.2）。

- PDF：``pdf.py`` 双后端（pypdfium2 / pdfplumber），架构兼容互换
- DOCX：``docx.py``（python-docx）
- HTML：``html.py``（lxml）
- 纯文本：``text.py``

第 2 层 OCR 与第 3 层 Docling 均**未**在本层实现；Docling 明确暂缓（见 ``registry``）。
"""
