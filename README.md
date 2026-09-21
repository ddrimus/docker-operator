<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/banner_light@2x.png">
    <img src="images/banner_dark@2x.png" width="65%" alt="Docker Operator banner">
  </picture>
</p>
<p align="center">
  A minimal, <em>fully declarative, git-driven</em>, secure <em>fully self-contained</em> GitOps-style deployer for docker compose stacks, with encrypted secrets via SOPS and notifications. Easily configured via environment variables and triggered by Forgejo webhooks.
</p>

# What it does

Docker Operator is a Docker application that watches a git repository and keeps your docker compose stacks in sync with it. You push a change, the stacks that changed are redeployed. That is all it does.

The design is simple. Git is the source of truth. A stack is a folder with a `compose.yaml`, so there is nothing to register, and the only things the operator remembers are the hash of what it deployed last and how many times a failing change was retried, in a small JSON file. No web UI, no database, no agents, just one container.

- It can deploy straight from the bare repository on your Forgejo host, mounted read-only. No access token is needed, it can't change your repository, and it keeps working when the git server is down.
- If git is unreachable, the last known-good checkout is kept and it tries again later.
- Secrets stay encrypted in git (SOPS + AGE). They are decrypted only at deploy time, into a `0600` file, and never logged.
- A stack that owns a Docker network is deployed before the stacks that use it.
- A stack is validated before it is deployed. If the deploy fails it is retried, and what was already running is left alone.
- Every stack is written as a plain `compose.yaml` and a single `.env` (config and secrets merged) in a folder you can open. Use `docker compose up`, `down` or `logs` there as usual, alongside the operator.
- Every stack gets its own message, with the status of each container.

The first run deploys every stack in the repository. After that, only the ones that changed.

# Getting Started

This takes four steps. You put your stacks in a git repository, configure and start Docker Operator, and tell Forgejo to call it on every push. When you are done, each push deploys the stacks that changed.

You need Docker with Compose, and a Forgejo (or Gitea) server on the same machine, because Docker Operator reads the repository straight from the disk of your Forgejo host. If Forgejo runs on another machine, set `GIT_REPO_URL` to a git URL with a read-only token instead of using the repository volume.

