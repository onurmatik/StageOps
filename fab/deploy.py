from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

import yaml
from fabric import Connection, task
from invoke import Collection

# ==================================================
# PATHS
# ==================================================

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = BASE_DIR / "app.yaml"
TEMPLATES_DIR = BASE_DIR / "templates"
SYSTEMD_DIR = TEMPLATES_DIR / "systemd"
SCRIPTS_DIR = BASE_DIR / "scripts"

# ==================================================
# HELPERS
# ==================================================


def debug(msg: str) -> None:
    print(f"[stageops] {msg}")


def load_yaml_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config file not found: {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    if not isinstance(data, dict):
        raise RuntimeError("Config must be a mapping at the top level.")
    return data


def normalize_apps(raw: object) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        apps: dict[str, dict] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise RuntimeError("Each app entry must be a mapping.")
            name = item.get("name") or item.get("project_name")
            if not name:
                raise RuntimeError("Each app entry must have a name.")
            apps[name] = item
        return apps
    raise RuntimeError("apps must be a mapping or a list of mappings.")

def require_mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a mapping.")
    return value


def require_keys(label: str, mapping: dict, keys: list[str]) -> None:
    missing = []
    for key in keys:
        if key not in mapping:
            missing.append(key)
            continue
        if mapping[key] is None:
            missing.append(key)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"{label} missing required keys: {joined}")


def validate_app(name: str, app: dict) -> dict:
    app_name = app.get("project_name") or app.get("name") or name
    app["project_name"] = app_name

    require_keys(
        f"apps.{name}",
        app,
        [
            "domain",
            "tier",
            "enable_node",
            "enable_celery",
            "backend_paths",
            "gunicorn_worker_class",
            "gunicorn_workers",
            "gunicorn_threads",
            "gunicorn_timeout",
            "gunicorn_graceful_timeout",
            "gunicorn_max_requests",
            "gunicorn_max_requests_jitter",
            "memory_limit",
        ],
    )

    if as_bool(app.get("enable_node")):
        require_keys(
            f"apps.{name}",
            app,
            ["node_dir", "node_port", "node_start_cmd"],
        )

    if as_bool(app.get("enable_celery")):
        require_keys(
            f"apps.{name}",
            app,
            ["celery_queue"],
        )

    if "cron" in app:
        app["cron"] = parse_cron_entries(app.get("cron"))

    return app


def load_all_configs(only: list[str] | None = None) -> tuple[dict, list[dict]]:
    config = load_yaml_config()
    server = require_mapping(config.get("server"), "server")
    require_keys("server", server, ["host", "user", "ssh_key", "log_access", "log_errors"])
    defaults = server.get("defaults") or {}
    if defaults:
        defaults = require_mapping(defaults, "server.defaults")

    raw_apps = normalize_apps(config.get("apps"))
    if not raw_apps:
        raise RuntimeError(f"No apps found in {CONFIG_PATH}")

    if only:
        missing = [name for name in only if name not in raw_apps]
        if missing:
            raise RuntimeError(f"Unknown apps in --only: {', '.join(missing)}")
        app_items = [(name, raw_apps[name]) for name in only]
    else:
        app_items = list(raw_apps.items())

    apps: list[dict] = []
    for name, raw in app_items:
        app = require_mapping(raw, f"apps.{name}")
        if defaults:
            apply_defaults(app, defaults)
        apps.append(validate_app(name, app))

    return server, apps


def as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def parse_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    raise RuntimeError("Expected a list or comma-separated string.")


def parse_backend_paths(raw: object) -> list[str]:
    paths = parse_list(raw)
    return [p.rstrip("/") for p in paths]


def apply_defaults(app: dict, defaults: dict) -> dict:
    for key, value in defaults.items():
        if key not in app or app[key] is None:
            app[key] = value
    return app


