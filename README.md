# Scout Dashboard — Backend (Django + DRF)

Multi-tenant API for scout organizations: organizations, users (username **or** email login, email optional),
units, roles + per-user permission grants, invitations, theme settings and an audit log.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # optional — defaults work for local dev (SQLite)
python manage.py migrate
python manage.py seed_demo      # demo org "1st Cedar Scout Group"
python manage.py runserver      # http://127.0.0.1:8000
```

Demo sign-ins (password `ScoutDemo!2026`): `rania` (owner/admin), `karim.h` (unit leader with extra grants),
`omar.t` (scout, no email). `seed_demo --reset` recreates the demo data.

Postgres instead of SQLite: `docker compose up -d db` and set
`DATABASE_URL=postgres://scout:scout@localhost:5432/scout` in `.env`.

API docs: `/api/docs/` (Swagger) · schema: `/api/schema/` · admin: `/admin/` (`python manage.py createsuperuser`).

## Checks

```bash
pytest              # 61 tests: auth, tenant isolation, permissions, invitations, settings
ruff check . && ruff format --check .
```

## How it fits together

| App | Responsibility |
| --- | --- |
| `apps/core` | `TenantModel` base, `X-Organization-ID` resolution (`tenancy.py`), pagination, error format |
| `apps/accounts` | Custom `User` (username required, email optional, both case-insensitive unique), username-or-email auth backend, JWT login, password reset, email verification |
| `apps/organizations` | `Organization`, `OrganizationSettings` (theme color overrides), `OrgMembership`, `Invitation`, sign-up, members API, dashboard summary, `seed_demo` |
| `apps/permissions` | Permission catalog (`catalog.py`), `Role`, `RoleAssignment`, `PermissionGrant`, `PermissionService`, DRF permission classes |
| `apps/units` | `Unit`, `UnitMembership` (a person can be in several units, each with a position) |
| `apps/audit` | `AuditLog` + `log_action()` |

### Multi-tenancy
Shared database, `organization` FK on tenant data. The client sends `X-Organization-ID`; it is honoured only for an
**active** membership (`apps/core/tenancy.py`). Users with exactly one membership may omit it. Viewsets use
`TenantScopedMixin`, so querysets are always filtered by the active organization.

### Permissions
- The catalog in `apps/permissions/catalog.py` is the source of truth (synced to the `Permission` table after migrate).
  Each permission is `unit_scoped` or organization-only.
- **Effective permissions** = role permissions × assignment scope ∪ direct grants (expired grants ignored).
  A role assigned for one unit contributes only its unit-scoped permissions, for that unit.
- Admins grant *any* permission to *any* member, org-wide or per unit, with optional expiry and note:
  `PUT /api/v1/users/{id}/permissions/` with `{"grants": [{"permission": "events.manage", "unit": 3}, ...]}`.
- Organization owners always have every permission.
- Views declare `permission_map = {action: codename}` and use `HasOrgPermission`.

### Main endpoints (`/api/v1/`)
`auth/login|refresh|logout|signup|password-reset|password-reset/confirm|verify-email`,
`auth/invitations/{token-or-code}/` (+ `accept/`), `me/` (+ `password/`, `organizations/`, `permissions/`),
`org/`, `org/settings/`, `dashboard/summary/`, `permissions/catalog/`, `users/` (+ `counts/`, `{id}/roles|units|permissions|set-password|invitation/`),
`units/`, `roles/`, `audit-logs/`.

Known gap: plain `APIView` endpoints (auth, me, org settings, dashboard) have no request/response annotations yet,
so drf-spectacular leaves them out of the generated schema.
