"""Acceptance tests for combined filters, stable pagination and CSV export.

The catalog is built with explicit timestamps (including a tie) so the tests
exercise the behaviours from the quiz brief rather than incidental data.
"""
import csv
import io
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from keepstack import export, ingest, search
from keepstack.app import app
from keepstack.auth import reset_login_throttle
from keepstack.db import get_conn, init_db

KW = "harborquiz"
APPROVED = "quiz-approved"
CAMPAIGN = "quiz-campaign"
INTERNAL = "quiz-internal"
ARCHIVE = "quiz-archive"


def setup_module(_):
    init_db()


def _png(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new(
        "RGB",
        (40 + seed, 30 + seed),
        ((seed * 37) % 256, (seed * 71) % 256, (seed * 113) % 256),
    ).save(buf, format="PNG")
    return buf.getvalue()


def _add_tags(asset_id: int, names) -> None:
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
    ingest.reindex_embedding(asset_id)


def _set_columns(asset_id: int, **fields) -> None:
    conn = get_conn()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE assets SET {assignments} WHERE id = ?",
        [*fields.values(), asset_id],
    )
    conn.commit()
    ingest.reindex_fts(asset_id)


def _create_collection(name: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO collections (uuid, name, kind, created_at) VALUES (?, ?, 'folder', ?)",
        (str(uuidlib.uuid4()), name, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return cur.lastrowid


def _create_asset(
    title: str,
    *,
    tags=(),
    collection_id=None,
    created_at=None,
    description=None,
    seed=0,
    filename=None,
    data=None,
):
    raw = data if data is not None else _png(seed)
    asset = ingest.ingest_stream(
        io.BytesIO(raw), filename or f"{KW}_{seed}.png", title=title, run_ai=False
    )
    if description is not None:
        _set_columns(asset["id"], description=description)
    if tags:
        _add_tags(asset["id"], tags)
    conn = get_conn()
    if collection_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) "
            "VALUES (?, ?, ?)",
            (collection_id, asset["id"], created_at),
        )
    if created_at is not None:
        conn.execute(
            "UPDATE assets SET created_at = ?, updated_at = ? WHERE id = ?",
            (created_at, created_at, asset["id"]),
        )
    conn.commit()
    return asset["id"], asset["uuid"]


@pytest.fixture(scope="module")
def catalog():
    collection = _create_collection(f"{KW} collection")
    other_collection = _create_collection(f"{KW} other collection")
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

    def ts(hours: int) -> str:
        return (base + timedelta(hours=hours)).isoformat()

    assets = {}
    specs = {
        # key: (tags, collection, created hour, description)
        "a1": ([APPROVED, CAMPAIGN], collection, 0, "first match"),
        "a2": ([APPROVED, CAMPAIGN, ARCHIVE], collection, 1, None),
        "a3": ([APPROVED, CAMPAIGN], collection, 2, None),
        "a4": ([APPROVED, CAMPAIGN, INTERNAL], collection, 3, None),
        "a5": ([APPROVED], collection, 4, None),
        "a6": ([CAMPAIGN], collection, 5, None),
        "a7": ([APPROVED, CAMPAIGN], collection, 6, None),
        "a8": ([APPROVED, CAMPAIGN], collection, 6, None),  # same timestamp as a7
        "a9": ([APPROVED, CAMPAIGN], other_collection, 7, None),
        "archived": ([APPROVED, CAMPAIGN], collection, 8, None),
    }
    for seed, (key, (tags, coll, hour, description)) in enumerate(specs.items(), start=1):
        asset_id, asset_uuid = _create_asset(
            f"{KW} {key}",
            tags=tags,
            collection_id=coll,
            created_at=ts(hour),
            description=description,
            seed=seed,
        )
        assets[key] = {"id": asset_id, "uuid": asset_uuid}
    conn = get_conn()
    conn.execute("UPDATE assets SET status = 'deleted' WHERE id = ?", (assets["archived"]["id"],))
    conn.commit()
    return SimpleNamespace(collection=collection, other=other_collection, **assets)


