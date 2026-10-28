"""Application-scoped public installer publications, independent of user accounts."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from .models import Base

blobs = sa.Table(
    "release_blobs",
    Base.metadata,
    sa.Column("app_id", sa.Text, sa.ForeignKey("core.apps.id"), primary_key=True),
    sa.Column("sha256", sa.Text, primary_key=True),
    sa.Column("size", sa.BigInteger, nullable=False),
    schema="core",
)

releases = sa.Table(
    "public_releases",
    Base.metadata,
    sa.Column("app_id", sa.Text, primary_key=True),
    sa.Column("channel", sa.Text, primary_key=True),
    sa.Column("platform", sa.Text, primary_key=True),
    sa.Column("architecture", sa.Text, primary_key=True),
    sa.Column("build_number", sa.BigInteger, primary_key=True),
    sa.Column("version_name", sa.Text, nullable=False),
    sa.Column("minimum_build", sa.BigInteger, nullable=False),
    sa.Column("force_update", sa.Boolean, nullable=False),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("notes", JSONB, nullable=False),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("size", sa.BigInteger, nullable=False),
    sa.Column("mime_type", sa.Text, nullable=False),
    sa.Column("record", JSONB, nullable=False),
    sa.ForeignKeyConstraint(["app_id", "sha256"], [blobs.c.app_id, blobs.c.sha256]),
    schema="core",
)
