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

Init overlay removed
--------------------

The HTTP init overlay has been removed. Use the CLI path in `scripts/bootstrap-lite.sh` to create the API key, then let `cs-seed.py` create the application/device profile/devices.
