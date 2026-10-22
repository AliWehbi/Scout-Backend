"""
Custom fields move here from the members app. The table (members_customfield) and its data stay
exactly where they are: this only moves the model in Django's records.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("organizations", "0001_initial"),
        ("members", "0004_member_left_date"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="CustomField",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        ("key", models.SlugField(max_length=60)),
                        ("label_en", models.CharField(max_length=120)),
                        ("label_ar", models.CharField(blank=True, max_length=120)),
                        ("field_type", models.CharField(choices=[("text", "Text"), ("number", "Number"), ("date", "Date"), ("yesno", "Yes / No"), ("choice", "Choice"), ("multi", "Multiple choice")], default="text", max_length=10)),
                        ("options", models.JSONField(blank=True, default=list)),
                        ("applies_to", models.CharField(choices=[("youth", "Youth members"), ("adult", "Adults"), ("all", "Everyone")], default="all", max_length=10)),
                        ("section", models.CharField(choices=[("additional", "Additional information"), ("medical", "Medical & emergency")], default="additional", max_length=12)),
                        ("required", models.BooleanField(default=False)),
                        ("visible_to_parents", models.BooleanField(default=True)),
                        ("editable_by_parents", models.BooleanField(default=False)),
                        ("order", models.PositiveIntegerField(default=0)),
                        ("is_active", models.BooleanField(default=True)),
                        ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="organizations.organization")),
                    ],
                    options={
                        "db_table": "members_customfield",
                        "ordering": ["order", "id"],
                        "constraints": [models.UniqueConstraint(fields=("organization", "key"), name="custom_field_org_key_unique")],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
