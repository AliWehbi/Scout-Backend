from django.db import migrations, models


def split_names(apps, schema_editor):
    Member = apps.get_model("members", "Member")
    for member in Member.objects.exclude(name_ar=""):
        first, _sep, last = member.name_ar.strip().partition(" ")
        member.first_name_ar = first
        member.last_name_ar = last.strip()
        member.save(update_fields=["first_name_ar", "last_name_ar"])


def join_names(apps, schema_editor):
    Member = apps.get_model("members", "Member")
    for member in Member.objects.all():
        member.name_ar = f"{member.first_name_ar} {member.last_name_ar}".strip()
        member.save(update_fields=["name_ar"])


class Migration(migrations.Migration):
    dependencies = [("members", "0002_members_from_logins")]

    operations = [
        migrations.AddField(
            model_name="member", name="first_name_ar", field=models.CharField(blank=True, max_length=150)
        ),
        migrations.AddField(
            model_name="member", name="last_name_ar", field=models.CharField(blank=True, max_length=150)
        ),
        migrations.RunPython(split_names, join_names),
        migrations.RemoveField(model_name="member", name="name_ar"),
    ]
