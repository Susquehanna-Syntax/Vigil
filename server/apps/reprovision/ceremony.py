"""Three-factor confirmation for a rebuild (docs/reprovisioning.md §4.4).

Password AND TOTP AND the typed hostname. The existing
require_totp_confirmation verifies only TOTP — the "password" in
definition_deploy's docstring is stale and is never read — so rebuild gets
its own gate with a real password check on top.
"""


def verify_rebuild_confirmation(user, payload, host) -> str | None:
    """Return an error string, or None when all three factors are satisfied.

    Order matters only for the message the operator sees; every factor is
    checked, and any one failing refuses the rebuild.
    """
    from apps.accounts.totp import require_totp_confirmation

    payload = payload or {}

    password = payload.get("password") or ""
    if not password or not user.check_password(password):
        return "Incorrect password"

    if err := require_totp_confirmation(user, payload):
        return err

    typed = payload.get("typed_hostname") or ""
    if typed != host.hostname:
        # Exact match on purpose: "close enough" is precisely what this
        # factor exists to prevent.
        return f"Typed hostname does not match {host.hostname}"

    return None
