"""Administrative registration after explicit schema migrations, never from an HTTP request."""

import sqlalchemy as sa

from .models import App


def registration_values(definition):
    return {
        "id": definition.id,
        "name": definition.name,
        "schema_name": definition.schema,
        "personal_data": definition.personal_data,
        "personal_space_name": definition.personal_space_name,
        "personal_id_prefix": definition.personal_id_prefix,
    }


def register_application(connection, definition):
    if not connection.in_transaction():
        raise ValueError("Registration requires an explicit migration transaction")
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:app_id,103))"),
        {"app_id": definition.id},
    )
    if (
        connection.scalar(sa.text("SELECT to_regnamespace(:name)"), {"name": definition.schema})
        is None
    ):
        raise ValueError("Create the application schema in a reviewed migration first")
    expected = registration_values(definition)
    previous = (
        connection.execute(sa.select(App).where(App.id == definition.id).with_for_update())
        .mappings()
        .one_or_none()
    )
    if previous:
        if any(previous[key] != value for key, value in expected.items()):
            raise ValueError("Existing application metadata requires an explicit migration")
    else:
        connection.execute(sa.insert(App).values(**expected))
