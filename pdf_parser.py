"""Extract plain text from an uploaded syllabus PDF."""

import io
from pypdf import PdfReader


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Given the raw bytes of a PDF file, return its concatenated text.

    Works with Streamlit's UploadedFile (call .getvalue() / .read() first)
    or any bytes-like object.
    """
    reader = PdfReader(io.BytesIO(file_bytes))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages_text.append(text)
    full_text = "\n\n".join(pages_text).strip()

    if not full_text:
        raise ValueError(
            "No extractable text found in this PDF. It may be a scanned "
            "image without an OCR text layer — try a text-based syllabus PDF."
        )
    return full_text