def parse_cron_entries(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    raise RuntimeError("cron must be a list of cron strings.")


def render_template(text: str, ctx: dict) -> str:
    for k, v in ctx.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text


def upload_text(c: Connection, text: str, dst: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(text)
        tmp_path = tmp.name

    remote_tmp = f"/tmp/{Path(dst).name}"

    try:
        c.put(tmp_path, remote_tmp)
        c.sudo(f"mv {remote_tmp} {dst}")
    finally:
        os.remove(tmp_path)


def upload_template(c: Connection, src: Path, dst: str, ctx: dict) -> None:
    rendered = render_template(src.read_text(), ctx)
    upload_text(c, rendered, dst)


def purge_systemd_instance_units(
    c: Connection, project: str, units: list[str]
) -> None:
    for unit in units:
        instance = unit.replace("@.", f"@{project}.")
        instance_path = f"/etc/systemd/system/{instance}"
        instance_dropin = f"/etc/systemd/system/{instance}.d"
        c.sudo(f"rm -f {instance_path}", warn=True)
        c.sudo(f"rm -rf {instance_dropin}", warn=True)


def disable_systemd_instances(c: Connection, project: str) -> None:
    for unit in ["app", "node", "celery"]:
        c.sudo(f"systemctl disable --now {unit}@{project}.service", warn=True)
    c.sudo(f"systemctl disable --now app@{project}.socket", warn=True)


def resolve_node_dir(raw: str | None, project_dir: str) -> str:
    if not raw:
        return project_dir
    raw = str(raw).strip()
    if raw.startswith(("'", '"')) and raw.endswith(raw[0]) and len(raw) >= 2:
        raw = raw[1:-1]
    if raw.startswith("/"):
        return raw
    return f"{project_dir}/{raw}"


def split_cron_entry(entry: str) -> tuple[str, str]:
    text = entry.strip()
    if not text:
        raise RuntimeError("cron entry cannot be empty.")
    if text.startswith("#"):
        raise RuntimeError("cron entry cannot be a comment.")
    if text.startswith("@"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError(f"Invalid cron entry: {entry}")
        return parts[0], parts[1]
    parts = text.split()
    if len(parts) < 6:
        raise RuntimeError(f"Invalid cron entry: {entry}")
    schedule = " ".join(parts[:5])
    command = " ".join(parts[5:])
    return schedule, command


def normalize_cron_command(command: str, project_dir: str, project_name: str) -> str:
    project_env_path = f"{project_dir}/venv/bin"
    ctx = {
        "PROJECT_PATH": project_dir,
        "PROJECT_ENV_PATH": project_env_path,
        "PROJECT_NAME": project_name,
    }

    if "{PROJECT_" in command:
        return render_template(command, ctx).strip()

    trimmed = command.strip()
    if not trimmed:
        raise RuntimeError("cron command cannot be empty.")

    raw_prefixes = (
        "/",
        "./",
        "bash ",
        "sh ",
        "python ",
        "pip ",
        "manage.py ",
        "django-admin ",
        "celery ",
        "gunicorn ",
    )
    if trimmed.startswith(raw_prefixes) or trimmed.startswith("$"):
        return trimmed

    return f"{project_env_path}/python manage.py {trimmed}"


def cron_bash_command(project_dir: str, command: str) -> str:
    env_file = f"{project_dir}/.env"
    snippet = (
        "set -a; "
        f'if [ -f "{env_file}" ]; then . "{env_file}"; fi; '
        "set +a; "
        f'cd "{project_dir}"; '
        f"{command}"
    )
    return f"/bin/bash -lc {shlex.quote(snippet)}"


def build_cron_lines(
    *,
    entries: list[str],
    user: str,
    project_dir: str,
    project_name: str,
) -> list[str]:
    lines = [
        f"# StageOps cron for {project_name}",
        "SHELL=/bin/bash",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    ]

    for entry in entries:
        if entry.strip().startswith("#"):
            continue
        schedule, command = split_cron_entry(entry)
        normalized = normalize_cron_command(command, project_dir, project_name)
        bash_cmd = cron_bash_command(project_dir, normalized)
        lines.append(f"{schedule} {user} {bash_cmd}")

    return lines


def install_cron(c: Connection, project_name: str, lines: list[str]) -> None:
    cron_path = f"/etc/cron.d/stageops-{project_name}"
    if not lines or len(lines) <= 3:
        c.sudo(f"rm -f {cron_path}", warn=True)
        return
    content = "\n".join(lines) + "\n"
    upload_text(c, content, cron_path)
    c.sudo(f"chmod 644 {cron_path}")
    c.sudo(f"chown root:root {cron_path}")


def render_project_template(value: str, project_name: str) -> str:
    return value.replace("{PROJECT_NAME}", project_name)


def setup_app(c: Connection, server: dict, app: dict) -> None:
    PROJECT_NAME = app["project_name"]
    DOMAIN = app["domain"]

    USER = server["user"]
    LOG_ACCESS = render_project_template(server["log_access"], PROJECT_NAME)
    LOG_ERRORS = render_project_template(server["log_errors"], PROJECT_NAME)

    TIER = app["tier"]

    ENABLE_NODE = as_bool(app["enable_node"])
    NODE_PORT = int(app["node_port"]) if ENABLE_NODE else None
    NODE_DIR = app.get("node_dir")
    NODE_START_CMD = app.get("node_start_cmd")

    ENABLE_CELERY = as_bool(app["enable_celery"])
    CELERY_QUEUE = app["celery_queue"] if ENABLE_CELERY else None

    BACKEND_PATHS = parse_backend_paths(app["backend_paths"])
    LEGACY_PROJECT_NAMES = parse_list(app.get("legacy_projects"))
    CRON_ENTRIES = parse_cron_entries(app.get("cron"))

    GUNICORN_WORKER_CLASS = app["gunicorn_worker_class"]
    GUNICORN_WORKERS = int(app["gunicorn_workers"])
    GUNICORN_THREADS = int(app["gunicorn_threads"])
    GUNICORN_TIMEOUT = int(app["gunicorn_timeout"])
    GUNICORN_GRACEFUL_TIMEOUT = int(app["gunicorn_graceful_timeout"])
    GUNICORN_MAX_REQUESTS = int(app["gunicorn_max_requests"])
    GUNICORN_MAX_REQUESTS_JITTER = int(app["gunicorn_max_requests_jitter"])

    MEMORY_LIMIT = app.get("memory_limit")
    CPU_QUOTA = app.get("cpu_quota")

    # --------------------------------------------------
    # SERVER PATHS
    # --------------------------------------------------

    PROJECT_DIR = f"/srv/apps/{PROJECT_NAME}"
    LOG_DIR = f"/var/log/{PROJECT_NAME}"
    RUN_DIR = f"/run/{PROJECT_NAME}"

    GUNICORN_SOCKET = f"{RUN_DIR}/gunicorn.sock"

    # --------------------------------------------------
    # DIRECTORIES
    # --------------------------------------------------

    c.sudo(f"mkdir -p {PROJECT_DIR} {LOG_DIR} {RUN_DIR}")
    c.sudo(f"chown -R {USER}:{USER} {PROJECT_DIR} {LOG_DIR}")
    c.sudo(f"chown {USER}:www-data {RUN_DIR}")
    c.sudo(f"chmod 2775 {RUN_DIR}")

    # --------------------------------------------------
    # CRON
    # --------------------------------------------------

    cron_lines = build_cron_lines(
        entries=CRON_ENTRIES,
        user=USER,
        project_dir=PROJECT_DIR,
        project_name=PROJECT_NAME,
    )
    install_cron(c, PROJECT_NAME, cron_lines)

    # --------------------------------------------------
    # SYSTEMD UNITS
    # --------------------------------------------------

    debug("Installing systemd templates")

    systemd_units = ["app@.service", "app@.socket", "node@.service", "celery@.service"]

    debug("Removing stale systemd instance overrides")
    purge_systemd_instance_units(c, PROJECT_NAME, systemd_units)

    if LEGACY_PROJECT_NAMES:
        debug(f"Removing legacy systemd instances: {', '.join(LEGACY_PROJECT_NAMES)}")
        for legacy in LEGACY_PROJECT_NAMES:
            disable_systemd_instances(c, legacy)
            purge_systemd_instance_units(c, legacy, systemd_units)
            c.sudo(f"rm -rf /run/{legacy}", warn=True)

    for unit in systemd_units:
        c.put(SYSTEMD_DIR / unit, f"/tmp/{unit}")
        c.sudo(f"mv /tmp/{unit} /etc/systemd/system/{unit}")

    # Per-app systemd overrides

    gunicorn_exec = (
        "/bin/sh -lc "
        f"\"/srv/apps/%i/venv/bin/gunicorn %i.wsgi:application "
        f"--worker-class {GUNICORN_WORKER_CLASS} "
        f"--workers {GUNICORN_WORKERS} "
        f"--threads {GUNICORN_THREADS} "
        f"--timeout {GUNICORN_TIMEOUT} "
        f"--graceful-timeout {GUNICORN_GRACEFUL_TIMEOUT} "
        f"--max-requests {GUNICORN_MAX_REQUESTS} "
        f"--max-requests-jitter {GUNICORN_MAX_REQUESTS_JITTER} "
        f"--bind unix:/run/%i/gunicorn.sock "
        f"--umask 007 "
        f"--user ubuntu "
        f"--group www-data "
        f"--access-logfile {LOG_ACCESS} "
        f"--error-logfile {LOG_ERRORS}\""
    )

    app_dropin = build_dropin(
        directives={
            "MemoryMax": MEMORY_LIMIT,
            "CPUQuota": CPU_QUOTA,
        },
        exec_start=gunicorn_exec,
    )
    write_dropin(c, f"app@{PROJECT_NAME}.service", app_dropin)

    if ENABLE_NODE:
        node_dir = resolve_node_dir(NODE_DIR, PROJECT_DIR)
        node_dropin = build_dropin(
            env={
                "NODE_PORT": NODE_PORT,
                "NODE_DIR": node_dir,
                "NODE_START_CMD": NODE_START_CMD,
            },
            directives={
                "MemoryMax": MEMORY_LIMIT,
                "CPUQuota": CPU_QUOTA,
            },
        )
        write_dropin(c, f"node@{PROJECT_NAME}.service", node_dropin)
    else:
        c.sudo(f"systemctl disable --now node@{PROJECT_NAME}.service", warn=True)

    if ENABLE_CELERY:
        celery_dropin = build_dropin(
            env={
                "CELERY_QUEUE": CELERY_QUEUE,
            },
            directives={
                "MemoryMax": MEMORY_LIMIT,
                "CPUQuota": CPU_QUOTA,
            },
        )
        write_dropin(c, f"celery@{PROJECT_NAME}.service", celery_dropin)
    else:
        c.sudo(f"systemctl disable --now celery@{PROJECT_NAME}.service", warn=True)

    # --------------------------------------------------
    # NGINX
    # --------------------------------------------------

    backend_locations = []
    for path in BACKEND_PATHS:
        backend_locations.append(
            f"""location ^~ {path}/ {{
            proxy_pass http://unix:{GUNICORN_SOCKET}:;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}"""
        )

    BACKEND_LOCATIONS = "\n\n".join(backend_locations)

    if ENABLE_NODE:
        FRONTEND_UPSTREAM = f"http://127.0.0.1:{NODE_PORT}"
    else:
        FRONTEND_UPSTREAM = f"http://unix:{GUNICORN_SOCKET}:"

    nginx_ctx = {
        "PROJECT_NAME": PROJECT_NAME,
        "DOMAIN": DOMAIN,
        "PROJECT_DIR": PROJECT_DIR,
        "GUNICORN_SOCKET": GUNICORN_SOCKET,
        "BACKEND_LOCATIONS": BACKEND_LOCATIONS,
        "FRONTEND_UPSTREAM": FRONTEND_UPSTREAM,
    }

    upload_template(
        c,
        TEMPLATES_DIR / "nginx" / "django.conf.j2",
        f"/etc/nginx/sites-available/{PROJECT_NAME}.conf",
        nginx_ctx,
    )

    if LEGACY_PROJECT_NAMES:
        debug(f"Removing legacy nginx sites: {', '.join(LEGACY_PROJECT_NAMES)}")
        for legacy in LEGACY_PROJECT_NAMES:
            c.sudo(f"rm -f /etc/nginx/sites-enabled/{legacy}.conf", warn=True)
            c.sudo(f"rm -f /etc/nginx/sites-available/{legacy}.conf", warn=True)

    c.sudo(
        f"ln -sf /etc/nginx/sites-available/{PROJECT_NAME}.conf "
        f"/etc/nginx/sites-enabled/{PROJECT_NAME}.conf"
    )

    # --------------------------------------------------
    # ENABLE SERVICES
    # --------------------------------------------------

    c.sudo("systemctl daemon-reload")

    if TIER == "hot":
        c.sudo(f"systemctl enable --now app@{PROJECT_NAME}.service")
        c.sudo(f"systemctl disable --now app@{PROJECT_NAME}.socket", warn=True)
    else:
        c.sudo(f"systemctl enable --now app@{PROJECT_NAME}.socket")

    if ENABLE_NODE:
        c.sudo(f"systemctl enable --now node@{PROJECT_NAME}.service")

    if ENABLE_CELERY:
        c.sudo(f"systemctl enable --now celery@{PROJECT_NAME}.service")

    c.sudo("nginx -t")
    c.sudo("systemctl reload nginx")

    debug("Restarting services")
    if TIER == "hot":
        c.sudo(f"systemctl restart app@{PROJECT_NAME}.service")
    else:
        c.sudo(f"systemctl restart app@{PROJECT_NAME}.socket")
        c.sudo(f"systemctl try-restart app@{PROJECT_NAME}.service")

    if ENABLE_NODE:
        c.sudo(f"systemctl restart node@{PROJECT_NAME}.service")

    if ENABLE_CELERY:
        c.sudo(f"systemctl restart celery@{PROJECT_NAME}.service")


def systemd_quote(value: object) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', "\\\"")
    return f'"{text}"'


def systemd_env_lines(env: dict[str, object]) -> list[str]:
    lines = []
    for key, value in env.items():
        if value is None:
            continue
        lines.append(f"Environment={systemd_quote(f'{key}={value}')}")
    return lines


def build_dropin(
    *,
    env: dict[str, object] | None = None,
    directives: dict[str, object] | None = None,
    exec_start: str | None = None,
) -> str:
    lines = ["[Service]"]

    if env:
        lines.extend(systemd_env_lines(env))

    if exec_start:
        lines.append("ExecStart=")
        lines.append(f"ExecStart={exec_start}")

    if directives:
        for key, value in directives.items():
            if value is None:
                continue
            lines.append(f"{key}={value}")

    return "\n".join(lines) + "\n"


def write_dropin(c: Connection, unit_name: str, content: str) -> None:
    dropin_dir = f"/etc/systemd/system/{unit_name}.d"
    dropin_path = f"{dropin_dir}/10-stageops.conf"
    c.sudo(f"mkdir -p {dropin_dir}")
    upload_text(c, content, dropin_path)


# ==================================================
# DEPLOY TASK
# ==================================================


@task
def infra(c, only=None):
    """
    Setup infra for all apps using StageOps.

    Usage:
        fab infra
        fab infra --only=mevzuat,newsradar
    """
    only_list = parse_list(only) if only else []
    server, apps = load_all_configs(only_list or None)

    HOST = server["host"]
    USER = server["user"]
    SSH_KEY = os.path.expanduser(server["ssh_key"])

    if only_list:
        debug(f"Setting up infra for {len(apps)} apps (filtered)")
    else:
        debug(f"Setting up infra for {len(apps)} apps")

    c = Connection(
        host=HOST,
        user=USER,
        connect_kwargs={"key_filename": SSH_KEY},
    )

    debug("Running host verification")
    c.put(SCRIPTS_DIR / "verify_host.sh", "/tmp/verify_host.sh")
    c.run("bash /tmp/verify_host.sh")

    for app in apps:
        debug(f"Setting up {app['project_name']}")
        setup_app(c, server, app)

    debug("Infra setup completed successfully")


ns = Collection(infra)
