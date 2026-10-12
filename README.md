# Scout Dashboard — Backend (Django + DRF)

Multi-tenant API for scout organizations: organizations, users (username **or** email login, email optional),
units and patrols, members (people with an optional login), guardians, medical info, custom fields, parent change
requests, events (types, invitations, replies, parent signatures, iCal feed), attendance, roles + per-user permission grants, invitations, theme settings and an audit log.

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
`maya.s` (parent of Jad and Yara, has a pending change request). `seed_demo --reset` recreates the demo data.

Postgres instead of SQLite: `docker compose up -d db` and set
`DATABASE_URL=postgres://scout:scout@localhost:5432/scout` in `.env`.

API docs: `/api/docs/` (Swagger) · schema: `/api/schema/` · admin: `/admin/` (`python manage.py createsuperuser`).

## Checks

```bash
pytest              # 104 tests: auth, tenant isolation, permissions, invitations, settings, members, attendance, events, inventory
ruff check . && ruff format --check .
```

## How it fits together

| App | Responsibility |
| --- | --- |
| `apps/core` | `TenantModel` base, `X-Organization-ID` resolution (`tenancy.py`), pagination, error format |
| `apps/accounts` | Custom `User` (username required, email optional, both case-insensitive unique), username-or-email auth backend, JWT login, password reset, email verification |
| `apps/organizations` | `Organization`, `OrganizationSettings` (theme color overrides), `OrgMembership`, `Invitation`, sign-up, members API, dashboard summary, `seed_demo` |
| `apps/permissions` | Permission catalog (`catalog.py`), `Role`, `RoleAssignment`, `PermissionGrant`, `PermissionService`, DRF permission classes |
| `apps/units` | `Unit`, `Patrol`, `UnitMembership` (a member can be in several units, each with a position and patrol) |
| `apps/members` | `Member` (person, optional login), `Guardian` + `GuardianLink`, `MedicalInfo`, `Certification`, `CustomField`, `ChangeRequest`; import (`importer.py`), parent portal views |
| `apps/attendance` | `Session` (a standalone session of a unit, or one day of an event), `AttendanceRecord` (the note is the reason for an excused absence), roster, trend and report endpoints |
| `apps/events` | `EventType`, `Event`, `EventInvitation` (reply + waitlist), `ConsentSignature`, `CalendarFeed`; rules in `services.py` |
| `apps/inventory` | `Item` (tracked / counted / consumable), `Piece`, `Category`, `Location`, `ExternalBorrower`, `Loan` + `LoanLine` (who/when out and in), `Reservation` (event gear), `ItemLog` (history), `StockTake`; rules in `services.py` |
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

### Members, logins and guardians
- A `Member` is a person. Linking a login is optional (`Member.membership`). Roles and grants belong to the login
  (`OrgMembership`); unit memberships belong to the member.
- A `Guardian` is a contact for one or more members and may also have a login (the parent portal).
- Member-related permissions are unit-scoped: a leader only sees members of units where they hold `members.view`.
  Medical info needs `members.view_medical`, and each read is logged as `person.medical_viewed`.
- Parents never write directly. `POST my/family/children/{id}/` creates a `ChangeRequest` with one item per field.
  A leader with `members.edit` approves all items, some items (`partial`) or none, and only approved items are applied.
- Migration `members.0002` converts Phase 1 data: logins with only the parent role become guardians, and everyone
  else becomes a member with their unit memberships moved over.

### Main endpoints (`/api/v1/`)
`auth/login|refresh|logout|signup|password-reset|password-reset/confirm|verify-email`,
`auth/invitations/{token-or-code}/` (+ `accept/`), `me/` (+ `password/`, `organizations/`, `permissions/`),
`org/`, `org/settings/`, `dashboard/summary/`, `permissions/catalog/`, `users/` (+ `counts/`, `{id}/roles|units|permissions|set-password|invitation/`),
`units/`, `patrols/`, `roles/`, `audit-logs/`,
`members/` (+ `counts/`, `export/`, `bulk/`, `import/preview|commit|sample/`, `{id}/units|guardians|medical|certifications|attendance|give-access/`),
`custom-fields/` (+ `reorder/`), `change-requests/` (+ `counts/`, `{id}/approve|reject/`),
`attendance/sessions/` (+ `trend/`, `{id}/records/`), `attendance/report/`, `my/family/` (+ `children/`, `children/{id}/`, `requests/`),
`event-types/`, `events/` (+ `{id}/publish|cancel|invite|invitations|attendance/`, `{id}/invitations/{inv}/respond/`),
`my/events/` (+ `invitations/{id}/respond|sign/`), `my/calendar/`, `calendar/{token}.ics` (public, the token is the credential),
`inventory/items/` (+ `{id}/history|pieces|pieces/{p}/condition|stock|availability/`), `inventory/loans/` (+ `{id}/return/`),
`inventory/categories/`, `inventory/locations/`, `inventory/external-borrowers/`, `inventory/stocktakes/` (+ `{id}/finish/`),
`inventory/summary/`, `inventory/lookup/`, `inventory/borrowers/`, `events/{id}/gear/` (+ `check-out/`), `my/gear/`.

### Inventory
- `inventory.view`, `inventory.check_out` and `inventory.manage` are organization-wide (`SHARED_STORE_PERMISSIONS` in
  the catalog): they apply everywhere even when they come from a role assigned for one unit or a unit-limited grant.
- Only physically impossible things are refused (lending a piece that is already out, more than is in the store, or a
  lost/retired piece). Damaged pieces and booking conflicts return `warnings` instead.
- Foreign keys from loans and history use `RESTRICT`, so items, members and events with history can't be deleted on
  their own, but deleting a whole organization still works.

### Events
- Seeing events needs `events.view`; creating and editing needs `events.manage` for **every** unit of the event
  (an event with no units is organization-wide and needs it organization-wide); check-in needs `events.check_in`
  for one of its units.
- Families only see published events they are invited to. Parents see a child's invitation when it was sent to
  parents too, which is forced on for events that need a signature.
- Attendance is taken once per event day: the first check-in of a day creates a `Session` linked to the event, so
  reports treat event days and standalone sessions the same way.
- `members_for_invite` includes everyone in the chosen units, leaders too.

Known gap: plain `APIView` endpoints (auth, me, org settings, dashboard) have no request/response annotations yet,
so drf-spectacular leaves them out of the generated schema.
