from django.db import migrations

DEFAULT_TYPES = (
    {"name_en": "Meeting", "name_ar": "اجتماع", "color": "primary", "icon": "groups", "default_mandatory": True},
    {"name_en": "Activity", "name_ar": "نشاط", "color": "secondary", "icon": "hiking", "default_mandatory": False},
)


def seed(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    EventType = apps.get_model("events", "EventType")
    for org in Organization.objects.all():
        for order, spec in enumerate(DEFAULT_TYPES):
            EventType.objects.get_or_create(organization=org, name_en=spec["name_en"], defaults={**spec, "order": order})


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial"), ("organizations", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
