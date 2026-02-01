from __future__ import annotations

import os
import sys
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
    c.sudo(f"chown -R {USER}:{USER} {PROJECT_DIR} {LOG_DIR} {RUN_DIR}")

    # --------------------------------------------------
    # PROJECT ENV
    # --------------------------------------------------

    debug("Uploading project .env")
    remote_env_tmp = f"/tmp/{PROJECT_NAME}.env"
    c.put(env_path, remote_env_tmp)
    c.sudo(f"mv {remote_env_tmp} {PROJECT_DIR}/.env")
    c.sudo(f"chown {USER}:{USER} {PROJECT_DIR}/.env")
    c.sudo(f"chmod 600 {PROJECT_DIR}/.env")

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
    # SYSTEMD UNITS
    # --------------------------------------------------

    debug("Installing systemd templates")

    for unit in ["app@.service", "app@.socket", "node@.service", "celery@.service"]:
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

    debug("Deploy completed successfully")

ns = Collection(deploy)
