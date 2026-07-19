from os import environ as env
from dotenv import load_dotenv

load_dotenv()

class Telegram:
    API_ID = int(env.get("API_ID"))
    API_HASH = str(env.get("API_HASH"))
    BOT_TOKEN = str(env.get("BOT_TOKEN"))
    OWNER_ID = int(env.get('OWNER_ID', '7978482443'))
    WORKERS = int(env.get("WORKERS", "6"))  # 6 workers = 6 commands at once

    # ---------------[ UNLIMITED MULTI-MONGODB SUPPORT ]--------------- #
    # Set DATABASE_URL for the 1st cluster, then DATABASE_URL_2, DATABASE_URL_3,
    # DATABASE_URL_4 ... for as many extra clusters as you want. There is no
    # upper limit - the bot scans env vars until it stops finding new ones and
    # auto-rotates to the next DB once the current one fills up.
    DATABASE_URLS = []
    _primary_db = env.get('DATABASE_URL')
    if _primary_db:
        DATABASE_URLS.append(str(_primary_db))
    _i = 2
    while env.get(f"DATABASE_URL_{_i}"):
        DATABASE_URLS.append(str(env.get(f"DATABASE_URL_{_i}")))
        _i += 1
    # Backward-compat alias, kept so nothing else breaks if referenced
    DATABASE_URL = DATABASE_URLS[0] if DATABASE_URLS else str(env.get('DATABASE_URL'))
    DATABASE_NAME = str(env.get('DATABASE_NAME', env.get('SESSION_NAME', 'FileStream')))
    # MongoDB Atlas free tier = 512MB. Default leaves safety headroom before rollover.
    DB_MAX_SIZE_MB = int(env.get("DB_MAX_SIZE_MB", "460"))
    # Shared secret so only your Laravel admin panel can call the internal
    # /api/unlinked-files and /api/link-file endpoints.
    API_SECRET = str(env.get("API_SECRET", ""))

    UPDATES_CHANNEL = str(env.get('UPDATES_CHANNEL', "Telegram"))
    SESSION_NAME = str(env.get('SESSION_NAME', 'FileStream'))
    FORCE_SUB_ID = env.get('FORCE_SUB_ID', None)
    FORCE_SUB = env.get('FORCE_UPDATES_CHANNEL', False)
    FORCE_SUB = True if str(FORCE_SUB).lower() == "true" else False
    SLEEP_THRESHOLD = int(env.get("SLEEP_THRESHOLD", "60"))
    FILE_PIC = env.get('FILE_PIC', "https://graph.org/file/5bb9935be0229adf98b73.jpg")
    START_PIC = env.get('START_PIC', "https://graph.org/file/290af25276fa34fa8f0aa.jpg")
    VERIFY_PIC = env.get('VERIFY_PIC', "https://graph.org/file/736e21cc0efa4d8c2a0e4.jpg")
    def _parse_channel_id(name):
        val = env.get(name)
        if not val:
            raise ValueError(
                f"{name} is not set. Add it to your env vars with your log channel's "
                f"numeric ID (e.g. -1001234567890) - forward any message from that "
                f"channel to @userinfobot to get the correct ID, and make sure this "
                f"bot is an admin of that channel."
            )
        try:
            return int(val)
        except ValueError:
            raise ValueError(
                f"{name} must be a numeric channel ID like -1001234567890, got: {val!r}"
            )

    MULTI_CLIENT = False
    FLOG_CHANNEL = _parse_channel_id("FLOG_CHANNEL")   # Logs channel for file logs
    ULOG_CHANNEL = _parse_channel_id("ULOG_CHANNEL")   # Logs channel for user logs
    MODE = env.get("MODE", "primary")
    SECONDARY = True if MODE.lower() == "secondary" else False
    AUTH_USERS = list(set(int(x) for x in str(env.get("AUTH_USERS", "")).split()))


def _detect_fqdn():
    """
    Auto-detect the public hostname the bot is reachable on, so FQDN doesn't
    have to be set by hand on every redeploy.

    Priority:
      1. Explicit FQDN env var (always wins if set).
      2. Well-known hosting-platform env vars (Render, Railway, Koyeb, Fly.io,
         Heroku, GitHub Codespaces) - these already expose the public domain.
      3. Bare VPS fallback: ask a public "what's my IP" service.
      4. BIND_ADDRESS, as a last resort.

    Returns (hostname, is_platform_domain) - is_platform_domain is used to pick
    sane HAS_SSL / NO_PORT defaults below.
    """
    manual = env.get("FQDN")
    if manual:
        return manual, True

    port = env.get("PORT", "8080")
    codespace = env.get("CODESPACE_NAME")
    platform_candidates = (
        env.get("RENDER_EXTERNAL_HOSTNAME"),
        env.get("KOYEB_PUBLIC_DOMAIN"),
        env.get("RAILWAY_PUBLIC_DOMAIN"),
        (env.get("FLY_APP_NAME") + ".fly.dev") if env.get("FLY_APP_NAME") else None,
        (env.get("HEROKU_APP_NAME") + ".herokuapp.com") if env.get("HEROKU_APP_NAME") else None,
        (f"{codespace}-{port}.app.github.dev") if codespace else None,
    )
    for host in platform_candidates:
        if host:
            return host, True

    # Bare VPS / custom server: detect the machine's public IP automatically.
    try:
        import requests
        resp = requests.get("https://api.ipify.org", timeout=5)
        if resp.ok and resp.text.strip():
            return resp.text.strip(), False
    except Exception:
        pass

    return str(env.get("BIND_ADDRESS", "0.0.0.0")), False


class Server:
    PORT = int(env.get("PORT", 8080))
    BIND_ADDRESS = str(env.get("BIND_ADDRESS", "0.0.0.0"))
    PING_INTERVAL = int(env.get("PING_INTERVAL", "1200"))

    FQDN, _IS_PLATFORM_DOMAIN = _detect_fqdn()

    # Hosting platforms (Render/Railway/Koyeb/Fly/Heroku/Codespaces) terminate
    # SSL and proxy port 443 -> our internal PORT for us, so default to
    # https + no-port there. A bare VPS/IP gets plain http + the real port
    # unless the person overrides it below.
    _has_ssl_env = env.get("HAS_SSL")
    HAS_SSL = (
        str(_has_ssl_env).lower() in ("1", "true", "t", "yes", "y")
        if _has_ssl_env is not None else _IS_PLATFORM_DOMAIN
    )
    _no_port_env = env.get("NO_PORT")
    NO_PORT = (
        str(_no_port_env).lower() in ("1", "true", "t", "yes", "y")
        if _no_port_env is not None else _IS_PLATFORM_DOMAIN
    )

    URL = "http{}://{}{}/".format(
        "s" if HAS_SSL else "", FQDN, "" if NO_PORT else ":" + str(PORT)
    )



