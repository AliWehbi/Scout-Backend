"""Every organization gets a permanent code, the prefix of its custom template codes."""

from django.db import migrations, models


def fill_codes(apps, schema_editor):
    from apps.organizations.models import make_org_code

    Organization = apps.get_model("organizations", "Organization")
    taken = set()
    for org in Organization.objects.order_by("created_at"):
        org.code = make_org_code(org.name, lambda c: c in taken)
        taken.add(org.code)
        org.save(update_fields=["code"])


class Migration(migrations.Migration):
    dependencies = [("organizations", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="code",
            field=models.CharField(editable=False, max_length=20, null=True),
        ),
        migrations.RunPython(fill_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="organization",
            name="code",
            field=models.CharField(editable=False, max_length=20, unique=True),
        ),
    ]
