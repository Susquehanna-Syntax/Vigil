# Roles and permissions

Vigil decides what each user can do by looking at their role and, on Business,
the site they are working in. This page explains the roles, how Vigil picks a
role for a user, and how to grant access to individual sites.

Per-site roles and the capability matrix are Business features. Free installs
have one site, so each user has one role for the whole instance.

## Roles

| Role | What it allows |
|---|---|
| **Owner** | Everything, in every site. Owner is the superuser flag on an account. |
| **Admin** | Everything in the sites the admin is granted. |
| **Operator** | Exactly the actions the capability matrix grants, for each site. |
| **Viewer** | Viewing everything in the sites the viewer is granted. Viewers cannot change anything. |
| **None** | No access to the site. |

Admin and Viewer are all or nothing inside their sites. Only Operator uses the
capability matrix.

## Which role applies to a user

Vigil checks these rules in order and stops at the first match:

1. If the user is a superuser, the role is Owner, in every site.
2. If the user has no site roles, Vigil uses the account's own role. Staff
   accounts are Admin. Other accounts use the role on their profile, or Viewer
   if none is set. This is the behavior of Vigil without per-site roles, so
   existing installs keep the same access.
3. If the user has a role for this site, Vigil uses that role.
4. If the user has a role for all sites (the global role), Vigil uses that role.
5. Otherwise the user has no access to the site.

For example, a global role of Viewer and a West Campus role of Operator give
the user Operator access at West Campus and Viewer access everywhere else.

### Granting a first site role narrows access

Once a user has any site role, rule 2 no longer applies to that user. Access
then comes only from the site roles you grant and the global role.

If a user had access to every site through their profile, granting them a role
for one site limits them to the sites you list. Vigil shows a warning before you
save the change.

## Owners and locked-out accounts

Vigil keeps at least one superuser. Two rules protect this:

- You cannot demote or delete the last remaining superuser.
- You cannot remove the Owner flag from your own account.

## What is per site

| Area | Per site? | Notes |
|---|---|---|
| Hosts | Yes | Each host belongs to one site. |
| Baselines | Yes | Each baseline is assigned to the sites that use it. |
| Automations | Yes | Each automation is assigned to the sites that use it. |
| Alerts | Yes | An alert belongs to its host's site. |
| Status pages | Yes | Each status page is published for one site. |
| Task library | No | Task definitions are shared by all sites. |
| Running a task | Yes | A run targets hosts, so Vigil checks the target host's site. |
| Accounts | No | One user list for the whole instance. |
| Settings | No | Instance-wide settings. |
| License | No | Instance-wide. |

Writing a task is the same everywhere. Running a task depends on the site of
each host it targets.

## Operator capabilities

An Operator has a set of capabilities for each site. Capabilities are fixed.
Vigil does not let you type a custom name.

| Area | Capabilities |
|---|---|
| `hosts` | `view`, `edit`, `approve`, `delete` |
| `tasks` | `view`, `run` |
| `baselines` | `view`, `run`, `edit` |
| `automations` | `view`, `edit`, `toggle` |
| `alerts` | `view`, `ack`, `silence` |
| `statuspages` | `view`, `edit` |

A capability that is not granted is denied.

Rebuilding a host with [reprovisioning](reprovisioning.md) needs the
`reprovision` capabilities `view` and `rebuild`. Only administrators can manage
the image library.

## Set up site roles

1. Open the user in the web interface.
2. Choose a role for each site, or a global role for all sites.
3. For an Operator, select the capabilities for each site.
4. Save the change and read the warning if it appears.

## Testing and safety

Vigil checks these cases in its test suite:

- Owner, Admin, Viewer, and None each get the access listed above.
- A user cannot see or change hosts, alerts, or other objects in a site they
  have no role for.
- Lists show only the rows in sites the user can see.
- When a license expires, existing site roles keep working. Granting a new
  site role needs a valid license.
