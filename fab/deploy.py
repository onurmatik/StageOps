from pathlib import Path
from fabric import Connection, task
import os

STAGEOPS_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = STAGEOPS_ROOT / "templates"

DEFAULT_HOST = os.environ.get("STAGE_HOST")
DEFAULT_USER = os.environ.get("STAGE_USER", "ubuntu")
DEFAULT_KEY = os.environ.get("STAGE_KEY", "~/.ssh/stage-ec2-key.pem")


def debug(msg: str):
    print(f"[stageops] {msg}")


def load_project_env():
    env_path = Path(".deploy/project.env")
    if not env_path.exists():
        raise RuntimeError("Missing .deploy/project.env")

    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def install_shared_systemd_templates(c):
    debug("Ensuring shared systemd templates")

    for name in ["app@.service", "app@.socket", "celery@.service", "node@.service"]:
        src = TEMPLATES / "systemd" / name
        if not src.exists():
            continue
        tmp = f"/tmp/{name}"
        c.put(src.as_posix(), tmp)
        c.sudo(f"mv {tmp} /etc/systemd/system/{name}")


@task
def deploy(c):
    """
    StageOps deploy:
    - applies systemd lifecycle
    - manages hot/cold tier
    - enables optional services (node, celery)
    """

    env = load_project_env()

    project = env["PROJECT_NAME"]
    tier = env.get("TIER", "cold")

    enable_node = env.get("ENABLE_NODE", "0") == "1"
    node_port = env.get("NODE_PORT")

    enable_celery = env.get("ENABLE_CELERY", "0") == "1"
    celery_queue = env.get("CELERY_QUEUE", project)

    debug(f"Deploying {project}")
    debug(f"tier={tier} node={enable_node} celery={enable_celery}")

    c = Connection(
        host=DEFAULT_HOST,
        user=DEFAULT_USER,
        connect_kwargs={"key_filename": os.path.expanduser(DEFAULT_KEY)},
    )

    install_shared_systemd_templates(c)

    # --- GUNICORN (core service) ---
    debug("Configuring gunicorn")

    if tier == "hot":
        c.sudo(f"systemctl disable app@{project}.socket", warn=True)
        c.sudo(f"systemctl enable app@{project}.service --now")
    elif tier == "cold":
        c.sudo(f"systemctl disable app@{project}.service", warn=True)
        c.sudo(f"systemctl enable app@{project}.socket")
    else:
        debug("Dormant tier: disabling gunicorn")
        c.sudo(f"systemctl disable app@{project}.service", warn=True)
        c.sudo(f"systemctl disable app@{project}.socket", warn=True)

    # --- NODE ---
    debug("Configuring node")

    if enable_node:
        c.sudo(
            f"systemctl enable node@{project}.service --now"
        )
    else:
        c.sudo(
            f"systemctl disable node@{project}.service",
            warn=True
        )

    # --- CELERY ---
    debug("Configuring celery")

    if enable_celery:
        c.sudo(
            f"systemctl enable celery@{project}.service --now"
        )
    else:
        c.sudo(
            f"systemctl disable celery@{project}.service",
            warn=True
        )

    debug("Reloading systemd")
    c.sudo("systemctl daemon-reload")

    debug("StageOps deploy complete")
