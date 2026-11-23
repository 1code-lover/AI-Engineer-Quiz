"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tags, collection, status)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

The structured filters compose: every condition is ANDed, ``tags`` requires
all named tags, and ``exclude_tags`` rejects an asset that carries any of
them. Results are ordered by a caller-chosen sort plus an immutable id
tie-breaker, so pagination is stable while the data does not change.

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar. They describe the whole active catalog, not the current
filtered result set; callers must not present them as match counts.
"""
from __future__ import annotations

from typing import Iterable, Optional

from . import ai
from .db import get_conn

# Sort keys always end with ``a.id``. Two assets can share a created_at (or a
# title/size), and without a deterministic tie-breaker SQLite may return them
# in a different order between LIMIT/OFFSET queries, duplicating one row and
# dropping another across page boundaries.
ORDER_BY = {
    "newest": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id DESC",
}


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query.

    Every non-alphanumeric character becomes a separator and each term is
    quoted, so FTS5 operators, quotes and other syntax cannot leak through.
    Text made up only of punctuation yields no terms, which means "no keyword
    constraint" rather than an invalid ``MATCH ''`` expression.
    """
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def normalize_tag_names(values: Optional[Iterable[str]]) -> list[str]:
    """Normalise tag names the same way the write paths do.

    Tag names are stored lowercase and matched by full name. Duplicates are
    dropped so repeated query parameters cannot change the result set.
    """
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if value is None:
            continue
        name = str(value).strip().lower()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _build_query(
    *,
    q: str,
    media_type: Optional[str],
    tags: list[str],
    exclude_tags: list[str],
    collection_id: Optional[int],
    status: str,
) -> tuple[str, str, list]:
    """Build the shared FROM/WHERE clause for list and export queries."""
    where = ["a.status = ?"]
    params: list = [status]
    join = ""

    fts = _fts_query(q) if q and q.strip() else ""
    if fts:
        join += " JOIN asset_fts f ON f.rowid = a.id"
        where.append("asset_fts MATCH ?")
        params.append(fts)
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)
    if collection_id is not None:
        # EXISTS rather than a join: a malformed/duplicated join row must not
        # change the result set, and the same predicate also serves COUNT.
        where.append(
            "EXISTS (SELECT 1 FROM collection_assets ca "
            "WHERE ca.asset_id = a.id AND ca.collection_id = ?)"
        )
        params.append(collection_id)
    for name in tags:
        where.append(
            "EXISTS (SELECT 1 FROM asset_tags inc JOIN tags ti ON ti.id = inc.tag_id "
            "WHERE inc.asset_id = a.id AND ti.name = ?)"
        )
        params.append(name)
    for name in exclude_tags:
        where.append(
            "NOT EXISTS (SELECT 1 FROM asset_tags exc JOIN tags te ON te.id = exc.tag_id "
            "WHERE exc.asset_id = a.id AND te.name = ?)"
        )
        params.append(name)

    return join, " AND ".join(where), params


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    conn = get_conn()

    # Semantic mode ranks the candidate pool by embedding similarity.
    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    include = normalize_tag_names(([tag] if tag else []) + list(tags or []))
    exclude = normalize_tag_names(exclude_tags)
    join, where_sql, params = _build_query(
        q=q,
        media_type=media_type,
        tags=include,
        exclude_tags=exclude,
        collection_id=collection_id,
        status=status,
    )
    order = ORDER_BY.get(sort, ORDER_BY["newest"])

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a{join} WHERE {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT a.* FROM assets a{join} WHERE {where_sql}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    return {
        "total": total,
        "items": [_card(dict(r)) for r in rows],
        "facets": _facets(status),
        "mode": mode,
        "limit": limit,
        "offset": offset,
    }


def export_rows(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
) -> list[dict]:
    """Return every asset matching the list filters, without pagination.

    The filter and order clauses are the same ones used by :func:`search`, so
    an export is exactly the full sequence a paged list walks through.
    """
    conn = get_conn()
    include = normalize_tag_names(([tag] if tag else []) + list(tags or []))
    exclude = normalize_tag_names(exclude_tags)
    join, where_sql, params = _build_query(
        q=q,
        media_type=media_type,
        tags=include,
        exclude_tags=exclude,
        collection_id=collection_id,
        status=status,
    )
    order = ORDER_BY.get(sort, ORDER_BY["newest"])
    rows = conn.execute(
        f"SELECT a.* FROM assets a{join} WHERE {where_sql} ORDER BY {order}",
        params,
    ).fetchall()

    tag_map = _tags_for([r["id"] for r in rows])
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item.pop("embedding", None)
        item["tags"] = tag_map.get(row["id"], [])
        out.append(item)
    return out


def _tags_for(asset_ids: list[int]) -> dict[int, list[str]]:
    """Load tag names for many assets, alphabetically, in bounded batches."""
    result: dict[int, list[str]] = {asset_id: [] for asset_id in asset_ids}
    if not asset_ids:
        return result
    conn = get_conn()
    # Keep the IN list comfortably below SQLite's default variable limit.
    chunk_size = 500
    for start in range(0, len(asset_ids), chunk_size):
        chunk = asset_ids[start:start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""SELECT at.asset_id, t.name FROM asset_tags at
                JOIN tags t ON t.id = at.tag_id
                WHERE at.asset_id IN ({placeholders})
                ORDER BY t.name""",
            chunk,
        ).fetchall()
        for row in rows:
            result[row["asset_id"]].append(row["name"])
    return result


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {"total": len(scored), "items": items, "facets": _facets(status), "mode": "semantic"}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    """Catalog-wide facet counts.

    These are deliberately not narrowed by the active keyword/tag filters.
    The UI labels them as catalog totals so they are never mistaken for the
    size of the current match set.
    """
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
