# Fork Upgrade Runbook — dropweb (zenbot) ← upstream Bedolaga

Operational runbook for moving a running instance onto this fork after the
**2026-06-23 rebase from upstream v3.59.0 onto v3.61.0**.

## Context

This repo is dropweb's fork of `BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot`.
Our only schema-affecting fork change is the **per-partner commission overrides**
migration. During the rebase it was **renumbered** to avoid colliding with
upstream's own `0089`:

| | Old fork (image built ≤ 2026-06-03) | New fork (v3.61.0 rebase) |
|---|---|---|
| partner-commission migration | `0089_partner_commission_overrides` | `0094_partner_commission_overrides` |
| upstream `0089` slot | (did not exist) | `0089_wheel_spins_telegram_charge_id` |

Image: `ghcr.io/enkinvsh/zenbot:latest` (default branch `main` publishes `:latest`,
linux/amd64). The app runs `alembic upgrade head` programmatically on startup.

## ⚠️ The gotcha (read before deploying)

An instance that ran the **old fork** has `alembic_version = 0089`, where `0089`
*meant our partner migration*. In the new image `0089` now means upstream's
`wheel_spins` migration. A naive `docker compose up -d` would therefore:

1. See `alembic_version = 0089`, assume upstream wheel is applied (it is NOT) →
   `wheel_spins.telegram_charge_id` silently skipped.
2. Apply 0090 → 0093.
3. Run `0094` (partner) → tries to ADD partner columns that **already exist** →
   `column already exists` → **startup crash**.

Only **old-fork** instances are affected. Fresh installs and stock-upstream
instances upgrade cleanly (our `0094` chains cleanly onto upstream `0093`).

## Step 0 — detect which case you're in

```bash
DB=<db_container>          # e.g. dropweb_bot_db / remnawave_bot_db
U=$(docker exec "$DB" printenv POSTGRES_USER)
D=$(docker exec "$DB" printenv POSTGRES_DB)
docker exec "$DB" psql -U "$U" -d "$D" -tAc "SELECT version_num FROM alembic_version;"
docker exec "$DB" psql -U "$U" -d "$D" -tAc "SELECT count(*) FROM information_schema.columns WHERE table_name='users' AND column_name='referral_first_payment_percent';"
docker exec "$DB" psql -U "$U" -d "$D" -tAc "SELECT count(*) FROM information_schema.columns WHERE table_name='wheel_spins' AND column_name='telegram_charge_id';"
```

| alembic_version | partner col | wheel.telegram_charge_id | Case | Action |
|---|---|---|---|---|
| `0089` | 1 (present) | 0 (absent) | **OLD FORK** | Procedure A (reconcile) |
| `0094 (head)` | 1 | present | already on new fork | nothing |
| upstream rev (`0093`/`0089_wheel`/…) | 0 | present/n-a | stock upstream | Procedure B |
| empty / no table | — | — | fresh install | Procedure B |

## Procedure A — OLD-FORK instance (migration reconciliation)

Per-host names differ — fill in `<compose_dir>` / service `bot` / `<bot_container>` / `<db_container>`.

```bash
cd <compose_dir>
DB=<db_container>
U=$(docker exec "$DB" printenv POSTGRES_USER)
D=$(docker exec "$DB" printenv POSTGRES_DB)

# 1) BACKUP (mandatory) — plain SQL dump, gzipped
TS=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p backups
docker exec "$DB" pg_dump -U "$U" -d "$D" --no-owner | gzip > "backups/predeploy_3.61_${TS}.sql.gz"
gzip -t "backups/predeploy_3.61_${TS}.sql.gz" && echo "backup OK"

# 2) Pull new image and confirm it is the intended build
docker compose pull bot
docker image inspect ghcr.io/enkinvsh/zenbot:latest \
  --format 'revision={{index .Config.Labels "org.opencontainers.image.revision"}} created={{.Created}}'

# 3) Pre-flight: tables exist, target columns absent (sanity)
docker exec "$DB" psql -U "$U" -d "$D" -tAc "SELECT to_regclass('public.wheel_spins'), to_regclass('public.payment_method_configs'), to_regclass('public.info_pages'), to_regclass('public.yandex_client_id_map');"

# 4) Reconcile the alembic graph in a one-off container (NO app start).
#    </dev/null prevents 'docker compose run' from eating the rest of the script.
docker compose run --rm -T --no-deps --entrypoint alembic bot stamp 0088 </dev/null
docker compose run --rm -T --no-deps --entrypoint alembic bot upgrade 0093 </dev/null   # applies wheel,0090,0091,0092,0093
docker compose run --rm -T --no-deps --entrypoint alembic bot stamp 0094 </dev/null     # partner cols already exist -> mark done
docker compose run --rm -T --no-deps --entrypoint alembic bot current </dev/null        # expect: 0094 (head)

# 5) Recreate the bot
docker compose up -d bot
```

Why `stamp 0094` (not run it): the partner columns already exist from the old
`0089`, and the `0094` body is identical (same two `users` columns), so we mark it
applied instead of re-running it.

## Procedure B — fresh / stock-upstream (painless)

```bash
cd <compose_dir>
docker compose pull bot
docker compose up -d bot        # startup auto-migration reaches 0094 cleanly
```

## Verify (any case)

```bash
docker ps --filter name=<bot_container> --format '{{.Status}}'                    # -> healthy
docker compose run --rm -T --no-deps --entrypoint alembic bot current </dev/null  # -> 0094 (head)
docker exec <bot_container> python -c "import urllib.request,os; req=urllib.request.Request('http://127.0.0.1:8080/health', headers={'Authorization':'Bearer '+os.environ['WEB_API_DEFAULT_TOKEN']}); print(urllib.request.urlopen(req,timeout=5).read().decode())"
docker logs <bot_container> --since 3m 2>&1 | grep -iE '\[error\]|\[critical\]'   # -> expect none
```

Expected `/health`: `{"status":"ok","bot_version":"3.61.0", ...}`.

## Rollback

The dump is plain SQL **without** `--clean`, so restore into an empty DB:

```bash
cd <compose_dir>
docker compose stop bot
# close app connections, then drop+recreate the DB (postgres 13+):
docker exec <db_container> psql -U "$U" -d postgres -c "DROP DATABASE \"$D\" WITH (FORCE); CREATE DATABASE \"$D\" OWNER \"$U\";"
gunzip -c backups/predeploy_3.61_<TS>.sql.gz | docker exec -i <db_container> psql -U "$U" -d "$D"
# if needed, re-pin the previous image in docker-compose.yml, then:
docker compose up -d bot
```

## Completed instances (2026-06-23, commit c35c592e)

| Instance | SSH host | compose dir | bot / db container | webhook | backup |
|---|---|---|---|---|---|
| dropweb-bot | `dropweb-bot` | `/opt/bedolaga` | `dropweb_bot` / `dropweb_bot_db` | worker.dropweb.org | `/opt/bedolaga/backups/predeploy_3.61_20260623T074637Z.sql.gz` |
| kover-bot | `kover` | `/opt/bedolaga-bot` | `remnawave_bot` / `remnawave_bot_db` | worker.koversamolet.org | `/opt/bedolaga-bot/backups/predeploy_3.61_20260623T081330Z.sql.gz` |

Both reconciled (Procedure A) and verified: `alembic 0094 (head)`, `/health` 200,
`bot_version 3.61.0`, 0 ERROR/CRITICAL.
