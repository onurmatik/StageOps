from __future__ import annotations

import os
import sys
import shlex
import subprocess
import tempfile
from pathlib import Path

from fabric import Connection, task
from invoke import Collection
from dotenv import dotenv_values

# ==================================================
# PATHS
# ==================================================

BASE_DIR = Path(__file__).resolve().parent.parent

ENVS_DIR = BASE_DIR / "envs"
TEMPLATES_DIR = BASE_DIR / "templates"
SYSTEMD_DIR = TEMPLATES_DIR / "systemd"
SCRIPTS_DIR = BASE_DIR / "scripts"

# ==================================================
# DEFAULTS (stage-level)
# ==================================================

DEFAULTS = {
    "HOST": "18.206.25.249",
    "USER": "ubuntu",
    "SSH_KEY": "~/.ssh/stage-ec2-key.pem",
    "BRANCH": "main",
    "WORKERS": "1",
    "THREADS": "2",
    "MEMORY_LIMIT": "400M",
    "ENABLE_NODE": "0",
    "ENABLE_CELERY": "0",
    "TIER": "cold",
}

# ==================================================
# HELPERS
# ==================================================

def debug(msg: str) -> None:
    print(f"[stageops] {msg}")

def load_project_env(project: str) -> dict:
    env_path = ENVS_DIR / f"{project}.env"
    if not env_path.exists():
        raise RuntimeError(f"Env file not found: {env_path}")


    raw = dotenv_values(env_path)
    env = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}

    required = ["PROJECT_NAME", "DOMAIN", "REPO_URL"]
    for key in required:
        if not env.get(key):
            raise RuntimeError(f"{key} is required in {env_path}")

    return env


def parse_backend_paths(raw: str) -> list[str]:
    if not raw:
        return []
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


def parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def get_github_token() -> str | None:
    script = SCRIPTS_DIR / "get_github_app_token.py"
    if not script.exists():
        return None

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
    )

    token = result.stdout.strip()
    return token or None


def render_template(text: str, ctx: dict) -> str:
    for k, v in ctx.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text


def upload_template(c: Connection, src: Path, dst: str, ctx: dict) -> None:
    rendered = render_template(src.read_text(), ctx)

    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(rendered)
        tmp_path = tmp.name

    remote_tmp = f"/tmp/{src.name}"

    try:
        c.put(tmp_path, remote_tmp)
        c.sudo(f"mv {remote_tmp} {dst}")
    finally:
        os.remove(tmp_path)


def shell_env_prefix(env: dict) -> str:
    parts = []
    for key, value in env.items():
        if value is None:
            continue
        parts.append(f"{key}={shlex.quote(str(value))}")
    return " ".join(parts)


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
    raw = raw.strip()
    if raw.startswith(("'", '"')) and raw.endswith(raw[0]) and len(raw) >= 2:
        raw = raw[1:-1]
    if raw.startswith("/"):
        return raw
    return f"{project_dir}/{raw}"

def normalize_env_text(env_text: str, project_dir: str) -> str:
    lines = env_text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        key, sep, value = line.partition("=")
        if key.strip() != "NODE_DIR" or not sep:
            out.append(line)
            continue
        raw = value.strip()
        quote = ""
        if raw.startswith(("'", '"')) and raw.endswith(raw[0]) and len(raw) >= 2:
            quote = raw[0]
            raw = raw[1:-1]
        resolved = resolve_node_dir(raw, project_dir)
        new_val = f"{quote}{resolved}{quote}" if quote else resolved
        out.append(f"{key}{sep}{new_val}")
    return "\n".join(out) + ("\n" if env_text.endswith("\n") else "")


# ==================================================
# DEPLOY TASK
# ==================================================

