"""PDF text extraction for the Silver layer.

Primary: pdfplumber (good for text PDFs with tables).
Fallback: PyMuPDF (fitz) — used if pdfplumber returns nothing or raises.

OCR for scanned PDFs is intentionally not implemented yet (Week 6 scope).
"""

from typing import Dict


def extract_pdf(path: str) -> Dict:
    """Return {"text": "<all pages joined>"} or raise on hard failure.

    Tries pdfplumber first; falls back to PyMuPDF if pdfplumber produces
    no readable text (e.g. a scanned PDF still returns nothing — that's
    surfaced to the Admin as "extraction returned no text").
    """
    text = ""

    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = []
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(page_text)
            text = "\n\n".join(pages).strip()
    except Exception:
        text = ""

    if not text:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            pages = []
            for page in doc:
                page_text = page.get_text("text") or ""
                if page_text.strip():
                    pages.append(page_text)
            doc.close()
            text = "\n\n".join(pages).strip()
        except Exception as e:
            raise RuntimeError(f"PDF extraction failed: {e}")

    return {"text": text}
