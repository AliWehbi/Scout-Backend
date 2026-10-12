import django.db.models.deletion
from django.db import migrations, models


def drop_orphans(apps, schema_editor):
    # Rows without a Member can't exist after members.0002, but be defensive.
    apps.get_model("units", "UnitMembership").objects.filter(member__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("units", "0002_unitmembership_member_and_more"),
        ("members", "0002_members_from_logins"),
    ]

    operations = [
        migrations.RunPython(drop_orphans, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="unitmembership", name="unit_membership_unique"),
        migrations.RemoveField(model_name="unitmembership", name="membership"),
        migrations.AlterField(
            model_name="unitmembership",
            name="member",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="unit_memberships", to="members.member"
            ),
        ),
        migrations.AddConstraint(
            model_name="unitmembership",
            constraint=models.UniqueConstraint(fields=("unit", "member"), name="unit_member_unique"),
        ),
    ]
