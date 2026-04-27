# Deployment

Production runs on a Contabo Ubuntu 24.04 VPS as a systemd service under
the `kotak` user.

## Files
- `kotak.service` — systemd unit. Source of truth; copy to
  `/etc/systemd/system/kotak.service` on the VPS.

## Update workflow
After editing `deploy/kotak.service`:

```bash
sudo cp /home/kotak/kotak-dashboard/deploy/kotak.service /etc/systemd/system/kotak.service
sudo systemctl daemon-reload
sudo systemctl restart kotak
sudo systemctl is-active kotak
```

## Code-only updates (no unit change)
```bash
cd /home/kotak/kotak-dashboard && git pull && sudo systemctl restart kotak
```

## Why ReadWritePaths includes the repo root
`config.yaml` lives at the repo root and is mutated from the `/config`
web UI via an atomic write (`tmp + rename`), which requires write access
to the parent directory. `data/` alone is not enough.
