"""Stream immutable file objects into SQL without holding a full library in RAM."""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from . import catalog_models as m
from .catalog_import import safe_path

TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".xml",
    ".toml",
    ".ini",
    ".svg",
    ".log",
    ".sql",
    ".css",
    ".js",
}


def decode_body(raw, path):
    encodings = ["utf-8-sig"]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    elif Path(path).suffix.lower() in TEXT_EXTENSIONS:
        encodings.append("gb18030")
    for encoding in encodings:
        try:
            value = raw.decode(encoding)
            if "\x00" in value:
                continue
            return value, encoding
        except UnicodeError:
            pass
    return None, None


@contextmanager
def object_transaction(engine):
    with engine.begin() as c:
        c.exec_driver_sql("SET LOCAL transaction_timeout='20s'")
        yield c


def save_objects(engine, plan, root, progress):
    records = list(plan.files.values())
    for offset in range(0, len(records), 100):
        values = [
            {k: r[k] for k in ("sha256", "size", "storage_key")}
            for r in records[offset : offset + 100]
        ]
        with object_transaction(engine) as c:
            c.execute(insert(m.files).values(values).on_conflict_do_nothing())
            existing = {
                r["sha256"]: dict(r)
                for r in c.execute(
                    sa.select(m.files).where(m.files.c.sha256.in_([r["sha256"] for r in values]))
                ).mappings()
            }
            if any(existing.get(r["sha256"]) != r for r in values):
                raise ValueError("blob_metadata_conflict")
    # Each body is read before its bounded transaction; at most one small file is
    # buffered. A failed draft can leave unreferenced immutable objects, never a
    # partial public edition. Body records inherit the resource's publication RLS.
    with engine.connect() as c:
        stored = set(
            c.scalars(
                sa.select(m.file_bodies.c.sha256).where(
                    m.file_bodies.c.sha256.in_([r["sha256"] for r in records if r["sql_inline"]])
                )
            )
        )
    for record in records:
        if not record["sql_inline"] or record["sha256"] in stored:
            continue
        with safe_path(root, record["storage_key"]).open("rb") as incoming:
            raw = incoming.read(plan.sql_file_max + 1)
        if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("publication_blob_integrity_failure")
        text, encoding = decode_body(raw, record["source_path"])
        structured = None
        if text is not None:
            try:
                candidate = json.loads(text)
                json.dumps(candidate, allow_nan=False)
                structured = candidate
            except (ValueError, RecursionError):
                pass
        with object_transaction(engine) as c:
            result = c.execute(
                insert(m.file_bodies)
                .values(
                    sha256=record["sha256"],
                    body=raw,
                    text_body=text,
                    encoding=encoding,
                    json_body=sa.null() if structured is None else structured,
                )
                .on_conflict_do_nothing()
            )
        if progress and result.rowcount:
            progress(
                "sql_file_saved",
                path=record["source_path"],
                bytes=len(raw),
                message="小文件原文已写入 SQL",
                content_type="json"
                if structured is not None
                else "text"
                if text is not None
                else "binary",
            )
