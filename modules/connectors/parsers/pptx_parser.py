# modules/connectors/parsers/pptx_parser.py
import io
import re
import zipfile
import logging
import xml.etree.ElementTree as ET
from modules.connectors.parsers.base import BaseParser

logger = logging.getLogger("pptx_parser")

class PPTXParser(BaseParser):
    """
    Zero-dependency Microsoft PowerPoint (.pptx) parser.
    Extracts text content slide-by-slide from the OpenXML package
    using Python's standard library zipfile and xml.etree.
    """
    def parse(self, content_bytes: bytes) -> str:
        if not content_bytes:
            return ""

        slides_text = []
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
                # Find all slide xml files and sort them naturally (slide1.xml, slide2.xml, ...)
                slide_files = [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
                
                def extract_slide_num(filename: str) -> int:
                    match = re.search(r"slide(\d+)\.xml", filename)
                    return int(match.group(1)) if match else 0

                slide_files.sort(key=extract_slide_num)

                for idx, slide_path in enumerate(slide_files, start=1):
                    try:
                        slide_xml = z.read(slide_path)
                        root = ET.fromstring(slide_xml)

                        # DrawingML text elements end with tag '}t' (e.g. {http://schemas.openxmlformats.org/drawingml/2006/main}t)
                        tokens = []
                        for elem in root.iter():
                            if elem.tag.endswith("}t") and elem.text:
                                text_val = elem.text.strip()
                                if text_val:
                                    tokens.append(text_val)

                        if tokens:
                            slide_content = " ".join(tokens)
                            slides_text.append(f"### [Slide {idx}]\n{slide_content}")
                    except Exception as slide_err:
                        logger.warning(f"Error parsing slide '{slide_path}': {slide_err}")
                        continue

            if not slides_text:
                return ""

            return "\n\n".join(slides_text)
        except Exception as e:
            logger.error(f"Failed to parse PPTX content: {e}")
            return ""
