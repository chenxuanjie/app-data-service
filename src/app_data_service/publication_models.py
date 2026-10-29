"""Public invalidation watermarks only; no credentials or private release records."""

import sqlalchemy as sa

from .models import Base

versions = sa.Table(
    "publication_versions",
    Base.metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("app_id", sa.Text, nullable=False),
    sa.Column("topic", sa.Text, nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("channel", sa.Text, nullable=False),
    sa.Column("platform", sa.Text, nullable=False),
    sa.Column("architecture", sa.Text, nullable=False),
    schema="core",
)
