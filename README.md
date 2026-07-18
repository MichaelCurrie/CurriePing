# CurriePing

A tiny self-hosted uptime monitor with a status page in the style of [status.claude.com](https://status.claude.com) — thin colored bars, one row per site, 90 days of history. It **pings the sites itself** on an interval and stores results in SQLite. You configure one thing: a list of URLs.

Runs on the smallest cloud box there is (a `t4g.nano`, 0.5 GB RAM) for a flat **~$2–3/month**, monitoring **unlimited** sites.

## How it compares

The paid services host everything for you but charge **per monitor**. The self-hosted tools are free to license — you just pay for the box they run on.

| Tool | Monthly price | Max sites monitored | Monthly hosting cost |
|---|---|---|---|
| [Pingdom](https://www.pingdom.com/pricing/) | **$15** | 10 | — |
| [UptimeRobot](https://uptimerobot.com/pricing/) | **$9** | 50 | — |
| [Better Stack](https://betterstack.com/pricing) | **$29** | 50 | — |
| [StatusCake](https://www.statuscake.com/pricing/) | **~$20** | 100 | — |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | **$0** | unlimited | ~1 GB RAM → `t4g.micro` ~ **$6** |
| **CurriePing** | **$0** | **unlimited** | 0.5 GB RAM → `t4g.nano` ~ **$2–3** |

Prices as of July 2026; SaaS tiers are billed-annually starting rates. `t4g.nano` is ~$3.07/mo on-demand, ≈$2/mo on a 1-year Savings Plan.

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

## One-shot LLM prompt (Cloudflare Tunnel, no open ports)

Prefer to let an LLM agent (Claude Code, etc.) do the whole thing — and skip the Elastic IP, A record, and open 80/443? Paste the block below into your LLM. It uses a **Cloudflare Tunnel**, so the box needs no public inbound ports and no static IP; Cloudflare terminates TLS and routes `status.contoso.com` to it.

> **Before you run it:** replace `status.contoso.com` with your hostname (its zone must already be in your Cloudflare account), have the AWS CLI and `cloudflared` authenticated, and set your EC2 key pair name.

````text
You are deploying CurriePing, a tiny self-hosted uptime/status-page monitor (https://github.com/MichaelCurrie/CurriePing). It is two Docker containers: a Flask app and Caddy. With STATUS_DOMAIN left blank, Caddy serves the page as plain HTTP on port 80 — perfect behind a Cloudflare Tunnel, which handles HTTPS.

Goal: run it on a t4g.nano in AWS and expose it at https://status.contoso.com through a Cloudflare Tunnel. Do not open ports 80/443 to the internet; SSH only.

Do this:

1. Create a security group allowing ONLY SSH (port 22):
```bash
REGION=us-west-1; KEYPAIR=my-keypair
VPC=$(aws ec2 describe-vpcs --region $REGION --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --region $REGION --group-name currieping --description "status page" --vpc-id $VPC --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region $REGION --group-id $SG --protocol tcp --port 22 --cidr 0.0.0.0/0
```

2. Launch an Ubuntu 24.04 ARM64 t4g.nano with this user-data (installs Docker + cloudflared, clones the repo, starts the app on localhost:80, then opens the tunnel and routes the hostname):
```bash
#!/bin/bash
set -eux
apt-get update && apt-get install -y docker.io docker-compose-v2 git curl
systemctl enable --now docker
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
git clone https://github.com/MichaelCurrie/CurriePing.git /opt/currieping
cd /opt/currieping && cp .env.example .env
# leave STATUS_DOMAIN blank so Caddy serves plain HTTP on :80
docker compose up -d --build

AMI=$(aws ssm get-parameter --region $REGION --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query 'Parameter.Value' --output text)
aws ec2 run-instances --region $REGION --image-id $AMI --instance-type t4g.nano --key-name $KEYPAIR --security-group-ids $SG --user-data file://user-data.sh --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=currieping}]'
```

3. Set up the Cloudflare Tunnel. Authenticate, create a named tunnel, point the hostname at the local app, and run it as a service (do this on the box, or use a tunnel token from the Cloudflare Zero Trust dashboard):
```bash
cloudflared tunnel login
cloudflared tunnel create currieping
cloudflared tunnel route dns currieping status.contoso.com
# config.yml: ingress rule -> service: http://localhost:80
cloudflared tunnel run currieping        # or: cloudflared service install <TOKEN>
```

4. SSH to the box, edit /opt/currieping/.env to set the real TARGETS (comma-separated Name=URL pairs), then:
```bash
sudo docker compose up -d
```

Report back the instance ID, public hostname, and the final URL.
````

## Updating

```bash
cd /opt/currieping && git pull && sudo docker compose up -d --build
```

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
