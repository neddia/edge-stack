Site-agent stack
================

- The compose uses `SITE_AGENT_IMAGE` (defaults to `ghcr.io/your-org/site-agent:latest`). Override when running: `SITE_AGENT_IMAGE=yourimage:tag docker compose up -d`.
- It mounts `../../site-pack/.env` for env vars, `../../site-pack/.secrets` (for `/secrets/influx.token`, optional ChirpStack API key, etc.), and `../../site-pack/storage` as `/app/storage` (rw) so registries and configs persist on the host.

If you don’t have a published image yet, build one from the site-agent repo and set `SITE_AGENT_IMAGE` accordingly, or package the app to the host and run it via systemd instead of this compose.
