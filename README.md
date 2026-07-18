# CurriePing

A tiny self-hosted uptime monitor with a status page in the style of
[status.claude.com](https://status.claude.com) — thin colored bars, one row per site, 90 days of history. It **pings the sites itself** on an interval and stores results in SQLite. The only thing you configure is a list of URLs in `.env`.

Designed to run on a *very* small box (a t4g.nano is plenty). Two containers:

* a Python (Flask + waitress) app and 
* [Caddy](https://caddyserver.com) in front for automatic HTTPS.

```
┌ Service Status ─────────────────────────────────────────┐
│  ● All Systems Operational                               │
│                                                          │
│  microsoft.com                     Operational · 100%    │
│  ▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏  (green bars)   │
│  90 days ago                                      Today  │
│                                                          │
│  api.google.com                    Operational · 99.8%   │
│  ▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏▏  (mostly green) │
└──────────────────────────────────────────────────────────┘
```

## Configuration

Everything is set in `.env` (copy `.env.example`):

| Variable | Meaning |
|---|---|
| `TARGETS` | Comma-separated `Name=URL` pairs. This is the only required setting. |
| `STATUS_DOMAIN` | Public hostname. Set it → Caddy gets an HTTPS cert. Blank → plain HTTP on :80. |
| `STATUS_TITLE` | Page heading. |
| `CHECK_INTERVAL_SECONDS` | Probe frequency (default 60). |
| `REQUEST_TIMEOUT_SECONDS` | Per-request timeout (default 10). |
| `HISTORY_DAYS` | Days of history shown (default 90). |

A site counts as **up** when it returns any HTTP status below 400 within the timeout (so a `301` redirect counts as up). Connection failures, TLS errors, timeouts, and 4xx/5xx count as down.

## Run locally

```bash
cp .env.example .env
# edit TARGETS; leave STATUS_DOMAIN blank for local
docker compose up -d --build
```

Open <http://localhost>. JSON API is at `/api/status`; health at `/healthz`.

## Deploy on a fresh AWS instance (one-off)

This repo is public, so the instance just clones it in the clear — the only secret is `.env`, which never leaves the box.

### 1. Security group (allow SSH + HTTP + HTTPS)

```bash
REGION=us-west-1
VPC=$(aws ec2 describe-vpcs --region $REGION \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)

SG=$(aws ec2 create-security-group --region $REGION \
  --group-name status-monitor --description "status page" \
  --vpc-id $VPC --query GroupId --output text)

for p in 22 80 443; do
  aws ec2 authorize-security-group-ingress --region $REGION \
    --group-id $SG --protocol tcp --port $p --cidr 0.0.0.0/0
done
```

### 2. Launch a t4g.nano (ARM64 Ubuntu 24.04)

`user-data.sh` installs Docker, clones this repo, and brings the stack up:

```bash
cat > user-data.sh <<'EOF'
#!/bin/bash
set -eux
apt-get update
apt-get install -y docker.io docker-compose-v2 git
systemctl enable --now docker
git clone https://github.com/MichaelCurrie/CurriePing.git /opt/status-monitor
cd /opt/status-monitor
cp .env.example .env
docker compose up -d --build
EOF

# Latest Ubuntu 24.04 ARM64 AMI from the public SSM parameter:
AMI=$(aws ssm get-parameter --region $REGION \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)

aws ec2 run-instances --region $REGION \
  --image-id $AMI --instance-type t4g.nano \
  --key-name <YOUR_KEYPAIR> --security-group-ids $SG \
  --user-data file://user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=status-monitor}]'
```

> **Sizing:** t4g.nano (0.5 GB) runs the app fine but the on-box `docker build`
> can be tight. If the first build OOMs, use `t4g.micro` (1 GB, ~$6/mo) or add a
> small swapfile.

### 3. Point DNS at it

Allocate an Elastic IP, associate it with the instance, then create an
**A record** `status.example.com → <Elastic IP>`.

> **IPv4 only — do not add an AAAA record.** IPv6 can be broken easily, while its IPv4-only sibling stays up.

### 4. Set the real targets

SSH in and edit `.env`:

```bash
cd /opt/status-monitor
sudo nano .env      # set STATUS_DOMAIN=status.example.com and the real TARGETS
sudo docker compose up -d      # re-reads .env; Caddy then fetches the HTTPS cert
```

See `.env.example` for the `TARGETS` format — a comma-separated list of
`Name=URL` pairs (or bare URLs), e.g.:

```
TARGETS=example.com=https://example.com,API=https://api.example.com
```

## Updating

```bash
cd /opt/status-monitor && git pull && sudo docker compose up -d --build
```

History persists across restarts in the `status-data` Docker volume.
