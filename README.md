# StageOps

**StageOps** is a lightweight deployment and operations toolkit for running **many small projects** on a **single staging server** with minimal overhead.

It is designed for environments where:

* You host **10–30 mostly idle projects**
* A few projects are always-on, the rest are cold
* Projects mature over time and later move to dedicated infrastructure
* You want **predictable behavior**, not constant DevOps work

StageOps uses **Fabric + systemd + nginx** and deliberately avoids containers, Kubernetes, or heavy orchestration.

---

## Core Ideas

* **Tiered hosting** (hot / cold / dormant)
* **systemd is the orchestrator**
* **nginx is the router**
* **Gunicorn + Celery are shared patterns**
* **Cold apps consume ~0 RAM when idle**
* **Promoting an app requires no redeploy**

This repo contains **only ops logic**.
Application code lives elsewhere.

---

## Target Host

Typical configuration:

* AWS EC2 `t3.medium`
* 2 vCPU / 4 GB RAM
* Ubuntu
* One shared nginx instance
* Redis shared across projects

---

## Repository Structure

```
StageOps/
├── fab/
│   └── deploy.py          # Main Fabric entrypoint
│
├── app.yaml               # Per-project infra config
│
├── templates/
│   ├── systemd/
│   │   ├── app@.service
│   │   ├── app@.socket
│   │   ├── celery@.service
│   │   └── node@.service
│   │
│   └── nginx/
│       └── django.conf.j2
│
├── scripts/
│   └── verify_host.sh
│
└── README.md
```

---

## Concept: Tiers

Each project belongs to one tier.

### 🔥 Hot

* Always-on
* Gunicorn always running
* Minimal latency

### ❄️ Cold

* systemd socket-activated
* Gunicorn starts on first request
* ~0 MB RAM while idle
* 1–3s cold start

### 💤 Dormant

* Code exists
* No services enabled
* No nginx config

Tier is defined per project in `app.yaml`.

---

## Per-Project Configuration

All infra configuration lives in a single `app.yaml`.

Example:

```yaml
server:
  host: 18.206.25.249
  user: ubuntu
  ssh_key: ~/.ssh/stage-ec2-key.pem
  log_access: /var/log/{PROJECT_NAME}/gunicorn-access.log
  log_errors: /var/log/{PROJECT_NAME}/gunicorn-error.log
  defaults:
    gunicorn_workers: 1
    gunicorn_threads: 2
    gunicorn_timeout: 60
    gunicorn_graceful_timeout: 30
    gunicorn_max_requests: 500
    gunicorn_max_requests_jitter: 50
    memory_limit: 400M
    cpu_quota: 40%

apps:
  newsradar:
    project_name: newsradar
    domain: newsradar.app
    tier: cold
    enable_node: false
    enable_celery: true
    celery_queue: newsradar
    backend_paths: ["/api"]
    gunicorn_worker_class: gthread
```

Values under `server.defaults` are applied to each app unless the app overrides them.

No project-specific logic lives in StageOps code.
Only data.

---

## Deployment Flow

From the **StageOps repo**:

```bash
fab infra
```

Filter to specific apps:

```bash
fab infra --only=mevzuat,newsradar
```

What this does:

1. Loads `app.yaml`
2. Connects to the staging server
3. Iterates all apps defined in `app.yaml`
4. Ensures base directories exist
5. Installs systemd templates
6. Renders project-specific systemd overrides
7. Renders nginx config
8. Enables services based on tier
9. Reloads systemd and nginx

---

## systemd Model

### Gunicorn (Django / API)

* `app@.service`
* `app@.socket` (for cold tier)

Cold tier uses **socket activation**:

* nginx hits unix socket
* systemd starts Gunicorn
* Gunicorn exits when idle (eventually)

### Celery

* `celery@.service`
* Optional, per project
* Uses shared Redis

### Node (SSR / frontend)

* `node@.service`
* Optional
* Used when `ENABLE_NODE=1`

---

## nginx Model

* One nginx instance
* One config per project
* Single domain per project (by design)

Behavior:

* If `ENABLE_NODE=1`

  * `/` → Node
  * backend paths → Gunicorn
* If `ENABLE_NODE=0`

  * `/` → Gunicorn

Backend paths are configurable and rendered from template.

---

## Cron Jobs

Add per-app crons under `cron`. Each entry is a standard cron line.

If the command does not start with an absolute path (or a shell/python prefix),
StageOps treats it as a Django management command and expands it to:

`/srv/apps/<project>/venv/bin/python manage.py <command>`

Examples:

```yaml
apps:
  mevzuat:
    cron:
      - "0 0 * * * fetch_new_docs"
      - "30 2 * * 1 /srv/apps/mevzuat/venv/bin/python manage.py cleanup"
      - "0 4 * * * {PROJECT_ENV_PATH}/python {PROJECT_PATH}/manage.py clearsessions"
```

Available placeholders:

* `{PROJECT_PATH}` → `/srv/apps/<project>`
* `{PROJECT_ENV_PATH}` → `/srv/apps/<project>/venv/bin`
* `{PROJECT_NAME}` → `<project>`

---

## Promotion Workflow

When a project grows:

1. Disable it on staging
2. Provision a new host
3. Deploy the same app code
4. Update DNS

No architectural changes required.

StageOps is intentionally **not** production tooling.

---

## Non-Goals

StageOps intentionally does **not**:

* Manage databases
* Manage secrets outside StageOps
* Handle autoscaling
* Replace CI/CD
* Support Kubernetes or Docker

This is a **staging / incubation tool**.

---

## Philosophy

* RAM is more expensive than latency on staging
* Idle apps should cost ~0
* systemd is powerful and boring (good)
* Simplicity beats cleverness
* Promotion should be frictionless

If this feels boring — it’s working.

---

## Status

StageOps is actively evolving, but the core ideas are stable.

Changes should:

* Reduce operational overhead
* Improve predictability
* Avoid adding complexity

---

## License

Internal tooling.
Use, fork, adapt freely.
