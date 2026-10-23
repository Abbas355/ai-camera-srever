"""Route catalog for the HTTP API (documentation + discovery).

The live dispatcher remains Handler in v380.api.http.server so behavior
stays identical. Use this list when adding partner / v1 endpoints later.
"""

ROUTES = {
    "GET": [
        "/",
        "/api/ping",
        "/api/me",
        "/api/users",
        "/api/cameras",
        "/api/cameras/{id}",
        "/api/cameras/{id}/ezviz",
        "/api/cameras/{id}/panorama",
        "/api/cameras/{id}/snapshot",
        "/api/cameras/{id}/ping",
        "/api/tables",
        "/api/index",
        "/api/storage",
        "/api/record/status",
        "/api/discover",
    ],
    "POST": [
        "/api/login",
        "/api/profile",
        "/api/users",
        "/api/cameras",
        "/api/cameras/{id}/command",
        "/api/cameras/{id}/control",
        "/api/cameras/{id}/panorama",
        "/api/cameras/{id}/ezviz/login",
        "/api/tables",
        "/api/tables/{id}/goto",
    ],
    "PUT": ["/api/cameras/{id}"],
    "DELETE": ["/api/cameras/{id}", "/api/tables/{id}", "/api/users/{username}"],
}
