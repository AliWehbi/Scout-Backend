"""
Phase 6: finance is plain bookkeeping for the whole organization.

- replaces `finance.manage_payments` and `finance.manage_expenses` with one `finance.manage`,
- makes the finance permissions organization-wide (unit-limited grants become organization-wide),
- roles that had either old permission get `finance.manage` instead.
"""

from django.db import migrations

OLD = ("finance.manage_payments", "finance.manage_expenses")


def forwards(apps, schema_editor):
    Permission = apps.get_model("permissions", "Permission")
    Role = apps.get_model("permissions", "Role")
    PermissionGrant = apps.get_model("permissions", "PermissionGrant")
    view, _ = Permission.objects.update_or_create(
        codename="finance.view", defaults={"group": "finance", "unit_scoped": False}
    )
    manage, _ = Permission.objects.update_or_create(
        codename="finance.manage", defaults={"group": "finance", "unit_scoped": False}
    )
    for role in Role.objects.filter(permissions__codename__in=OLD).distinct():
        role.permissions.add(manage)
    for grant in PermissionGrant.objects.filter(permission__codename__in=OLD):
        grant.permission = manage
        grant.unit = None
        clash = PermissionGrant.objects.filter(
            membership=grant.membership, permission=manage, unit__isnull=True
        ).exclude(pk=grant.pk)
        if clash.exists():
            grant.delete()
        else:
            grant.save(update_fields=["permission", "unit"])
    for grant in PermissionGrant.objects.filter(permission=view, unit__isnull=False):
        if PermissionGrant.objects.filter(membership=grant.membership, permission=view, unit__isnull=True).exists():
            grant.delete()
        else:
            grant.unit = None
            grant.save(update_fields=["unit"])
    Permission.objects.filter(codename__in=OLD).delete()


class Migration(migrations.Migration):
    dependencies = [("permissions", "0002_shared_inventory")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
