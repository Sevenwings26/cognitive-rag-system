# modules/connectors/parsers/factory.py
import os
from modules.connectors.parsers.base import BaseParser
from modules.connectors.parsers.pdf_parser import PDFParser
from modules.connectors.parsers.docx_parser import DocxParser
from modules.connectors.parsers.text_parser import TextParser
from modules.connectors.parsers.tabular_parser import TabularParser
from modules.connectors.parsers.pptx_parser import PPTXParser

class ParserFactory:
    _parsers = {
        ".pdf": PDFParser(),
        ".docx": DocxParser(),
        ".doc": DocxParser(),
        ".pptx": PPTXParser(),
        ".ppt": PPTXParser(),
        ".txt": TextParser(),
        ".md": TextParser(),
        ".json": TextParser(),
        ".xlsx": TabularParser(),
        ".xls": TabularParser(),
        ".csv": TabularParser(),
        ".tsv": TabularParser(),
    }

    @classmethod
    def get_parser(cls, filename: str, mime_type: str = None) -> BaseParser:
        ext = os.path.splitext(filename)[1].lower()
        parser = cls._parsers.get(ext)
        if not parser:
            if mime_type and ("spreadsheet" in mime_type or "excel" in mime_type or "csv" in mime_type):
                return TabularParser()
            return TextParser()
        return parser
