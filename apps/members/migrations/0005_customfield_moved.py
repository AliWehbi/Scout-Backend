"""CustomField now lives in the customfields app (same table, nothing changes in the database)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("members", "0004_member_left_date"),
        ("customfields", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="CustomField")],
            database_operations=[],
        ),
    ]
