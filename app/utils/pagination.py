"""Shared list pagination for RisKonek admin/portal tables.

Lists across the app build a filtered result set in Python (mirroring the
audit trail, which already paginates an in-memory list). This helper keeps
that convention: paginate the already-filtered Python list, offer the
standard page-size choices (10 / 25 / 50 / 100) plus a "View All" option,
and expose everything the shared `shared/_pagination.html` macro needs.

Query params are read as Optional[str] and coerced safely here (never
parsed as int in the route signature) — consistent with the live-filter
convention documented in CLAUDE.md.
"""
from urllib.parse import urlencode

DEFAULT_PER_PAGE = 25
PAGE_SIZE_CHOICES = [10, 25, 50, 100]   # "all" is offered in addition to these


def parse_per_page(raw, default: int = DEFAULT_PER_PAGE):
    """Coerce a per_page query value to a valid choice.

    Returns an int from PAGE_SIZE_CHOICES, the string "all", or `default`
    for blank/garbage input. Never raises.
    """
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s == "all":
        return "all"
    if s.isdigit() and int(s) in PAGE_SIZE_CHOICES:
        return int(s)
    return default


def parse_page(raw, default: int = 1):
    """Coerce a page query value to a positive int; 1 for blank/garbage."""
    if raw is None:
        return default
    s = str(raw).strip()
    if s.isdigit() and int(s) >= 1:
        return int(s)
    return default


class Page:
    """A single page of results plus the metadata the pager macro renders."""

    def __init__(self, items, page, per_page, total):
        self.items = items
        self.per_page = per_page            # int or "all"
        self.total = total
        self.choices = PAGE_SIZE_CHOICES
        if per_page == "all":
            self.page = 1
            self.total_pages = 1
            self.size_value = "all"
            self.start_index = 1 if total else 0
            self.end_index = total
        else:
            self.total_pages = max(1, (total + per_page - 1) // per_page)
            self.page = max(1, min(page, self.total_pages))
            self.size_value = str(per_page)
            self.start_index = (self.page - 1) * per_page + 1 if total else 0
            self.end_index = min(self.page * per_page, total)

    @property
    def has_prev(self):
        return self.per_page != "all" and self.page > 1

    @property
    def has_next(self):
        return self.per_page != "all" and self.page < self.total_pages


def paginate(rows, page, per_page):
    """Slice an already-filtered Python list into a Page.

    `page` and `per_page` should come from parse_page / parse_per_page.
    """
    total = len(rows)
    if per_page == "all":
        return Page(rows, 1, "all", total)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return Page(rows[start:start + per_page], page, per_page, total)


def build_base_query(params: dict) -> str:
    """URL-encode active filter params, dropping blanks and the page /
    per_page keys (those are appended by the pager macro itself)."""
    clean = {
        k: v for k, v in params.items()
        if v not in ("", None) and k not in ("page", "per_page")
    }
    return urlencode(clean)