@pytest.fixture(scope="module")
def client():
    reset_login_throttle()
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin"}
        )
        assert response.status_code == 200, response.text
        test_client.headers.update({"Authorization": "Bearer " + response.json()["token"]})
        yield test_client


def _filtered(**overrides):
    params = dict(
        q=KW,
        collection_id=None,
        tags=[APPROVED, CAMPAIGN],
        exclude_tags=[INTERNAL],
        sort="oldest",
        limit=100,
        offset=0,
    )
    params.update(overrides)
    return params


def test_combined_filters_intersect(catalog):
    result = search.search(**_filtered(collection_id=catalog.collection))

    expected = [catalog.a1["uuid"], catalog.a2["uuid"], catalog.a3["uuid"],
                catalog.a7["uuid"], catalog.a8["uuid"]]
    assert result["total"] == 5
    assert [item["uuid"] for item in result["items"]] == expected


def test_collection_filter_excludes_assets_outside_it(catalog):
    in_collection = search.search(**_filtered(collection_id=catalog.collection))
    without_collection = search.search(**_filtered(collection_id=None))

    assert without_collection["total"] == in_collection["total"] + 1
    assert catalog.a9["uuid"] in {item["uuid"] for item in without_collection["items"]}
    assert catalog.a9["uuid"] not in {item["uuid"] for item in in_collection["items"]}


def test_only_active_assets_and_any_exclude_tag(catalog):
    active = search.search(**_filtered(collection_id=catalog.collection,
                                       exclude_tags=[]))
    assert catalog.a4["uuid"] in {item["uuid"] for item in active["items"]}
    assert catalog.archived["uuid"] not in {item["uuid"] for item in active["items"]}

    # Either excluded tag is enough to drop an asset: a2 carries archive,
    # a4 carries internal.
    excluded = search.search(**_filtered(collection_id=catalog.collection,
                                         exclude_tags=[INTERNAL, ARCHIVE]))
    uuids = {item["uuid"] for item in excluded["items"]}
    assert excluded["total"] == 4
    assert catalog.a2["uuid"] not in uuids
    assert catalog.a4["uuid"] not in uuids
    assert catalog.a1["uuid"] in uuids and catalog.a3["uuid"] in uuids


def test_duplicate_include_tags_do_not_change_results(catalog):
    once = search.search(**_filtered(collection_id=catalog.collection))
    repeated = search.search(**_filtered(
        collection_id=catalog.collection,
        tags=[APPROVED, CAMPAIGN, APPROVED, "  Quiz-Approved  "],
    ))
    assert repeated["total"] == once["total"] == 5
    assert [i["uuid"] for i in repeated["items"]] == [i["uuid"] for i in once["items"]]


def test_include_and_exclude_same_tag_yields_empty_result(catalog):
    result = search.search(**_filtered(
        collection_id=catalog.collection,
        tags=[APPROVED],
        exclude_tags=[APPROVED],
    ))
    assert result["total"] == 0
    assert result["items"] == []


def test_keyword_search_obeys_selected_created_sort(catalog):
    oldest = search.search(**_filtered(collection_id=catalog.collection, sort="oldest"))
    newest = search.search(**_filtered(collection_id=catalog.collection, sort="newest"))

    oldest_uuids = [item["uuid"] for item in oldest["items"]]
    newest_uuids = [item["uuid"] for item in newest["items"]]
    assert oldest_uuids == [catalog.a1["uuid"], catalog.a2["uuid"], catalog.a3["uuid"],
                            catalog.a7["uuid"], catalog.a8["uuid"]]
    assert newest_uuids == list(reversed(oldest_uuids))
    assert [item["created_at"] for item in oldest["items"]] == sorted(
        item["created_at"] for item in oldest["items"]
    )