@task
def deploy(c, project):
    """
    Deploy a project using StageOps.

    Usage:
        fab deploy:newsradar
    """
    env_path = ENVS_DIR / f"{project}.env"
    env = load_project_env(project)

    PROJECT_NAME = env["PROJECT_NAME"]
    DOMAIN = env["DOMAIN"]
    REPO_URL = env["REPO_URL"]
    BRANCH = env["BRANCH"]

    HOST = env["HOST"]
    USER = env["USER"]
    SSH_KEY = os.path.expanduser(env["SSH_KEY"])

    TIER = env["TIER"]

    ENABLE_NODE = env.get("ENABLE_NODE") in {"1", "true", "yes"}
    NODE_PORT = env.get("NODE_PORT", "3000")

    ENABLE_CELERY = env.get("ENABLE_CELERY") in {"1", "true", "yes"}
    CELERY_QUEUE = env.get("CELERY_QUEUE", PROJECT_NAME)

    BACKEND_PATHS = env.get("BACKEND_PATHS", "")
    LEGACY_PROJECT_NAMES = parse_csv_list(env.get("LEGACY_PROJECT_NAMES"))

    # --------------------------------------------------
    # SERVER PATHS
    # --------------------------------------------------

    PROJECT_DIR = f"/srv/apps/{PROJECT_NAME}"
    VENV_DIR = f"{PROJECT_DIR}/venv"
    LOG_DIR = f"/var/log/{PROJECT_NAME}"
    RUN_DIR = f"/run/{PROJECT_NAME}"

    GUNICORN_SOCKET = f"{RUN_DIR}/gunicorn.sock"

    # --------------------------------------------------
    # CONNECTION
    # --------------------------------------------------

    debug(f"Deploying {PROJECT_NAME} ({TIER})")

    c = Connection(
        host=HOST,
        user=USER,
        connect_kwargs={"key_filename": SSH_KEY},
    )

    # --------------------------------------------------
    # VERIFY HOST
    # --------------------------------------------------

    debug("Running host verification")
    c.put(SCRIPTS_DIR / "verify_host.sh", "/tmp/verify_host.sh")
    c.run("bash /tmp/verify_host.sh")

    # --------------------------------------------------
    # DIRECTORIES
    # --------------------------------------------------

    c.sudo(f"mkdir -p {PROJECT_DIR} {LOG_DIR} {RUN_DIR}")
    c.sudo(f"chown -R {USER}:{USER} {PROJECT_DIR} {LOG_DIR}")
    c.sudo(f"chown {USER}:www-data {RUN_DIR}")
    c.sudo(f"chmod 2775 {RUN_DIR}")

    # --------------------------------------------------
    # CLONE / UPDATE REPO
    # --------------------------------------------------

    token = get_github_token()

    if c.run(f"test -d {PROJECT_DIR}/.git", warn=True).failed:
        debug("Cloning repository")
        clone_url = REPO_URL
        if token and REPO_URL.startswith("https://"):
            clone_url = REPO_URL.replace(
                "https://", f"https://x-access-token:{token}@"
            )
        c.run(f"git clone -b {BRANCH} {clone_url} {PROJECT_DIR}")
    else:
        debug("Updating repository")
        with c.cd(PROJECT_DIR):
            c.run("git fetch")
            c.run(f"git reset --hard origin/{BRANCH}")

    # --------------------------------------------------
    # PROJECT ENV
    # --------------------------------------------------

    debug("Uploading project .env")
    env_text = normalize_env_text(env_path.read_text(), PROJECT_DIR)
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp_env:
        tmp_env.write(env_text)
        tmp_env_path = tmp_env.name

    remote_env_tmp = f"/tmp/{PROJECT_NAME}.env"
    try:
        c.put(tmp_env_path, remote_env_tmp)
        c.sudo(f"mv {remote_env_tmp} {PROJECT_DIR}/.env")
        c.sudo(f"chown {USER}:{USER} {PROJECT_DIR}/.env")
        c.sudo(f"chmod 600 {PROJECT_DIR}/.env")
    finally:
        os.remove(tmp_env_path)

    # --------------------------------------------------
    # PYTHON ENV
    # --------------------------------------------------

    if c.run(f"test -d {VENV_DIR}", warn=True).failed:
        debug("Creating virtualenv")
        c.run(f"python3 -m venv {VENV_DIR}")

    with c.cd(PROJECT_DIR):
        debug("Installing Python dependencies")
        c.run(f"{VENV_DIR}/bin/pip install -U pip")
        c.run(f"{VENV_DIR}/bin/pip install -r requirements.txt", warn=True)

    # --------------------------------------------------
    # NODE / FRONTEND
    # --------------------------------------------------

    if ENABLE_NODE:
        node_dir = resolve_node_dir(env.get("NODE_DIR"), PROJECT_DIR)
        debug("Preparing frontend")

        if c.run(f"test -f {node_dir}/package.json", warn=True).failed:
            raise RuntimeError(
                f"package.json not found at {node_dir}. "
                f"Set NODE_DIR in {env_path}."
            )

        node_install_cmd = env.get("NODE_INSTALL_CMD") or "npm install"
        node_build_cmd = env.get("NODE_BUILD_CMD") or "npm run build --if-present"

        with c.cd(node_dir):
            debug("Installing Node dependencies")
            c.run(node_install_cmd)
            debug("Building frontend")
            c.run(node_build_cmd)

    # --------------------------------------------------
    # DJANGO STATIC
    # --------------------------------------------------

    manage_py = f"{PROJECT_DIR}/manage.py"
    skip_collectstatic = env.get("SKIP_COLLECTSTATIC") in {"1", "true", "yes"}
    if not skip_collectstatic and c.run(f"test -f {manage_py}", warn=True).ok:
        debug("Collecting Django static files")
        with c.cd(PROJECT_DIR):
            env_prefix = shell_env_prefix(env)
            c.run(
                f"{env_prefix} {VENV_DIR}/bin/python manage.py collectstatic --noinput"
            )

    # --------------------------------------------------
    # SYSTEMD UNITS
    # --------------------------------------------------

    debug("Installing systemd templates")

    systemd_units = ["app@.service", "app@.socket", "node@.service", "celery@.service"]
    keep_systemd_overrides = env.get("KEEP_SYSTEMD_OVERRIDES") in {"1", "true", "yes"}

    if not keep_systemd_overrides:
        debug("Removing stale systemd instance overrides")
        purge_systemd_instance_units(c, PROJECT_NAME, systemd_units)

    if LEGACY_PROJECT_NAMES and not keep_systemd_overrides:
        debug(f"Removing legacy systemd instances: {', '.join(LEGACY_PROJECT_NAMES)}")
        for legacy in LEGACY_PROJECT_NAMES:
            disable_systemd_instances(c, legacy)
            purge_systemd_instance_units(c, legacy, systemd_units)
            c.sudo(f"rm -rf /run/{legacy}", warn=True)

    for unit in systemd_units:
        c.put(SYSTEMD_DIR / unit, f"/tmp/{unit}")
        c.sudo(f"mv /tmp/{unit} /etc/systemd/system/{unit}")

    # --------------------------------------------------
    # NGINX
    # --------------------------------------------------

    backend_locations = []
    for path in parse_backend_paths(BACKEND_PATHS):
        backend_locations.append(
            f"""location ^~ {path}/ {{
            proxy_pass http://unix:{GUNICORN_SOCKET}:;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}""")

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
        c.sudo(f"systemctl enable --now app@{PROJECT_NAME}")
    else:
        c.sudo(f"systemctl enable --now app@{PROJECT_NAME}.socket")

    if ENABLE_NODE:
        c.sudo(f"systemctl enable --now node@{PROJECT_NAME}")

    if ENABLE_CELERY:
        c.sudo(f"systemctl enable --now celery@{PROJECT_NAME}")

    c.sudo("nginx -t")
    c.sudo("systemctl reload nginx")

    debug("Restarting services")
    if TIER == "hot":
        c.sudo(f"systemctl restart app@{PROJECT_NAME}")
    else:
        c.sudo(f"systemctl restart app@{PROJECT_NAME}.socket")
        c.sudo(f"systemctl try-restart app@{PROJECT_NAME}.service")

    if ENABLE_NODE:
        c.sudo(f"systemctl restart node@{PROJECT_NAME}")

    if ENABLE_CELERY:
        c.sudo(f"systemctl restart celery@{PROJECT_NAME}")

    debug("Deploy completed successfully")

ns = Collection(deploy)
