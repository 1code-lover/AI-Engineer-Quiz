"""CSV export for catalog metadata.

Only the information list is exported -- never the asset bytes. The format is
RFC 4180 CSV (UTF-8 with a BOM so spreadsheet tools open Chinese correctly),
with fields quoted by the standard library whenever they contain commas,
quotes or newlines. Multiple tags are joined with ``|`` inside one cell.

Cells that a spreadsheet could interpret as a formula are prefixed with a
single quote. That is a defensive measure: uploaded titles/filenames are
untrusted input, and a CSV opened in Excel must not execute them.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, Sequence

COLUMNS: tuple[str, ...] = (
    "uuid",
    "title",
    "description",
    "filename",
    "media_type",
    "tags",
    "created_at",
)
TAG_SEPARATOR = "|"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def rows_to_csv(
    rows: Iterable[dict],
    columns: Sequence[str] = COLUMNS,
    tag_separator: str = TAG_SEPARATOR,
) -> str:
    """Serialise export rows (``{"tags": [..], ...}``) to a CSV document."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for row in rows:
        values = []
        for column in columns:
            if column == "tags":
                text = tag_separator.join(row.get("tags") or [])
            else:
                text = row.get(column)
            values.append(_safe_cell(text))
        writer.writerow(values)
    # BOM + UTF-8 is what Excel expects for non-ASCII text. Readers should use
    # utf-8-sig; the BOM is otherwise invisible in the first header cell.
    return "\ufeff" + buf.getvalue()