def test_pagination_is_stable_with_identical_created_at(catalog):
    expected = [catalog.a1["uuid"], catalog.a2["uuid"], catalog.a3["uuid"],
                catalog.a7["uuid"], catalog.a8["uuid"]]
    pages = []
    for offset in (0, 2, 4):
        page = search.search(**_filtered(
            collection_id=catalog.collection, sort="oldest", limit=2, offset=offset
        ))
        assert page["total"] == 5
        pages.extend(item["uuid"] for item in page["items"])

    assert pages == expected
    assert len(pages) == len(set(pages)) == 5

    # Repeating a page while the data is unchanged must return the same rows
    # in the same order, including the two assets with equal created_at.
    again = search.search(**_filtered(
        collection_id=catalog.collection, sort="oldest", limit=2, offset=2
    ))
    assert [item["uuid"] for item in again["items"]] == expected[2:4]


def test_export_rows_match_the_full_sorted_match_set(catalog):
    params = _filtered(collection_id=catalog.collection, sort="oldest")
    expected = [catalog.a1["uuid"], catalog.a2["uuid"], catalog.a3["uuid"],
                catalog.a7["uuid"], catalog.a8["uuid"]]

    rows = search.export_rows(**{k: v for k, v in params.items() if k not in ("limit", "offset")})
    assert [row["uuid"] for row in rows] == expected

    # A single page is a prefix of the export; export itself is not limited.
    page_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    page = search.search(**page_params, limit=2, offset=0)
    assert [item["uuid"] for item in page["items"]] == expected[:2]
    assert len(rows) == 5 > len(page["items"])


@pytest.fixture(scope="module")
def cjk_asset():
    title = f'{KW}cjk 海港, "晨光"\n第二行'
    description = '描述, 含"引号"\n以及换行'
    filename = f"{KW}-cjk,file.png"
    asset_id, asset_uuid = _create_asset(
        title,
        tags=["quiz-cjk", "含,逗号", '带"引号'],
        description=description,
        seed=90,
        filename=filename,
    )
    return {
        "id": asset_id,
        "uuid": asset_uuid,
        "title": title,
        "description": description,
        "filename": filename,
    }


def test_csv_round_trips_chinese_commas_quotes_and_newlines(cjk_asset):
    rows = search.export_rows(q=f"{KW}cjk", tags=["quiz-cjk"])
    assert len(rows) == 1

    text = export.rows_to_csv(rows)
    assert text.startswith("\ufeff"), "BOM keeps Excel from mangling Chinese"
    table = list(csv.reader(io.StringIO(text[1:], newline="")))
    assert table[0] == list(export.COLUMNS)
    record = dict(zip(table[0], table[1]))
    assert record["title"] == cjk_asset["title"]
    assert record["description"] == cjk_asset["description"]
    assert record["filename"] == cjk_asset["filename"]
    assert record["media_type"] == "image"
    assert record["created_at"]
    assert set(record["tags"].split(export.TAG_SEPARATOR)) == {"quiz-cjk", "含,逗号", '带"引号'}


def test_empty_export_still_has_header():
    rows = search.export_rows(q="zzz_no_such_asset_zzz")
    assert rows == []
    text = export.rows_to_csv(rows)
    table = list(csv.reader(io.StringIO(text[1:], newline="")))
    assert table == [list(export.COLUMNS)]


def test_media_type_filter_applies_to_export():
    token = f"mediatoken{uuidlib.uuid4().hex[:8]}"
    _create_asset(
        f"{token} report",
        tags=["quiz-media"],
        filename=f"{token}.txt",
        data=f"{token} plain text report".encode(),
    )

    documents = search.export_rows(q=token, media_type="document")
    images = search.export_rows(q=token, media_type="image")
    assert len(documents) == 1
    assert documents[0]["media_type"] == "document"
    assert images == []


def test_export_endpoint_requires_login():
    with TestClient(app) as anonymous:
        response = anonymous.get("/api/assets/export.csv", params={"q": KW})
    assert response.status_code == 401


