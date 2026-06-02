"""Excel and CSV extraction for the Silver layer.

Uses pandas (with openpyxl for .xlsx). Returns dict-of-lists so JSON
serialization in UploadedReport.extracted_data stays simple.

Date cells in Excel often come back as pandas Timestamps — those are
normalized to YYYY-MM-DD strings here so the review template can
display and edit them consistently.
"""

from typing import Dict, List, Any
import math
import pandas as pd


def _normalize_value(v: Any) -> Any:
    """Convert pandas/NumPy scalars to JSON-safe primitives.

    - NaN/NaT → None
    - Timestamp/datetime → 'YYYY-MM-DD'
    - everything else → str (trimmed) for stable form rendering
    """
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.strftime("%Y-%m-%d")
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (int, float, bool)):
        return v
    return str(v).strip()


def _df_to_rows(df: pd.DataFrame) -> Dict:
    df = df.where(pd.notnull(df), None)
    columns = [str(c).strip() for c in df.columns]
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        row = {}
        for col_original, col_clean in zip(df.columns, columns):
            row[col_clean] = _normalize_value(r[col_original])
        rows.append(row)
    return {"columns": columns, "rows": rows}


def extract_excel(path: str) -> Dict:
    """Read the first sheet of an XLS/XLSX file into normalized rows."""
    try:
        df = pd.read_excel(path, sheet_name=0)
    except Exception as e:
        raise RuntimeError(f"Excel extraction failed: {e}")
    return _df_to_rows(df)


def extract_csv(path: str) -> Dict:
    """Read a CSV file into normalized rows. Tries UTF-8 then latin-1."""
    last_err = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=enc)
            return _df_to_rows(df)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"CSV extraction failed: {last_err}")
