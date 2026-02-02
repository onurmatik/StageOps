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
├── envs/
│   └── newsradar.env      # Per-project environment & config
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
│   ├── get_github_app_token.py
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

Tier is defined per project in its env file.

```env
TIER=hot | cold | dormant
```

---

## Per-Project Configuration

Each project has **one env file** under `envs/`.

Example: `envs/newsradar.env`

```env
# Identity
PROJECT_NAME=newsradar

# Tier
TIER=cold

# Networking
DOMAIN=newsradar.app

# Runtime capabilities
ENABLE_NODE=0
ENABLE_CELERY=1
CELERY_QUEUE=newsradar

# Resource hints
WORKERS=1
THREADS=2
MEMORY_LIMIT=400M
```

No project-specific logic lives in StageOps code.
Only data.

---

## Deployment Flow

From the **StageOps repo**:

```bash
fab deploy:newsradar
```

What this does:

1. Loads `envs/newsradar.env`
2. Connects to the staging server
3. Ensures base directories exist
4. Installs systemd templates (once)
5. Renders project-specific systemd units
6. Renders nginx config
7. Enables services based on tier
8. Reloads systemd and nginx

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
* Manage secrets beyond `.env`
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
