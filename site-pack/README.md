Site pack (per-site bundle)
===========================

This folder is meant to hold *one* site’s config + secrets when you stage or restore an IPC. Keep it out of git unless you encrypt it; treat it like a seed vault.

What to place here
------------------
- `.env` – real env for the agent (copy from `.env.example` and fill secrets).
- `.secrets/` – first-boot Influx admin user/pass/token (`influx.admin.*` files). Replace placeholders with real values.
- `chirpstack-devices.yml` (optional) – DevEUIs + AppKeys if you want to drive provisioning from a seed.
- `storage/` – site-config, registries, miner db, and profiles (see `storage/README.md`).
- Optional helpers in `.secrets/`: `.env.grafana` (admin creds) and `.env.smtp` (mail settings) consumed by the data stack.
- Optional: `chirpstack.api_key` / `chirpstack.app_id` (bootstrap-lite.sh writes the API key; or store them manually).

Workflow notes
--------------
- Seed/restore: copy the `storage/` contents (site-config + registries + profiles) from your source of truth into this folder, and keep `.env` alongside.
- Backups: after tuning via UI, grab the updated `storage/control-registry.json` and `storage/provision-registry.json` from the running box and drop them here before committing to your site-seed repo.
- If `miners.sqlite` is absent on a fresh box, the agent can seed ranges from `site-config.yml`; still back up `miners.sqlite` if you rely on UI edits.
- Tokens: the data stack mints a bucket-scoped token and writes it to `site-pack/.secrets/influx.token` (mounted as `/secrets/influx.token`), so `INFLUX_TOKEN` does not need to be written into `.env` (use `INFLUX_TOKEN_FILE`).
