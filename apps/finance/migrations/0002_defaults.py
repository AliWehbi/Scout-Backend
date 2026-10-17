"""Every existing organization gets its currency as the default one, and starting categories."""

from django.db import migrations

from apps.finance.presets import preset

CATEGORIES = (
    ("expense", "Transport", "النقل", "#2B6A95"),
    ("expense", "Food", "الطعام", "#8F5D1C"),
    ("expense", "Campsite & venue", "المخيم والمكان", "#2F5D3A"),
    ("expense", "Equipment", "المعدّات", "#935700"),
    ("expense", "Badges & uniforms", "الشارات واللباس", "#1D5E63"),
    ("expense", "Hall & utilities", "القاعة والخدمات", "#595D53"),
    ("income", "Membership dues", "الاشتراكات", "#2F5D3A"),
    ("income", "Participation fees", "رسوم المشاركة", "#2E7D4F"),
    ("income", "Donations", "التبرعات", "#7A2335"),
)


def forwards(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Currency = apps.get_model("finance", "Currency")
    Category = apps.get_model("finance", "Category")
    for org in Organization.objects.all():
        if not Currency.objects.filter(organization=org).exists():
            Currency.objects.create(organization=org, is_default=True, rate=1, **preset(org.currency))
        if not Category.objects.filter(organization=org).exists():
            Category.objects.bulk_create(
                Category(organization=org, kind=kind, name_en=en, name_ar=ar, color=color, order=i)
                for i, (kind, en, ar, color) in enumerate(CATEGORIES)
            )


class Migration(migrations.Migration):
    dependencies = [("finance", "0001_initial"), ("organizations", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