def test_export_endpoint_returns_same_rows_as_service(client, catalog):
    params = [
        ("q", KW),
        ("collection_id", str(catalog.collection)),
        ("tags", APPROVED),
        ("tags", CAMPAIGN),
        ("exclude_tags", INTERNAL),
        ("sort", "oldest"),
    ]
    response = client.get("/api/assets/export.csv", params=params)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-total-count"] == "5"

    table = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))
    assert table[0] == list(export.COLUMNS)
    assert [row[0] for row in table[1:]] == [
        catalog.a1["uuid"], catalog.a2["uuid"], catalog.a3["uuid"],
        catalog.a7["uuid"], catalog.a8["uuid"],
    ]


def test_export_endpoint_zero_results_is_header_only(client):
    response = client.get("/api/assets/export.csv", params={"q": "zzz_no_match_zzz"})
    assert response.status_code == 200
    assert response.headers["x-total-count"] == "0"
    table = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))
    assert table == [list(export.COLUMNS)]


def test_list_endpoint_accepts_repeated_tag_parameters(client, catalog):
    response = client.get("/api/assets", params=[
        ("q", KW),
        ("collection_id", str(catalog.collection)),
        ("tags", APPROVED),
        ("tags", CAMPAIGN),
        ("exclude_tags", INTERNAL),
        ("sort", "oldest"),
        ("limit", "2"),
        ("offset", "0"),
    ])
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert [item["uuid"] for item in body["items"]] == [
        catalog.a1["uuid"], catalog.a2["uuid"],
    ]


def test_invalid_pagination_parameters_are_rejected(client):
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": "abc"}):
        response = client.get("/api/assets", params=params)
        assert response.status_code == 422, (params, response.status_code)


def test_special_character_keywords_do_not_error(client, catalog):
    for query in ('"', "!!!", "and or not", f'{KW} "quoted"', "海港", "100%", "a:b"):
        response = client.get("/api/assets", params={"q": query, "limit": 5})
        assert response.status_code == 200, (query, response.status_code, response.text)

    # Punctuation-only input has no FTS terms and therefore means "no keyword".
    result = search.search(q='"""', limit=1)
    assert result["total"] >= 1


def test_chinese_keyword_search_finds_asset(cjk_asset):
    result = search.search(q="海港", tags=["quiz-cjk"])
    assert result["total"] == 1
    assert "海港" in result["items"][0]["title"]


def test_edit_title_description_and_tags_reflect_in_search_and_export(client):
    asset_id, asset_uuid = _create_asset(
        "edittoken original title",
        tags=["quiz-edit-old"],
        description="old description",
        seed=120,
    )
    assert search.search(q="edittoken", tags=["quiz-edit-old"])["total"] == 1

    response = client.patch(
        f"/api/assets/{asset_uuid}",
        json={"title": "edittoken renamed title", "description": "fresh description"},
    )
    assert response.status_code == 200

    updated = search.search(q="edittoken", tags=["quiz-edit-old"])
    assert updated["items"][0]["title"] == "edittoken renamed title"
    assert search.search(q="original", tags=["quiz-edit-old"])["total"] == 0

    response = client.post(f"/api/assets/{asset_uuid}/tags", json={"name": "quiz-edit-new"})
    assert response.status_code == 200
    tagged = search.search(q="edittoken", tags=["quiz-edit-new"])
    assert tagged["total"] == 1
    rows = search.export_rows(q="edittoken", tags=["quiz-edit-new"])
    assert rows[0]["description"] == "fresh description"

    response = client.delete(f"/api/assets/{asset_uuid}/tags/quiz-edit-old")
    assert response.status_code == 200
    assert search.search(q="edittoken", tags=["quiz-edit-old"])["total"] == 0


def test_csv_formula_prefix_is_neutralised():
    rows = [{
        "uuid": "u-1",
        "title": "=HYPERLINK(\"http://example.org\")",
        "description": "-42 + 1",
        "filename": "report.csv",
        "media_type": "document",
        "tags": ["a"],
        "created_at": "2026-01-01T00:00:00+00:00",
    }]
    table = list(csv.reader(io.StringIO(export.rows_to_csv(rows)[1:], newline="")))
    assert table[1][1].startswith("'=")
    assert table[1][2].startswith("'-")
