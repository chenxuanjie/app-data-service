"""Bounded, idempotent board-game commands; server derives actor and application."""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import distinct_on, insert

from app_data_service.models import Dataset, User

from .models import PERSONAL, comments, likes, preferences, receipts


def re_slug(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00]+$")

    @field_validator("*")
    @classmethod
    def valid_text(cls, value):
        def check(item, depth=0):
            if depth > 20 or isinstance(item, str) and "\x00" in item:
                raise ValueError("Invalid record text or structure")
            if isinstance(item, dict):
                for key, entry in item.items():
                    check(key, depth + 1)
                    check(entry, depth + 1)
            elif isinstance(item, list):
                for entry in item:
                    check(entry, depth + 1)

        check(value)
        return value


class Favorite(Record):
    game_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    created_at: AwareDatetime


class Recent(Record):
    game_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    visited_at: AwareDatetime


class Preference(Record):
    id: Literal["profile"] = "profile"
    nickname: str = Field(default="", max_length=80)
    voice_reply_enabled: bool = False
    assistant_mode: Literal["text_and_dictation", "realtime_voice"] = "text_and_dictation"
    game_answer_mode: Literal["knowledge_only", "knowledge_then_direct"] = "knowledge_only"
    global_answer_mode: Literal["knowledge_only", "knowledge_then_direct"] = "knowledge_then_direct"
    use_game_knowledge: bool = False


class Conversation(Record):
    title: str = Field(max_length=200)
    scope: Literal["global", "game"]
    game_id: str | None = Field(default=None, max_length=200)
    created_at: AwareDatetime
    modified_at: AwareDatetime
    opened: bool = True


class Message(Record):
    conversation_id: str = Field(min_length=1, max_length=200)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=512000)
    created_at: AwareDatetime
    state: Literal["complete", "failed"] = "complete"
    source: str | None = Field(default=None, max_length=80)
    citations: list[dict] = Field(default_factory=list, max_length=200)
    evidence: list[dict] = Field(default_factory=list, max_length=200)
    can_retry: bool = False
    retry_prompt: str | None = Field(default=None, max_length=512000)


class Vote(Record):
    campaign: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    game_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")


RECORDS = dict(
    zip(PERSONAL, (Favorite, Recent, Preference, Conversation, Message, Vote), strict=True)
)


class Mutation(Record):
    entity: Literal[
        "favorites", "recent_games", "preferences", "conversations", "messages", "votes"
    ]
    record_id: str = Field(min_length=1, max_length=200)
    record: dict | None
    changed_fields: list[str] | None = Field(default=None, min_length=1, max_length=6)


class Upload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1, max_length=200)
    mutations: list[Mutation] = Field(min_length=1, max_length=100)


def visible_dataset(connection, dataset_id):
    if not connection.scalar(
        sa.select(Dataset.id).where(Dataset.id == dataset_id, Dataset.app_id == "board_game_agent")
    ):
        raise HTTPException(404, "dataset_unavailable")


def receipt(connection, actor, identifier, payload, apply):
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # Serializes this actor's commands, including absent-row concurrent inserts.
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:actor,713))"), {"actor": actor}
    )
    old = (
        connection.execute(
            sa.select(receipts).where(receipts.c.user_id == actor, receipts.c.id == identifier)
        )
        .mappings()
        .one_or_none()
    )
    if old:
        if old["payload_hash"] != digest:
            raise HTTPException(409, "mutation_reused")
        return old["result"]
    result = apply()
    connection.execute(
        receipts.insert().values(user_id=actor, id=identifier, payload_hash=digest, result=result)
    )
    return result


def upload(connection, actor, body):
    visible_dataset(connection, body.dataset_id)
    results = []
    for mutation in body.mutations:

        def apply(mutation=mutation):
            table = PERSONAL[mutation.entity]
            predicate = (table.c.dataset_id == body.dataset_id, table.c.id == mutation.record_id)
            old = (
                connection.execute(sa.select(table).where(*predicate).with_for_update())
                .mappings()
                .one_or_none()
            )
            now = datetime.now(UTC)
            if mutation.record is None:
                if old:
                    connection.execute(
                        table.update()
                        .where(*predicate)
                        .values(deleted_at=now, updated_at=now, row_version=table.c.row_version + 1)
                    )
                return {
                    "id": mutation.id,
                    "status": "applied",
                    "version": old["row_version"] + 1 if old else 0,
                }
            record = RECORDS[mutation.entity].model_validate(mutation.record)
            if record.id != mutation.record_id:
                raise HTTPException(422, "record_identity_mismatch")
            if mutation.entity in ("favorites", "recent_games") and record.id != record.game_slug:
                raise HTTPException(422, "record_identity_mismatch")
            if mutation.entity == "votes" and record.id != f"{record.campaign}|{record.game_slug}":
                raise HTTPException(422, "record_identity_mismatch")
            if mutation.entity == "messages" and not record.id.startswith(
                record.conversation_id + "|"
            ):
                raise HTTPException(422, "record_identity_mismatch")
            values = record.model_dump()
            if mutation.changed_fields is not None:
                allowed = set(Preference.model_fields) - {"id"}
                if mutation.entity != "preferences" or not set(mutation.changed_fields) <= allowed:
                    raise HTTPException(422, "preference_fields_invalid")
                current = (
                    Preference().model_dump()
                    if old is None
                    else {key: old[key] for key in Preference.model_fields}
                )
                values = {**current, **{key: values[key] for key in mutation.changed_fields}}
            version = old["row_version"] + 1 if old else 1
            values.update(
                dataset_id=body.dataset_id, row_version=version, updated_at=now, deleted_at=None
            )
            statement = insert(table).values(**values)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[table.c.dataset_id, table.c.id], set_=values
                )
            )
            return {"id": mutation.id, "status": "applied", "version": version}

        results.append(
            receipt(
                connection,
                actor,
                mutation.id,
                {"dataset_id": body.dataset_id, **mutation.model_dump()},
                apply,
            )
        )
    return {"results": results}


