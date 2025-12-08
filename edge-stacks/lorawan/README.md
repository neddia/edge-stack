ChirpStack stack
================

This folder expects the upstream ChirpStack Docker repo to be cloned here:

```bash
git clone https://github.com/chirpstack/chirpstack-docker.git .
```

You can then run the upstream compose directly:

```bash
docker compose up -d
```

Optional init overlay
---------------------

If you want an idempotent bootstrap that creates an application and API key, run with the overlay in this folder:

```bash
docker compose -f docker-compose.yml -f docker-compose.init.yml up -d
```

Provide admin creds and tenant info via environment (for example with an `.env` file alongside the compose):

```
CHIRPSTACK_URL=http://chirpstack:8080
CHIRPSTACK_TENANT_ID=REPLACE
CHIRPSTACK_APP_NAME=site-001
CHIRPSTACK_ADMIN_EMAIL=admin@example.com
CHIRPSTACK_ADMIN_PASSWORD=REPLACE
```

The `cs-init` service writes `chirpstack.api_key` and `chirpstack.app_id` into `site-pack/.secrets` (mounted as `/secrets`) for the agent to consume. If admin creds or tenant id are absent, `cs-init` exits quietly.
