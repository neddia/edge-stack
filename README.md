Edge stack scaffold
===================

This folder holds the infra pieces that *aren’t* in the site-agent repo: Docker stacks for Influx + ChirpStack + the agent, plus a “site pack” to stage per-site config/secrets.

Layout
------
- `edge-stacks/data/docker-compose.yml` – InfluxDB with first-boot init + token minting (token lands in `site-pack/.secrets/influx.token`) and Grafana with Influx datasource provisioning.
- `edge-stacks/lorawan/` – place the upstream `chirpstack-docker` repo here; optional `docker-compose.init.yml` auto-creates an app + API key and drops them in `site-pack/.secrets`.
- `edge-stacks/app/docker-compose.yml` – site-agent container wired to `site-pack/.env`, `site-pack/.secrets`, and `site-pack/storage`.
- `site-pack/` – per-site bundle (not for git). `.env` + `.secrets/` + `storage/` live here; see `site-pack/README.md`.
- `scripts/` – helper scripts for bootstrap and packaging.
- `scripts/setup-lora-gateway.sh` – stamps the concentrator EUI into `global_conf.json` (expects sx1302_hal built and `/dev/ht1303-0` symlink).
- `scripts/lora-pkt-fwd.service` – systemd unit template for the packet forwarder.
- `scripts/init-chirpstack-config.sh` – generates `chirpstack.toml` from a template with a fresh secret (no overwrite if it already exists).
- `bootstrap-lite.sh` will install `masscan` on the host and set capabilities for miner discovery if it is not already present.
- `scripts/cs-seed.py` – seeds ChirpStack (gateway/app/device profile/devices) from `site-pack/storage/chirpstack-devices.yml`.

Quick start (local dev / staging an IPC)
----------------------------------------
1) Fill the site pack (single site at a time):
   - Copy `.env.example` → `.env` and fill secrets/endpoints.
   - Replace `site-pack/.secrets/influx.*` with real admin creds/tokens.
   - Fill `site-pack/.secrets/.env.grafana` and (if needed) `.env.smtp` for Grafana auth/SMTP.
   - (Optional) Copy `site-pack/lorawan.env.example` → `site-pack/lorawan.env` and fill ChirpStack admin/tenant/app vars so the init overlay can create an API key/app.
   - Copy a real `site-config.yml`, profiles, and any registries into `site-pack/storage/`.

2) Prep ChirpStack stack:
   - `cd edge-stacks/lorawan && git clone https://github.com/chirpstack/chirpstack-docker.git .`
   - Optionally prepare an `.env` there with `CHIRPSTACK_*` values if you want the init overlay.
   - If you use a `chirpstack.toml.template`, generate `chirpstack.toml` with a fresh secret before starting.
   - Move the repo under `edge-stacks/lorawan` so `bootstrap-lite.sh` can find it.
   - `bootstrap-lite.sh` will call `scripts/init-chirpstack-config.sh` once to generate `chirpstack.toml` if missing.
   - If `docker-compose.yml` is missing, `bootstrap-lite.sh` will auto-clone `chirpstack-docker` into `edge-stacks/lorawan`.
   - `bootstrap-lite.sh` will run `scripts/cs-seed.py` if `site-pack/storage/chirpstack-devices.yml` exists and `site-pack/.secrets/chirpstack.api_key` is present (uses gateway EUI from `site-pack/.secrets/gateway_eui` if available).
   - If `site-pack/lorawan.env` exists, `bootstrap-lite.sh` copies it to `edge-stacks/lorawan/.env` so the init overlay picks up your admin/tenant vars automatically.

3) Bring everything up (creates shared network automatically):
   - `./scripts/bootstrap-lite.sh`
   - Order: data → lorawan → app. If the overlay is present and env is set, `cs-init` writes `chirpstack.api_key` + `chirpstack.app_id` to `site-pack/.secrets`.

4) site-agent image:
   - `edge-stacks/app` defaults to `SITE_AGENT_IMAGE=ghcr.io/your-org/site-agent:latest`.
   - Use `scripts/package-site-agent.sh` if you want a tarball to scp instead of giving the IPC git access.

Backups / restore
-----------------
- Treat `site-pack/storage/control-registry.json` and `site-pack/storage/provision-registry.json` as runtime state you want to keep; after UI changes, copy them back into your per-site seed repo.
- If `miners.db` is absent on a fresh box, the agent can seed ranges from `site-config.yml`; still back up `miners.db` if you rely on UI edits.
- `.env` and `.secrets/` never belong in git; scp them per-site when provisioning.

Notes
-----
- The old `readme` file is kept for history, but this `README.md` is the canonical layout going forward.
- The shared Docker bits rely on the external network `edge_core`; `scripts/bootstrap-lite.sh` will create it if missing.
- Miner discovery: `bootstrap-lite.sh` installs `masscan` on the host (not in the container) and applies `setcap cap_net_raw,cap_net_admin=eip /usr/bin/masscan`.
- LoRa gateway: install a udev rule to expose the Heltec as `/dev/ht1303-0`, build `sx1302_hal`, then run `setup-lora-gateway.sh` (bootstrap calls it if installed). Install `scripts/lora-pkt-fwd.service` into `/etc/systemd/system/` if you want the packet forwarder on boot.
- ChirpStack seeding: provide `site-pack/storage/chirpstack-devices.yml`, `site-pack/.secrets/chirpstack.api_key`, and (optionally) `site-pack/.secrets/gateway_eui` to auto-create gateway/app/devices.
  - Optional: set `CHIRPSTACK_DEFAULT_APP_KEY` if you want a single app key used for any device missing an explicit one.
- Suggested udev rule (edit VID/PID if different):
  ```
  sudo tee /etc/udev/rules.d/80-heltec-ht1303.rules >/dev/null <<'EOF'
  SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", SYMLINK+="ht1303-0"
  EOF
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```
- Config separation guideline:
  - `site-config.yml` for versioned topology/defaults,
  - `.env` for secrets/endpoints,
  - `registry_*.json` + `miners.db` for runtime state you back up after UI edits.
