"""Create a reproducible catalog for the quiz's acceptance scenario.

Run from the repository root, with the package installed (``uv pip install -e
'.[dev]'``)::

    python scripts/generate_demo_data.py

The script uses ``KEEPSTACK_DATA_DIR`` (default ``./data``) and refuses to run
twice against the same catalog, so it never silently duplicates assets.

It creates 14 synthetic images, two collections, and a deliberate mix of tags:
six assets match

    keyword "harbor" + collection "Harbor Campaign (quiz)"
    + tags approved AND campaign - tag internal

Two of the six share an identical created_at so stable pagination can be
checked by eye. Asset 07 carries Chinese text plus commas, quotes and
newlines to exercise CSV quoting.
"""
from __future__ import annotations

import io
import random
import uuid as uuidlib
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

from keepstack import ingest
from keepstack.audit import now_iso
from keepstack.db import get_conn, init_db

COLLECTION_NAME = "Harbor Campaign (quiz)"
OTHER_COLLECTION_NAME = "Elsewhere (quiz)"
BASE_TIME = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)

# title, tags, collection slot, created-at offset (hours), status
CASES = [
    ("Harbor at dawn", ["approved", "campaign"], "harbor", 0, "active"),
    ("Harbor at dusk", ["approved", "campaign"], "harbor", 1, "active"),
    ("Harbor pilot chart", ["approved", "campaign"], "harbor", 2, "active"),
    ("Harbor internal memo", ["approved", "campaign", "internal"], "harbor", 3, "active"),
    ("Harbor approved only", ["approved"], "harbor", 4, "active"),
    ("Harbor campaign only", ["campaign"], "harbor", 5, "active"),
    ('海港 Harbor, "晨光"\n第二行', ["approved", "campaign"], "harbor", 6, "active"),
    ("Harbor twin A", ["approved", "campaign"], "harbor", 6, "active"),
    ("Harbor twin B", ["approved", "campaign"], "harbor", 6, "active"),
    ("Harbor archived brief", ["approved", "campaign", "internal"], "harbor", 7, "active"),
    ("Harbor untagged survey", [], "harbor", 8, "active"),
    ("Harbor retired notice", ["approved", "campaign"], "harbor", 9, "deleted"),
    ("Harbor outside the collection", ["approved", "campaign"], "other", 10, "active"),
    ("Harbor loose plate", ["approved", "campaign"], None, 11, "active"),
]

PALETTES = [
    ((94, 132, 247), (24, 28, 38)),
    ((70, 192, 138), (18, 30, 26)),
    ((242, 96, 122), (34, 18, 24)),
    ((220, 160, 60), (32, 26, 14)),
    ((150, 110, 220), (24, 18, 34)),
    ((60, 180, 200), (14, 28, 30)),
]


def _image_bytes(index: int, label: str) -> bytes:
    random.seed(4100 + index)
    fg, bg = PALETTES[index % len(PALETTES)]
    width, height = 900, 640
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    for _ in range(7):
        x0 = random.randint(-120, width)
        y0 = random.randint(-120, height)
        radius = random.randint(140, 420)
        shade = tuple(min(255, c + random.randint(-25, 35)) for c in fg)
        draw.ellipse([x0, y0, x0 + radius, y0 + radius], fill=shade)
    draw.rectangle([0, height - 130, width, height], fill=(0, 0, 0))
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    draw.text((width / 2, height - 65), label, font=font, fill=(255, 255, 255), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _add_tags(asset_id: int, names: list[str]) -> None:
    conn = get_conn()
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
            (asset_id, tag_id),
        )
    conn.commit()
    ingest.reindex_fts(asset_id)


def _create_collection(name: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO collections (uuid, name, description, kind, created_at) "
        "VALUES (?, ?, ?, 'folder', ?)",
        (str(uuidlib.uuid4()), name, "Generated quiz fixture", now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def run() -> None:
    init_db()
    conn = get_conn()
    if conn.execute("SELECT 1 FROM collections WHERE name = ?", (COLLECTION_NAME,)).fetchone():
        print(f"'{COLLECTION_NAME}' already exists in {conn.execute('PRAGMA database_list').fetchone()['file']}.")
        print("Point KEEPSTACK_DATA_DIR at a fresh directory to generate another fixture.")
        return

    harbor_collection = _create_collection(COLLECTION_NAME)
    other_collection = _create_collection(OTHER_COLLECTION_NAME)
    slots = {"harbor": harbor_collection, "other": other_collection, None: None}

    created = []
    for index, (title, tags, slot, hour, status) in enumerate(CASES):
        label = f"Harbor {index + 1:02d}"
        filename = f"harbor_{index + 1:02d}.jpg"
        asset = ingest.ingest_stream(
            io.BytesIO(_image_bytes(index, label)), filename, run_ai=False, title=title
        )
        _add_tags(asset["id"], tags)
        created_at = (BASE_TIME + timedelta(hours=hour)).isoformat()
        conn.execute(
            "UPDATE assets SET created_at = ?, updated_at = ?, status = ? WHERE id = ?",
            (created_at, created_at, status, asset["id"]),
        )
        collection_id = slots[slot]
        if collection_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) "
                "VALUES (?, ?, ?)",
                (collection_id, asset["id"], created_at),
            )
        if index == 6:
            # The Chinese asset also demonstrates CSV escaping of commas,
            # quotes and newlines.
            conn.execute(
                "UPDATE assets SET description = ? WHERE id = ?",
                ('描述, 含"引号"\n以及换行', asset["id"]),
            )
        conn.commit()
        if index == 6:
            ingest.reindex_fts(asset["id"])
        created.append(asset["id"])

    print(f"Created {len(created)} assets and 2 collections.")
    print("")
    print("Acceptance scenario:")
    print(f"  keyword: harbor")
    print(f"  collection: {COLLECTION_NAME}")
    print("  must have: approved, campaign")
    print("  must exclude: internal")
    print("  expected: 6 matches (01, 02, 03, 07, 08, 09), of which 07/08/09 share created_at")
    print("")
    print("Start the app with:  python -m keepstack")
    print("Then sign in as admin / admin.")


if __name__ == "__main__":
    run()
