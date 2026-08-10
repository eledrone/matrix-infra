# matrix-infra

Configuration for a Dendrite homeserver with a MatrixRTC (Element Call) backend,
deployed by GitHub Actions over Tailscale, with secrets held in a self-hosted
Infisical vault.

```
matrix-infra/
├── homeserver/     Dendrite, Postgres, coturn, LiveKit, lk-jwt-service, Element Call
├── vault/          Infisical (own stack, so a homeserver deploy cannot take it down)
├── deploy.sh       renders templates from env, then docker compose up -d
└── scripts/        one-time migration helper
```

The repo root is also the deployment directory on the server: Compose bind mounts
are relative, so the working tree and the runtime directory are the same place.
Runtime state lives under `homeserver/` and is gitignored.

## What is in git, and what is not

| Committed | Not committed |
|---|---|
| `homeserver/docker-compose.yml` (secrets as `${VARS}`) | `.env` — the secret values |
| `homeserver/config/dendrite.yaml.tmpl` | `config/dendrite.yaml` — rendered at deploy |
| `homeserver/livekit/livekit.yaml.tmpl` | `livekit/livekit.yaml` — rendered |
| `homeserver/coturn/turnserver.conf.tmpl` | `coturn/turnserver.conf` — rendered |
| `homeserver/element-call/config.json` | signing keys, TLS private keys |
| `deploy.sh`, `vault/`, workflow | all runtime state: database, media, logs, backups |

Every secret is a `${VARIABLE}` in a `.tmpl` file, substituted at deploy time from
the vault. The whole configuration is versioned; no credential is.

## Deploy flow

```
push to main
  └─ validate      compose parses; every ${VAR} in a template is declared in deploy.sh
  └─ deploy        join tailnet → read secrets from vault → ssh to the server
                   → git pull → write .env → ./deploy.sh
                       → envsubst templates → docker compose up -d
                       → wait for /_matrix/client/versions
```

GitHub stores only four bootstrap credentials; every application secret, including
the deploy SSH key, is read from the vault during the job.

| Name | Type |
|---|---|
| `TS_OAUTH_CLIENT_ID` | variable |
| `TS_OAUTH_SECRET` | secret |
| `INFISICAL_CLIENT_ID` | variable |
| `INFISICAL_CLIENT_SECRET` | secret |

The vault listens on a Tailscale address only, so it is reachable from the runner
solely because the job joins the tailnet, and a CI tag restricts that node to SSH
and the vault on one host.

## Running a deploy by hand

```bash
cd ~/docker/matrix-infra
./deploy.sh              # render config and reconcile, keeping current images
PULL=1 ./deploy.sh       # deliberately pull images first
```

`PULL` is opt-in on purpose: images are floating tags, so pulling on every config
change would mean an ordinary edit could also upgrade the homeserver. Database
schema migrations run automatically at startup and are one-way.

## Rollback

```bash
git log --oneline
git checkout <good-commit> -- homeserver/config/dendrite.yaml.tmpl
./deploy.sh
```

`deploy.sh` also keeps a `.prev` copy of each file it re-renders.

This rolls back **configuration only**. If a deploy also pulled new images, git
cannot undo a schema migration that has already run.

## First-time setup

Extracting the secrets out of existing config files into templates:

```bash
python3 scripts/make-templates.py
```

It writes the `.tmpl` files and a `.env`, then verifies that rendering each
template reproduces the current live file byte-for-byte. If anything reports
`DIFFERS`, stop rather than deploy.

The vault is brought up separately; see the header of `vault/docker-compose.yml`.

## Notes

- Compose project names are pinned with `name:` in both stacks. Without that the
  project name follows the directory name, and moving the directory renames every
  container — which breaks anything referring to a container by name.
- `TURN_SHARED_SECRET` is used by both the homeserver config and the TURN server
  config; one variable feeds both so they cannot drift apart.
