"""Report exporter: atomic CSV/JSON writes for the lead report."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

CSV_COLUMNS: Sequence[str] = (
    "Channel Name",
    "Channel URL",
    "Extracted Email",
    "MX Status",
    "Timestamp",
)

SUPPORTED_FORMATS = ("csv", "json")


def _atomic_write(path: Path, data: str) -> None:
    """Write *data* to *path* atomically (tmp file in same dir + os.replace)."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp_path = directory / (path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _row_dict(lead: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a lead mapping onto the CSV column set (missing -> '')."""
    return {column: lead.get(column, "") for column in CSV_COLUMNS}


def to_csv(leads: Iterable[Mapping[str, Any]]) -> str:
    """Render *leads* as CSV text using the canonical column order."""
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS))
    writer.writeheader()
    for lead in leads:
        writer.writerow(_row_dict(lead))
    return buffer.getvalue()


def to_json(leads: Iterable[Mapping[str, Any]]) -> str:
    """Render *leads* as pretty-printed JSON text (always an array)."""
    rows = [_row_dict(lead) for lead in leads]
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


def export_report(leads: Iterable[Mapping[str, Any]], out_path: str | os.PathLike) -> Path:
    """Write *leads* to *out_path* atomically; format chosen by extension.

    ``.json`` produces JSON, everything else produces CSV.
    Returns the resolved output path.
    """
    path = Path(out_path)
    suffix = path.suffix.lower().lstrip(".")
    fmt = suffix if suffix in SUPPORTED_FORMATS else "csv"
    payload = to_json(leads) if fmt == "json" else to_csv(leads)
    _atomic_write(path, payload)
    return path.resolve()
