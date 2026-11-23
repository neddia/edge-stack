# pid-infra (Aug 2025): notes

This is the first version of the edge stack. It's kept exactly as it was
written, with its original commits. These notes say what it was trying to do,
what held up and what didn't. Nothing here was fixed after the fact.

## What it did

`scripts/firstboot-provision.sh` ran once on a fresh Ubuntu install:
1. Hardened SSH (no root, no passwords) and turned on automatic security updates.
2. Brought up Tailscale with retries. **It only turned on the firewall once
   Tailscale was confirmed up**, so a failed join couldn't lock the box out.
   `pid-failsafe.timer` checked again 10 minutes after boot.
3. Installed Docker and started InfluxDB, ChirpStack, Mosquitto and Postgres.
4. Created a separate InfluxDB write token and ChirpStack tenant for each site.
5. Enabled a daily self-test (`pid-selftest.sh`). It checked Tailscale, the
   firewall, each container, an InfluxDB write-then-read round trip and the
   file permissions on secrets. It saved the results as JSON and as a metric.
6. Enabled an agent that deployed a tagged app release into its own venv and
   switched over by repointing a symlink.

## What went wrong (found in a later review)

- **Credentials got wiped.** Both init scripts create their output files with
  `install -m 0640 /dev/null FILE`, which empties an existing file.
  `init-chirpstack.sh` runs second, so it erased the InfluxDB admin password
  and the tokens that `init-influx.sh` had just saved.
- **The firewall didn't cover the services.** UFW only allowed the Tailscale
  interface, but Docker publishes ports through its own iptables rules, which
  bypass UFW. The Compose ports needed to be bound to an address.
- **The stack wasn't complete.** The Compose file mounts `./chirpstack` and
  `./mosquitto` config folders that were never committed. Nothing passed the
  generated Postgres password to ChirpStack. The "rm duplicate" commit deleted
  the only unit that ran the first-boot script.
- **Smaller issues:**
  - The self-test looked for a firewall rule that's never created, so its
    firewall check always warned.
  - The deploy agent restarted the app every 45 minutes even with nothing new.
  - The `tools/` scripts run `find ..`, so they edit files one folder above
    wherever they're run.
  - The README stops partway through.

The later version split the setup into separate Compose stacks with init
containers (the rest of this repo). ipc-stack then rebuilt the provisioning and
health checks with tests.
