"""Which address serves what, when custom domains are set.

- admin host  → the platform admin (/platform); "/" opens it.
- owner host  → the business dashboard (/app) and callers' order pages (/o/...); "/" opens the dashboard.
- any other host (the default Azure address) keeps working for the OpenAI webhook and health
  checks, and sends people opening admin pages to the right domain.
Only GET/HEAD requests are redirected, so a form post is never turned into a broken redirect.
"""

from app.config import Settings

OWNER_PATHS = ("/app", "/o/")
ADMIN_PATHS = ("/platform",)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p if p.endswith("/") else p + "/") for p in prefixes)


def redirect_for(settings: Settings, host: str, method: str, path: str, query: str = "") -> str | None:
    owner, admin = settings.owner_host.lower(), settings.admin_host.lower()
    if not (owner or admin) or method not in ("GET", "HEAD"):
        return None
    host = host.split(":")[0].lower()
    suffix = f"?{query}" if query else ""
    if host == admin:
        if path == "/":
            return "/platform"
        if owner and _under(path, OWNER_PATHS):
            return f"https://{owner}{path}{suffix}"
    elif host == owner:
        if path == "/":
            return "/app"
        if admin and _under(path, ADMIN_PATHS):
            return f"https://{admin}{path}{suffix}"
    else:
        if owner and _under(path, OWNER_PATHS):
            return f"https://{owner}{path}{suffix}"
        if admin and _under(path, ADMIN_PATHS):
            return f"https://{admin}{path}{suffix}"
    return None