def public_actor(identifier):
    return hashlib.sha256(f"board_game_agent.public.v1|{identifier}".encode()).hexdigest()


class CommunityCommand(Record):
    action: Literal["publish", "delete", "like"]
    game_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    comment_id: str = Field(pattern=r"^c[a-f0-9]{32}$")
    body: str = Field(default="", max_length=1000)
    rating: int | None = Field(default=None, ge=1, le=10)
    parent_id: str | None = Field(default=None, pattern=r"^c[a-f0-9]{32}$")
    active: bool = False


def community_command(connection, actor, body):
    def apply():
        target = (
            connection.execute(sa.select(comments).where(comments.c.id == body.comment_id))
            .mappings()
            .one_or_none()
        )
        if body.action == "publish":
            if target or not body.body.strip():
                raise HTTPException(409 if target else 422, "comment_rejected")
            parent = None
            if body.parent_id:
                parent = (
                    connection.execute(sa.select(comments).where(comments.c.id == body.parent_id))
                    .mappings()
                    .one_or_none()
                )
                if (
                    parent is None
                    or parent["deleted_at"]
                    or parent["game_slug"] != body.game_slug
                    or body.rating is not None
                ):
                    raise HTTPException(409, "comment_parent_unavailable")
            nickname = connection.scalar(
                sa.select(preferences.c.nickname)
                .join(Dataset, preferences.c.dataset_id == Dataset.id)
                .where(Dataset.owner_id == actor, preferences.c.deleted_at.is_(None))
            )
            name = nickname or connection.scalar(
                sa.select(User.display_name).where(User.id == actor)
            )
            connection.execute(
                comments.insert().values(
                    id=body.comment_id,
                    user_id=actor,
                    game_slug=body.game_slug,
                    author_name=(name or "玩家")[:80],
                    body=body.body.strip(),
                    rating=body.rating,
                    parent_id=body.parent_id,
                    root_id=(parent["root_id"] or parent["id"]) if parent else None,
                )
            )
        else:
            if target is None or target["game_slug"] != body.game_slug:
                raise HTTPException(404, "comment_unavailable")
            if body.action == "delete":
                if target["user_id"] != actor:
                    raise HTTPException(403, "comment_not_owned")
                connection.execute(
                    comments.update()
                    .where(comments.c.id == body.comment_id)
                    .values(deleted_at=datetime.now(UTC))
                )
            elif target["deleted_at"]:
                raise HTTPException(409, "comment_deleted")
            else:
                values = dict(comment_id=body.comment_id, user_id=actor, active=body.active)
                connection.execute(
                    insert(likes)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[likes.c.comment_id, likes.c.user_id],
                        set_={"active": body.active},
                    )
                )
        return {"id": body.id, "status": "applied"}

    return receipt(connection, actor, body.id, body.model_dump(), apply)


def feed(connection, game, actor, *, cursor=None, limit=100):
    predicate = [comments.c.game_slug == game]
    if cursor:
        try:
            stamp, identifier = json.loads(base64.urlsafe_b64decode(cursor))
            date = datetime.fromisoformat(stamp)
            if not date.tzinfo or not re.fullmatch(r"c[a-f0-9]{32}", identifier):
                raise ValueError()
            predicate.append(sa.tuple_(comments.c.created_at, comments.c.id) < (date, identifier))
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(422, "comment_cursor_invalid") from error
    rows = (
        connection.execute(
            sa.select(comments)
            .where(*predicate)
            .order_by(comments.c.created_at.desc(), comments.c.id.desc())
            .limit(limit + 1)
        )
        .mappings()
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    counts = dict(
        connection.execute(
            sa.text("SELECT comment_id,n FROM board_game_agent.like_counts(:game)"), {"game": game}
        ).all()
    )
    mine = (
        set(
            connection.execute(
                sa.select(likes.c.comment_id).where(likes.c.user_id == actor, likes.c.active)
            ).scalars()
        )
        if actor
        else set()
    )
    entries = [
        dict(
            id=r["id"],
            author_id=public_actor(r["user_id"]),
            game_slug=r["game_slug"],
            author=r["author_name"],
            text="" if r["deleted_at"] else r["body"],
            rating=r["rating"],
            parent_id=r["parent_id"],
            root_id=r["root_id"],
            created_at=r["created_at"],
            deleted=r["deleted_at"] is not None,
            likes=counts.get(r["id"], 0),
            liked_by_me=r["id"] in mine,
        )
        for r in rows
    ]
    heads = (
        sa.select(comments.c.user_id, comments.c.rating, comments.c.deleted_at)
        .ext(distinct_on(comments.c.user_id))
        .where(comments.c.game_slug == game, comments.c.rating.is_not(None))
        .order_by(comments.c.user_id, comments.c.created_at.desc(), comments.c.id.desc())
        .subquery()
    )
    distribution = dict(
        connection.execute(
            sa.select(heads.c.rating, sa.func.count())
            .where(heads.c.deleted_at.is_(None))
            .group_by(heads.c.rating)
        ).all()
    )
    count = sum(distribution.values())
    return {
        "comments": entries,
        "ratings": {
            "count": count,
            "average": sum(k * v for k, v in distribution.items()) / count if count else None,
            "distribution": {str(i): distribution.get(i, 0) for i in range(1, 11)},
        },
        "next_cursor": base64.urlsafe_b64encode(
            json.dumps([rows[-1]["created_at"].isoformat(), rows[-1]["id"]]).encode()
        ).decode()
        if more
        else None,
    }
