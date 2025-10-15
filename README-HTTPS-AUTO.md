# HTTPS automation with Docker (no external tools)

This update lets **Docker generate self-signed TLS certs automatically** at runtime, so a user can simply run:

```bash
docker compose up -d
```

and get the app on:

```
https://<host-or-ip>:3443/
https://<host-or-ip>:3443/broadcaster
https://<host-or-ip>:3443/viewer
```

## How it works

- An init-like service **`certgen`** runs `/app/bin/generate-certs.sh` inside the image.
- It creates `/app/certs/server.key` and `/app/certs/server.crt` with **SubjectAltName** built from `CERT_HOSTNAMES`.
- The main service mounts the same named volume `certs:` (so keys are not baked into the image).

## Configure hostnames/IPs (optional)

By default, SANs are: `localhost 127.0.0.1`. To add your LAN IP or mDNS name:

- Edit `docker-compose.yml` (`certgen.environment.CERT_HOSTNAMES`), **or**
- Override at runtime:

```bash
CERT_HOSTNAMES="localhost 127.0.0.1 192.168.1.9" docker compose up -d --build
```

## Security notes

- Private key lives only in the **named volume** `certs:` and is **not** committed, nor baked into the image.
- The volume is mounted read-only where appropriate in production scenarios (you can adjust to `:ro` once generated).
