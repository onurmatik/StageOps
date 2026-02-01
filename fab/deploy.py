from __future__ import annotations

import os
from pathlib import Path
from fabric import Connection, task
from invoke import Collection
from dotenv import load_dotenv

# --------------------------------------------------
# ENV & GLOBALS
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / "project.env"

load_dotenv(ENV_FILE)

PROJECT_NAME = os.environ["PROJECT_NAME"]
DOMAIN = os.environ["DOMAIN"]

REPO_URL = os.environ["REPO_URL"]
BRANCH = os.environ.get("BRANCH", "main")

HOST = os.environ["HOST"]
USER = os.environ.get("USER", "ubuntu")
SSH_KEY = os.environ["SSH_KEY"]

TIER = os.environ.get("TIER", "cold")  # hot | cold

ENABLE_NODE = os.environ.get("ENABLE_NODE", "0") in {"1", "true", "yes"}
NODE_PORT = os.environ.get("NODE_PORT", "3000")

BACKEND_PATHS = os.environ.get("BACKEND_PATHS", "")

# --------------------------------------------------
# PATHS (SERVER)
# --------------------------------------------------

PROJECT_DIR = f"/srv/apps/{PROJECT_NAME}"
VENV_DIR = f"{PROJECT_DIR}/venv"

LOG_DIR = f"/var/log/{PROJECT_NAME}"
RUN_DIR = f"/run/{PROJECT_NAME}"

GUNICORN_SOCKET = f"{RUN_DIR}/gunicorn.sock"

# --------------------------------------------------
# HELPERS
# --------------------------------------------------


def debug(msg: str) -> None:
    print(f"[stageops] {msg}")


def parse_backend_paths(raw: str) -> list[str]:
    if not raw:
        return []
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


def render_template(text: str, ctx: dict) -> str:
    for k, v in ctx.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text


def upload_template(c, local_path: Path, remote_path: str, context: dict):
    rendered = render_template(local_path.read_text(), context)
    tmp = f"/tmp/{local_path.name}"
    c.put(Path(local_path).write_text(rendered) or local_path, tmp)
    c.sudo(f"mv {tmp} {remote_path}")


# --------------------------------------------------
# NGINX CONTEXT
# --------------------------------------------------

backend_locations = []
for path in parse_backend_paths(BACKEND_PATHS):
    backend_locations.append(f"""
    location ^~ {path}/ {{
        proxy_pass http://unix:{GUNICORN_SOCKET}:;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
    """)

BACKEND_LOCATIONS = "\n".join(backend_locations)

if ENABLE_NODE:
    FRONTEND_UPSTREAM = f"http://127.0.0.1:{NODE_PORT}"
else:
    FRONTEND_UPSTREAM = f"http://unix:{GUNICORN_SOCKET}:"

TEMPLATE_CONTEXT = {
    "PROJECT_NAME": PROJECT_NAME,
    "DOMAIN": DOMAIN,
    "PROJECT_DIR": PROJECT_DIR,
    "GUNICORN_SOCKET": GUNICORN_SOCKET,
    "BACKEND_LOCATIONS": BACKEND_LOCATIONS,
    "FRONTEND_UPSTREAM": FRONTEND_UPSTREAM,
}

# --------------------------------------------------
# DEPLOY TASK
# --------------------------------------------------


@task
def deploy(c):
    """
    Deploy a project using StageOps.
    """
    debug(f"Deploying {PROJECT_NAME} ({TIER})")

    c = Connection(
        host=HOST,
        user=USER,
        connect_kwargs={"key_filename": SSH_KEY},
    )

    # Directories
    c.sudo(f"mkdir -p {PROJECT_DIR} {LOG_DIR} {RUN_DIR}")
    c.sudo(f"chown -R {USER}:{USER} {PROJECT_DIR} {LOG_DIR}")

    # Clone / update repo
    if c.run(f"test -d {PROJECT_DIR}/.git", warn=True).failed:
        debug("Cloning repository")
        c.run(f"git clone -b {BRANCH} {REPO_URL} {PROJECT_DIR}")
    else:
        debug("Updating repository")
        with c.cd(PROJECT_DIR):
            c.run("git fetch")
            c.run(f"git reset --hard origin/{BRANCH}")

    # Virtualenv
    if c.run(f"test -d {VENV_DIR}", warn=True).failed:
        debug("Creating virtualenv")
        c.run(f"python3 -m venv {VENV_DIR}")

    with c.cd(PROJECT_DIR):
        debug("Installing Python dependencies")
        c.run(f"{VENV_DIR}/bin/pip install -U pip")
        c.run(f"{VENV_DIR}/bin/pip install -r requirements.txt", warn=True)

    # --------------------------------------------------
    # systemd units
    # --------------------------------------------------

    debug("Installing systemd templates")

    c.put("systemd/app@.service", "/tmp/app@.service")
    c.put("systemd/app@.socket", "/tmp/app@.socket")
    c.put("systemd/node@.service", "/tmp/node@.service")
    c.put("systemd/celery@.service", "/tmp/celery@.service")

    c.sudo("mv /tmp/app@.service /etc/systemd/system/")
    c.sudo("mv /tmp/app@.socket /etc/systemd/system/")
    c.sudo("mv /tmp/node@.service /etc/systemd/system/")
    c.sudo("mv /tmp/celery@.service /etc/systemd/system/")

    # --------------------------------------------------
    # nginx
    # --------------------------------------------------

    debug("Configuring nginx")

    upload_template(
        c,
        BASE_DIR / "templates/nginx/django.conf.j2",
        f"/etc/nginx/sites-available/{PROJECT_NAME}.conf",
        TEMPLATE_CONTEXT,
    )

    c.sudo(
        f"ln -sf /etc/nginx/sites-available/{PROJECT_NAME}.conf "
        f"/etc/nginx/sites-enabled/{PROJECT_NAME}.conf"
    )

    # --------------------------------------------------
    # Enable services
    # --------------------------------------------------

    c.sudo("systemctl daemon-reload")

    if TIER == "hot":
        c.sudo(f"systemctl enable --now app@{PROJECT_NAME}")
    else:
        c.sudo(f"systemctl enable --now app@{PROJECT_NAME}.socket")

    if ENABLE_NODE:
        c.sudo(f"systemctl enable --now node@{PROJECT_NAME}")

    c.sudo("nginx -t")
    c.sudo("systemctl reload nginx")

    debug("Deploy completed successfully")


ns = Collection(deploy)
