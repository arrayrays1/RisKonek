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

def extract_pdf_content(file_path):
    import pdfplumber
    extracted_content = []

    with pdfplumber.open(file_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):

            # Extract normal text
            text = page.extract_text(layout=True) or ""

            # Extract tables
            tables = page.extract_tables()

            extracted_content.append({
                "page": page_number,
                "text": text,
                "tables": tables
            })

    return extracted_content

def format_tables(tables):
    formatted_tables = []

    for table_index, table in enumerate(tables, start=1):

        if not table:
            continue

        rows = []

        for row in table:
            # Remove None values and normalize whitespace
            cleaned_row = [
                str(cell).strip().replace("\n", " ")
                if cell else ""
                for cell in row
            ]

            if any(cleaned_row):
                rows.append(cleaned_row)

        if not rows:
            continue

        formatted_tables.append({
            "table_number": table_index,
            "content": rows
        })

    return formatted_tables

def prepare_pdf_text(file_path):
    pages = extract_pdf_content(file_path)

    document_text = []

    for page in pages:

        document_text.append(
            f"\n========== PAGE {page['page']} ==========\n"
        )

        # Normal text
        if page["text"]:
            document_text.append(
                "\n--- PAGE TEXT ---\n"
            )
            document_text.append(page["text"])

        # Tables
        if page["tables"]:
            document_text.append(
                "\n--- PAGE TABLES ---\n"
            )

            formatted_tables = format_tables(page["tables"])

            for table in formatted_tables:

                document_text.append(
                    f"\nTABLE {table['table_number']}\n"
                )

                for row in table["content"]:
                    document_text.append(" | ".join(row))

    return {"text": "\n".join(document_text)}
