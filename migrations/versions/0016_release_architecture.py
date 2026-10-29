"""Align generic Android packages with the shared remote_update `any` contract."""

from alembic import op

revision = "0016_release_architecture"
down_revision = "0015_public_releases"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
      ALTER TABLE core.public_releases NO FORCE ROW LEVEL SECURITY;
      ALTER TABLE core.public_releases DROP CONSTRAINT public_releases_architecture_check;
      -- Normalize the indexed target, preserving the immutable original audit record.
      UPDATE core.public_releases SET architecture='any' WHERE architecture='universal';
      ALTER TABLE core.public_releases ADD CONSTRAINT public_releases_architecture_check
        CHECK(architecture IN ('arm64','arm','x64','x86','any'));
      ALTER TABLE core.public_releases FORCE ROW LEVEL SECURITY;
    """)


def downgrade():
    raise RuntimeError("Keep the canonical release contract; use a reviewed forward migration")
