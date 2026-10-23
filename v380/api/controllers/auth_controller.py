"""Auth controller — same functions the HTTP handler already uses."""

from v380.api.auth.service import (  # noqa: F401
    auth_user,
    check_user,
    create_user,
    delete_user,
    ensure_admin,
    list_users,
    new_token,
    update_profile,
)
