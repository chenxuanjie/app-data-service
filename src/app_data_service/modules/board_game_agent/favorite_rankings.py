"""Read the current public catalog and favorite totals from one SQL snapshot."""

from typing import Literal

import sqlalchemy as sa
from sqlalchemy import Engine

from . import business
from . import catalog_models as catalog
from .catalog_service import current_editions, public_snapshot

RankingLocale = Literal["zhHans", "en"]


class RankingsUnavailable(RuntimeError):
    pass


def read_favorite_rankings(engine: Engine, limit: int, locale: RankingLocale):
    with public_snapshot(engine) as (connection, revision):
        if revision is None:
            raise RankingsUnavailable("rankings_unavailable")
        counts = business.public_counts(connection)
        if counts is None:
            raise RankingsUnavailable("rankings_unavailable")
        editions, texts = catalog.editions, catalog.texts
        title = (
            sa.select(texts.c.title)
            .where(texts.c.batch_id == editions.c.batch_id, texts.c.edition_id == editions.c.id)
            .order_by(
                sa.case(
                    (texts.c.locale == locale, 0),
                    (texts.c.locale == "en", 1),
                    (texts.c.locale == "zhHans", 2),
                    else_=3,
                ),
                texts.c.locale,
            )
            .limit(1)
            .scalar_subquery()
        )
        games = connection.execute(
            sa.select(editions.c.slug, sa.func.coalesce(title, editions.c.slug))
            .select_from(current_editions(revision))
            .where(editions.c.enabled)
            .order_by(editions.c.sort_order, editions.c.id)
        ).all()

    # Match Flutter's first occurrence per slug and UTF-16 title comparison.
    titles = {}
    for slug, title in games:
        slug = slug.strip()
        if slug:
            titles.setdefault(slug, title)
    favorites = counts["favorites"]
    rows = [(slug, title, max(0, favorites.get(slug, 0))) for slug, title in titles.items()]
    rows.sort(key=lambda row: (-row[2], row[1].lower().encode("utf-16-be"), row[0]))
    items = []
    rank, previous = 0, -1
    for index, (slug, title, count) in enumerate(rows[:limit], start=1):
        if count != previous:
            rank, previous = index, count
        items.append({"slug": slug, "title": title, "rank": rank, "favorite_count": count})
    return {"catalog_revision": revision, "items": items}