Encrypted secrets are optional. You can start without them and add them later, see the section [Secrets](#secrets).

## 1. Prepare your stacks repository

Docker Operator deploys whatever it finds in your stacks repository, so the first thing to do is to lay it out the way it expects. Create a `compose/` folder at the root, and inside it one folder per stack. The folder name is the name of the stack, and each one needs at least a `compose.yaml`. Everything else is optional, see [Stack Files](#stack-files). When it looks like the example below, commit it and push it.

```
homelab-docker/
└── compose/
    ├── traefik/
    │   ├── compose.yaml
    │   ├── .env.config              # optional, plain variables
    │   ├── .env.secrets.encrypted   # optional, see Secrets
    │   └── .env.secrets.example     # optional, lists the secret names, never deployed
    └── grafana/
        └── ...
```

## 2. Configure

```bash
git clone https://github.com/ddrimus/docker-operator.git
cd docker-operator
cp example.env .env
```

In `.env`, this is the minimum you need to change:

```env
# a random value, for example the output of: openssl rand -hex 32
WEBHOOK_SECRET=

# leave it empty if you don't use encrypted secrets
SOPS_AGE_KEY_FILE=
```

Everything else in `example.env` already has a working default, including the required `GIT_REPO_URL`, which matches the repository volume in `compose.yaml`. Two more you will probably want: `GIT_BRANCH` if your branch is not `main`, and `NOTIFY_WEBHOOK_URL` to get notifications. The full list is in [Configuration](#configuration).

In `compose.yaml`, under the `docker-operator` service, publish the webhook port by adding:

```yaml
    ports:
      - "8080:8080"
```

Then replace the `/path/to/forgejo/data/git/repositories/owner/repo.git` volume with the path of your stacks repository on the Forgejo host.

Using encrypted secrets? Put your AGE key in `data/config/sops_age.key` and leave `SOPS_AGE_KEY_FILE` as it is, see [Secrets](#secrets).

## 3. Start

```bash
docker compose up -d --build
docker compose logs -f docker-operator   # Ctrl+C to stop following
```

The logs should show `listening on 0.0.0.0:8080/webhook`, then the first deploy of your stacks.

## 4. Add the webhook

In your stacks repository, go to **Settings → Webhooks → Add webhook** and fill in:

- **Target URL**: `http://YOUR_SERVER_IP:8080/webhook`
- **Secret**: the `WEBHOOK_SECRET` from your `.env`
- **Trigger on**: `Push events`

Click **Test Delivery**, a `pong` answer means it works. From now on, every push deploys your stacks.

# Usage

Once it is running, you work on your stacks in git and Docker Operator does the rest. This is what a stack is made of, where its files end up, and how to keep its secrets encrypted.

## Stack Files

Every directory under `COMPOSE_SUBDIR` that contains a `compose.yaml` is a stack, named after its folder. Everything below must be committed to git:

| File                     | Purpose                                                                     |
|--------------------------|-----------------------------------------------------------------------------|
| `compose.yaml`           | The stack itself.                                                           |
| `.env.config`            | Plain, non-sensitive environment variables.                                 |
| `.env.secrets.encrypted` | Secrets encrypted with SOPS + AGE, decrypted at deploy time.                |
| `.depends_on`            | One stack name per line (`#` comments allowed), deployed before this stack. |
| `.paused`                | An empty file which keeps the stack from being deployed until it's removed. |

## Deploy Folder

Every deploy writes the stack to `/deploy/<stack>` in the container, which is `data/data/deploy/<stack>` on the host with the default `compose.yaml`. It holds the `compose.yaml` and one `.env` with the config and the decrypted secrets merged. Set `DEPLOY_UID` and `DEPLOY_GID` to your user to open it without root, then work as usual:

```bash
cd data/data/deploy/traefik
docker compose logs -f
docker compose down
```

The files stay as they are until that stack changes in git and is deployed again. To keep the operator away from a stack meanwhile, commit a `.paused` file in its folder.

## Secrets

Secrets are stored in git encrypted. Each stack's `.env` is built from `.env.config` plus the decrypted `.env.secrets.encrypted`, values are never logged. You need [AGE](https://github.com/FiloSottile/age) and [SOPS](https://github.com/getsops/sops) on the machine where you edit your stacks.

```bash
# 1. create your key, keep it out of git and back it up
age-keygen -o sops_age.key && chmod 600 sops_age.key

# 2. write the secrets, then encrypt them in place with your public key (age1...)
echo 'DB_PASSWORD=your-password' > compose/traefik/.env.secrets.encrypted
sops --encrypt --age age1yourpublickey... --in-place \
  --input-type dotenv --output-type dotenv compose/traefik/.env.secrets.encrypted

# 3. edit them later
SOPS_AGE_KEY_FILE=sops_age.key sops edit \
  --input-type dotenv --output-type dotenv compose/traefik/.env.secrets.encrypted
```

Before you commit, open the file and check that the values look like `ENC[...]`. If you can read them, they are not encrypted. The key file never goes in git, it stays on the server in `data/config/sops_age.key`, see [Configure](#2-configure).

# Configuration

This is the full list of the environment variables you can set in your `.env`, the same ones as in [example.env](example.env). Please read everything carefully before asking for help, as most problems are simple configuration issues.

| Variable                                    | Default    | Description                                                                          |
|---------------------------------------------|------------|--------------------------------------------------------------------------------------|
| `GIT_REPO_URL`                              | required   | Git URL or bind-mounted path of the repository holding your stacks.                  |
| `WEBHOOK_SECRET`                            | required   | Secret used to verify webhook signatures.                                            |
| `GIT_BRANCH`                                | `main`     | Branch to deploy from. Pushes to other branches are ignored.                         |
| `COMPOSE_SUBDIR`                            | `compose`  | Directory in the repository which contains the stacks.                               |
| `WEBHOOK_PATH`                              | `/webhook` | Path the webhook is served on.                                                       |
| `SOPS_AGE_KEY_FILE`                         |            | AGE key used to decrypt secrets. Required if any stack has `.env.secrets.encrypted`. |
| `REGISTRY_HOST` / `_USERNAME` / `_PASSWORD` |            | Private registry login, all three or none.                                           |
| `PULL_IMAGES`                               | `true`     | Pull images before deploying.                                                        |
| `PRUNE_REMOVED_STACKS`                      | `false`    | Tear down stacks removed from git.                                                   |
| `POLL_INTERVAL_SECONDS`                     | `300`      | How often to reconcile without a webhook. `0` disables polling.                      |
| `DEPLOY_TIMEOUT_SECONDS`                    | `300`      | Time a single deploy has before it's considered failed.                              |
| `DEPLOY_MAX_RETRIES`                        | `3`        | Attempts at a failing change before giving up.                                       |
| `DEPLOY_RETRY_DELAY_SECONDS`                | `60`       | Delay between retries.                                                               |
| `DEPLOY_PRIORITY`                           |            | Comma-separated stack names to deploy first, where dependencies allow.               |
| `DEPLOY_UID` / `DEPLOY_GID`                 |            | Owner of `/deploy`, so you can `docker compose` in it as yourself.                   |
| `NOTIFY_WEBHOOK_URL`                        |            | Discord-compatible webhook URL for notifications.                                    |
| `LOG_LEVEL`                                 | `INFO`     | Log verbosity.                                                                       |
| `TZ`                                        | `UTC`      | Timezone used for logs and notifications.                                            |

Run these inside the container with `docker compose exec docker-operator python -m docker_operator <flag>`:

- `--once` runs a single reconcile pass and exits.
- `--force STACK` redeploys `STACK` even if unchanged (repeatable, `all` for every stack), requires `--once`. For example, `docker compose exec docker-operator python -m docker_operator --once --force traefik`.
- `--validate` validates every stack's compose config without deploying, handy for CI.

# Contributing

If you have an improvement, extra information, or you notice something wrong with Docker Operator, feedback is welcome. Feel free to open a pull request with your suggestion or the details.

If something doesn't work as you expect, please open an [issue](https://github.com/ddrimus/docker-operator/issues) with everything relevant: what you did, what you expected, and the logs from `docker compose logs docker-operator` (set `LOG_LEVEL=DEBUG` for more). Remove any secret or token before you paste them. Your suggestions and feedback are always welcome and appreciated!
