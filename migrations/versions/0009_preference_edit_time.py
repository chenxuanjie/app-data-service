"""Track the actual edit time for automatically merged allocation preferences."""

import sqlalchemy as sa
from alembic import op

revision = "0009_preference_edit_time"
down_revision = "0008_personal_ledgers"
branch_labels = None
depends_on = None


def upgrade():
    # Existing rows deliberately remain unknown: upload time is not edit time.
    op.add_column(
        "business_preferences", sa.Column("edited_at", sa.DateTime(timezone=True)), schema="zheri"
    )


def downgrade():
    op.drop_column("business_preferences", "edited_at", schema="zheri")
