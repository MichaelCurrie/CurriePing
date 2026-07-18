# CurriePing

A tiny self-hosted uptime monitor with a status page in the style of [status.claude.com](https://status.claude.com). It **pings the sites itself** on an interval and stores results in SQLite.

| Tool | Subscription cost / month | Max sites monitored | Hosting cost / month |
|---|---|---|---|
| [Better Stack](https://betterstack.com/pricing) | **$29** | 50 | — |
| [StatusCake](https://www.statuscake.com/pricing/) | **~$20** | 100 | — |
| [Pingdom](https://www.pingdom.com/pricing/) | **$15** | 10 | — |
| [UptimeRobot](https://uptimerobot.com/pricing/) | **$9** | 50 | — |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | **$0** | unlimited | ~1 GB RAM → `t4g.micro` ~ **$6** |
| **CurriePing** | **$0** | **unlimited** | 0.5 GB RAM → `t4g.nano` ~ **$3** |

## Example

https://status.michaelcurrie.com

## How to Deploy

###  Via one-shot LLM prompt

Paste into an LLM agent (Claude Code, etc.):

```text
Read https://github.com/MichaelCurrie/CurriePing and deploy it.
```

## Deploy on a fresh AWS box (public HTTPS via your own domain)

Run these on your laptop (needs the AWS CLI, logged in). They create a `t4g.nano`, install everything, and start the stack.

1. Pick a region and your existing EC2 key pair:
   ```bash
   REGION=us-west-1
   KEYPAIR=my-keypair
   ```
2. Create a security group that allows SSH + HTTP + HTTPS:
   ```bash
   VPC=$(aws ec2 describe-vpcs --region $REGION \
     --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
   SG=$(aws ec2 create-security-group --region $REGION \
     --group-name currieping --description "status page" \
     --vpc-id $VPC --query GroupId --output text)
   for p in 22 80 443; do
     aws ec2 authorize-security-group-ingress --region $REGION \
       --group-id $SG --protocol tcp --port $p --cidr 0.0.0.0/0
   done
   ```
3. Write the boot script:
   ```bash
   cat > user-data.sh <<'EOF'
   #!/bin/bash
   set -eux
   apt-get update
   apt-get install -y docker.io docker-compose-v2 git
   systemctl enable --now docker
   git clone https://github.com/MichaelCurrie/CurriePing.git /opt/currieping
   cd /opt/currieping
   cp .env.example .env
   docker compose up -d --build
   cp scripts/auto-update.sh /usr/local/bin/currieping-auto-update
   chmod +x /usr/local/bin/currieping-auto-update
   echo '*/15 * * * * root /usr/local/bin/currieping-auto-update' \
     > /etc/cron.d/currieping-auto-update
   chmod 644 /etc/cron.d/currieping-auto-update
   EOF
   ```
4. Launch the instance:
   ```bash
   AMI=$(aws ssm get-parameter --region $REGION \
     --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
     --query 'Parameter.Value' --output text)
   aws ec2 run-instances --region $REGION \
     --image-id $AMI --instance-type t4g.nano \
     --key-name $KEYPAIR --security-group-ids $SG \
     --user-data file://user-data.sh \
     --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=currieping}]'
   ```
5. Point DNS at the hostname with cloudflared (Cloudflare proxy **off** — DNS only / grey cloud; Caddy needs a direct path to issue the cert):
   ```bash
   cloudflared tunnel route dns currieping status.example.com
   ```
6. SSH in, set your real domain and targets, then reload:
   ```bash
   cd /opt/currieping
   sudo nano .env      # set STATUS_DOMAIN=status.example.com and TARGETS
   sudo docker compose up -d      # Caddy fetches the HTTPS cert
   ```
7. Install the auto-update cron (pulls `main` when GitHub moves, rebuilds only then):
   ```bash
   sudo cp /opt/currieping/scripts/auto-update.sh /usr/local/bin/currieping-auto-update
   sudo chmod +x /usr/local/bin/currieping-auto-update
   echo '*/15 * * * * root /usr/local/bin/currieping-auto-update' \
     | sudo tee /etc/cron.d/currieping-auto-update
   sudo chmod 644 /etc/cron.d/currieping-auto-update
   ```
   If the clone lives somewhere else (e.g. `/opt/curieping`), set that path in the cron line:
   `*/15 * * * * root CURRIEPING_DIR=/opt/curieping /usr/local/bin/currieping-auto-update`

## Updating

Manual:

```bash
cd /opt/currieping && sudo git pull && sudo docker compose up -d --build
```

Or rely on the cron from deploy step 7 — every 15 minutes it `git fetch`es, and only if `origin/main` advanced does it `git reset --hard origin/main` and `docker compose up -d --build`. The deploy host tracks GitHub exactly; untracked files (`.env`) are kept. No-op runs are silent; real updates append to `/var/log/currieping-auto-update.log`.

History persists across restarts in the `status-data` Docker volume.

## Configuration reference

Everything lives in `.env` (copy from `.env.example`):

| Variable | Meaning |
|---|---|
| `TARGETS` | Comma-separated `Name=URL` pairs, e.g. `example.com=https://example.com,API=https://api.example.com`. The only required setting. |
| `STATUS_DOMAIN` | Public hostname → Caddy gets an HTTPS cert. Blank → plain HTTP on :80 (local, or behind a tunnel). |
| `STATUS_TITLE` | Page heading (default `Service Status`). |
| `CHECK_INTERVAL_SECONDS` | Probe frequency (default 60). |
| `REQUEST_TIMEOUT_SECONDS` | Per-request timeout (default 10). |
| `HISTORY_DAYS` | Days of history shown (default 90). |

A site counts as **up** when it returns any HTTP status below 400 within the timeout (a `301` redirect counts as up). Connection failures, TLS errors, timeouts, and 4xx/5xx count as down. JSON API is at `/api/status`; health at `/healthz`.
