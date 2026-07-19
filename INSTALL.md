### Deploy to AWS EC2 (Cloudflare Tunnel — recommended)

This is the supported production path: a `t4g.nano` plus a **Cloudflare Tunnel** so IPv4 and IPv6 visitors both reach the status page. Cloudflare terminates HTTPS; `cloudflared` dials out to Cloudflare and forwards to `app:8080` inside Docker. No inbound HTTP/S ports are required on the instance.

Afterwards, the box refreshes to the latest CurriePing from git every 15 minutes.

#### Choose probe mode (`CHECK_IPV4`)

CurriePing **always** runs HTTP checks over **IPv6**. Whether it also checks **IPv4** is controlled by `.env`. The value must be exactly `True` or `False` (required; anything else, or a missing key, fails startup).

| `.env` | EC2 networking | Status page checkboxes | Outbound probes | Extra AWS cost |
|---|---|---|---|---|
| `CHECK_IPV4=False` | **IPv6-only** — no public IPv4 | IPv6 ✓ · IPv4 ☐ | IPv6 only | none |
| `CHECK_IPV4=True` | **Elastic IP** (public IPv4) + IPv6 | IPv6 ✓ · IPv4 ✓ | IPv4 + IPv6 | ~$3.65/mo for the public IPv4 |

A site is marked red (down) if **any probed family** fails. The LIVE label then spells out which family failed (e.g. `IPv6 up · IPv4 down`).

Visitors still use the Cloudflare Tunnel either way — the Elastic IP is only so the **monitor** can dial IPv4 targets. Do **not** add a NAT Gateway just for probes; an Elastic IP on the instance is the cheap dual-stack option.

Pick one value and keep it consistent in the launch script below (`CHECK_IPV4=False` or `True`).

#### 1. Create the tunnel (Cloudflare Zero Trust)

1. Sign in at [one.dash.cloudflare.com](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels** → **Create a tunnel**.
2. Choose **Cloudflared** → name it e.g. `currieping` → **Save**.
3. Pick the **Docker** install tab and copy the token (the long string after `TUNNEL_TOKEN=` / in `cloudflared tunnel run --token …`).
4. Under **Public Hostname** add:
   - **Subdomain / Domain:** your `STATUS_DOMAIN` (e.g. `status` + `example.com`)
   - **Service:** `http://app:8080`  
     (`app` is the Compose service name; cloudflared resolves it on the Docker network.)
5. **DNS — put the zone on Cloudflare (Full Setup).**  
   A bare OpenSRS/Route53 CNAME to `<UUID>.cfargotunnel.com` is **not** enough for public browsers: that name only resolves to a private `fd10:` address, so `curl` / Chrome fail with “could not resolve host”. The hostname must be an **orange-cloud (proxied)** record in a Cloudflare zone so resolvers get Cloudflare’s public anycast **A/AAAA** (IPv4-only clients use the A).

   Checklist (OpenSRS → Cloudflare example):

   1. Cloudflare dashboard → **Add site** → `example.com` (Free is fine).
   2. Import/recreate every existing record (apex/www/mail A, MX, TXT, DKIM CNAMEs, etc.). Keep mail/DKIM **DNS only** (grey cloud).
   3. Add **CNAME** `status` → `<TUNNEL_UUID>.cfargotunnel.com` with **Proxy ON** (orange cloud).
   4. At the registrar (OpenSRS **Name Servers**), replace `ns*.systemdns.com` with the two Cloudflare nameservers shown in the dashboard (e.g. `aliza.ns.cloudflare.com` / `melnicoff.ns.cloudflare.com`).
   5. Wait until the zone status is **Active** and Universal SSL finishes (HTTPS may  fail for a few minutes while the cert issues).
   6. Verify from an IPv4-only machine (disable WARP/VPN first):

      ```bash
      nslookup -type=A status.example.com 1.1.1.1   # expect a public Cloudflare A, not fd10:
      curl -4 -I https://status.example.com          # expect HTTP/2 or HTTP/1.1 200
      ```

   If your laptop’s DHCP DNS is still the home router, it may keep serving a stale `fd10:` answer after the cutover. Point the NIC at `1.1.1.1` / `1.0.0.1` (or `8.8.8.8`) and `Clear-DnsClientCache` (Windows) / flush DNS.

   **IPv6-only EC2 note:** `docker-compose.yml` runs `cloudflared` with `--edge-ip-version 6 --protocol http2` so the connector can reach Cloudflare without a public IPv4 on the instance. That still works when you also attach an Elastic IP.

#### 2. Launch the EC2 host

Install the `aws` CLI and authenticate. You need a key pair in the region. Then on your local machine:

```bash
# 1. Pick settings
REGION=ap-southeast-1
KEYPAIR=my-keypair
STATUS_DOMAIN=status.example.com
STATUS_TITLE='My Service Status'
TARGETS='microsoft=https://www.microsoft.com,google=https://www.google.com'
# Token from Zero Trust → Tunnels → your tunnel → Docker
CLOUDFLARE_TUNNEL_TOKEN='eyJ...'

# Probe mode (must be exactly True or False):
#   False = cheapest IPv6-only host
#   True  = attach an Elastic IP so CHECK_IPV4 probes work (~$3.65/mo)
CHECK_IPV4=False

# 2. Default VPC + IPv6 (outbound + optional SSH).
VPC=$(aws ec2 describe-vpcs --region $REGION \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)

IPV6_ASSOC=$(aws ec2 describe-vpcs --region $REGION --vpc-ids "$VPC" \
  --query 'Vpcs[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlock' --output text)
if [ "$IPV6_ASSOC" = "None" ] || [ -z "$IPV6_ASSOC" ]; then
  aws ec2 associate-vpc-cidr-block --region $REGION \
    --vpc-id "$VPC" --amazon-provided-ipv6-cidr-block >/dev/null
  for _ in $(seq 1 36); do
    IPV6_ASSOC=$(aws ec2 describe-vpcs --region $REGION --vpc-ids "$VPC" \
      --query 'Vpcs[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlock' --output text)
    STATE=$(aws ec2 describe-vpcs --region $REGION --vpc-ids "$VPC" \
      --query 'Vpcs[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlockState.State' --output text)
    [ "$STATE" = "associated" ] && [ -n "$IPV6_ASSOC" ] && [ "$IPV6_ASSOC" != "None" ] && break
    sleep 5
  done
fi
SUBNET_IPV6="${IPV6_ASSOC%::/56}::/64"

SUBNET=$(aws ec2 describe-subnets --region $REGION \
  --filters Name=vpc-id,Values="$VPC" Name=default-for-az,Values=true \
  --query 'Subnets[0].SubnetId' --output text)

EXISTING_SUBNET_V6=$(aws ec2 describe-subnets --region $REGION --subnet-ids "$SUBNET" \
  --query 'Subnets[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlock' --output text)
if [ "$EXISTING_SUBNET_V6" = "None" ] || [ -z "$EXISTING_SUBNET_V6" ]; then
  aws ec2 associate-subnet-cidr-block --region $REGION \
    --subnet-id "$SUBNET" --ipv6-cidr-block "$SUBNET_IPV6"
fi
aws ec2 modify-subnet-attribute --region $REGION \
  --subnet-id "$SUBNET" --assign-ipv6-address-on-creation

# IPv6-only: do not auto-assign a public IPv4. Dual-stack: allow map-public-ip
# (we still prefer an Elastic IP below when CHECK_IPV4=True).
if [ "$CHECK_IPV4" = "True" ]; then
  aws ec2 modify-subnet-attribute --region $REGION \
    --subnet-id "$SUBNET" --map-public-ip-on-launch
else
  aws ec2 modify-subnet-attribute --region $REGION \
    --subnet-id "$SUBNET" --no-map-public-ip-on-launch
fi

IGW=$(aws ec2 describe-internet-gateways --region $REGION \
  --filters Name=attachment.vpc-id,Values="$VPC" \
  --query 'InternetGateways[0].InternetGatewayId' --output text)
RTB=$(aws ec2 describe-route-tables --region $REGION \
  --filters Name=association.subnet-id,Values="$SUBNET" \
  --query 'RouteTables[0].RouteTableId' --output text)
if [ "$RTB" = "None" ] || [ -z "$RTB" ]; then
  RTB=$(aws ec2 describe-route-tables --region $REGION \
    --filters Name=vpc-id,Values="$VPC" Name=association.main,Values=true \
    --query 'RouteTables[0].RouteTableId' --output text)
fi
aws ec2 create-route --region $REGION --route-table-id "$RTB" \
  --destination-ipv6-cidr-block ::/0 --gateway-id "$IGW" 2>/dev/null || true
# Dual-stack needs the usual IPv4 default route via the IGW (default VPC usually has it).
aws ec2 create-route --region $REGION --route-table-id "$RTB" \
  --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW" 2>/dev/null || true

# 3. Security group.
# IPv6-only: SSH + egress on ::/0; revoke IPv4 egress so nothing assumes 0.0.0.0/0.
# Dual-stack: keep IPv4 egress; optional SSH on 0.0.0.0/0 as well as ::/0.
SG=$(aws ec2 create-security-group --region $REGION \
  --group-name currieping --description "CurriePing (Cloudflare Tunnel)" \
  --vpc-id "$VPC" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id "$SG" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,Ipv6Ranges=[{CidrIpv6=::/0}]"
aws ec2 authorize-security-group-egress --region $REGION \
  --group-id "$SG" --ip-permissions \
  "IpProtocol=-1,Ipv6Ranges=[{CidrIpv6=::/0}]" 2>/dev/null || true

if [ "$CHECK_IPV4" = "True" ]; then
  aws ec2 authorize-security-group-ingress --region $REGION \
    --group-id "$SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}]" 2>/dev/null || true
  # Default SG already allows IPv4 egress; leave it.
else
  aws ec2 revoke-security-group-egress --region $REGION --group-id "$SG" \
    --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]' 2>/dev/null || true
fi

# 4. Boot script: OS updates, Docker, CurriePing + tunnel profile + auto-update.
cat > user-data.sh <<EOF
#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y docker.io docker-compose-v2 git unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades || true
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'DOCKER'
{
  "ipv6": true,
  "fixed-cidr-v6": "fd00:dead:beef:1::/64",
  "ip6tables": true
}
DOCKER
systemctl enable docker
systemctl restart docker
git clone https://github.com/MichaelCurrie/CurriePing.git /opt/currieping
cd /opt/currieping
cp .env.example .env
sed -i \\
  -e 's|^STATUS_DOMAIN=.*|STATUS_DOMAIN=${STATUS_DOMAIN}|' \\
  -e 's|^STATUS_TITLE=.*|STATUS_TITLE=${STATUS_TITLE}|' \\
  -e 's|^TARGETS=.*|TARGETS=${TARGETS}|' \\
  -e 's|^CHECK_IPV4=.*|CHECK_IPV4=${CHECK_IPV4}|' \\
  -e 's|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=tunnel|' \\
  -e 's|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}|' \\
  .env
docker compose up -d --build --remove-orphans
cp scripts/auto-update.sh /usr/local/bin/currieping-auto-update
chmod +x /usr/local/bin/currieping-auto-update
echo '*/15 * * * * root /usr/local/bin/currieping-auto-update' \\
  > /etc/cron.d/currieping-auto-update
chmod 644 /etc/cron.d/currieping-auto-update
# IPv6-only hosts cannot git-fetch GitHub (A-record only). Point origin at a
# dual-stack VPC sibling that mirrors the repo over HTTPS, e.g.:
#   git remote set-url origin ubuntu@172.31.x.x:/var/cache/currieping.git
EOF

# 5. Launch. IPv6-only: no public IPv4. Dual-stack: public IPv4 + Elastic IP.
AMI=$(aws ssm get-parameter --region $REGION \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)

if [ "$CHECK_IPV4" = "True" ]; then
  ASSOCIATE_PUBLIC=true
else
  ASSOCIATE_PUBLIC=false
fi

INSTANCE_ID=$(aws ec2 run-instances --region $REGION \
  --image-id "$AMI" --instance-type t4g.nano \
  --key-name "$KEYPAIR" \
  --network-interfaces "DeviceIndex=0,SubnetId=$SUBNET,Groups=$SG,AssociatePublicIpAddress=$ASSOCIATE_PUBLIC,Ipv6AddressCount=1" \
  --user-data file://user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=currieping}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region $REGION --instance-ids "$INSTANCE_ID"
IPV6=$(aws ec2 describe-instances --region $REGION --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].NetworkInterfaces[0].Ipv6Addresses[0].Ipv6Address' \
  --output text)
PRIVATE_IP=$(aws ec2 describe-instances --region $REGION --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)

PUBLIC_IPV4=""
if [ "$CHECK_IPV4" = "True" ]; then
  # Stable public IPv4 for outbound IPv4 probes (billed ~$3.65/mo while allocated).
  ALLOC=$(aws ec2 allocate-address --region $REGION --domain vpc \
    --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=currieping}]' \
    --query 'AllocationId' --output text)
  ENI=$(aws ec2 describe-instances --region $REGION --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId' \
    --output text)
  aws ec2 associate-address --region $REGION \
    --allocation-id "$ALLOC" --network-interface-id "$ENI"
  PUBLIC_IPV4=$(aws ec2 describe-addresses --region $REGION --allocation-ids "$ALLOC" \
    --query 'Addresses[0].PublicIp' --output text)
fi

echo "Instance $INSTANCE_ID  IPv6=$IPV6  private=$PRIVATE_IP  public_ipv4=${PUBLIC_IPV4:-none}"
echo "CHECK_IPV4=$CHECK_IPV4  (status page IPv4 checkbox follows this)"
echo "Public access is via Cloudflare Tunnel → https://$STATUS_DOMAIN"
```

#### 3. Verify

```bash
curl -I "https://$STATUS_DOMAIN"
docker compose -f /opt/currieping/docker-compose.yml ps   # on the host: app + tunnel (+ proxy)
# On the status page header: IPv6 should be checked; IPv4 only if CHECK_IPV4=True.
```

SSH:

```bash
# Always available over IPv6 (laptop needs IPv6), or jump through another host:
ssh -6 -i /path/to/key.pem ubuntu@$IPV6

# IPv4-only laptop → jump host → private IP
ssh -i /path/to/key.pem \
  -o "ProxyCommand=ssh -i /path/to/key.pem -W %h:%p ubuntu@JUMP_PUBLIC_IPV4" \
  ubuntu@$PRIVATE_IP

# Dual-stack (CHECK_IPV4=True): SSH directly to the Elastic IP
# ssh -i /path/to/key.pem ubuntu@$PUBLIC_IPV4
```

### Enabling the tunnel on an existing box

Install path is `/opt/currieping` for new deploys; older hosts may be `/opt/curieping` (`ls /opt`).

1. Create the tunnel + public hostname (`http://app:8080`) as in §1; point DNS with a **CNAME** to `<tunnel-id>.cfargotunnel.com` (remove old **A** / **AAAA**).
2. On the host:

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

APP_DIR=/opt/currieping
[ -d /opt/curieping ] && APP_DIR=/opt/curieping
cd "$APP_DIR"
sudo git pull   # once these changes are on the branch you deploy

# Ensure .env has the token and enables the compose profile
grep -q '^COMPOSE_PROFILES=' .env \
  && sudo sed -i 's|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=tunnel|' .env \
  || echo 'COMPOSE_PROFILES=tunnel' | sudo tee -a .env
grep -q '^CLOUDFLARE_TUNNEL_TOKEN=' .env \
  && sudo sed -i "s|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=YOUR_TOKEN_HERE|" .env \
  || echo 'CLOUDFLARE_TUNNEL_TOKEN=YOUR_TOKEN_HERE' | sudo tee -a .env

# Probe mode: must be exactly True or False (False on IPv6-only hosts)
grep -q '^CHECK_IPV4=' .env \
  && sudo sed -i 's|^CHECK_IPV4=.*|CHECK_IPV4=False|' .env \
  || echo 'CHECK_IPV4=False' | sudo tee -a .env

sudo docker compose up -d --build --remove-orphans
sudo docker compose ps
sudo docker compose logs tunnel --tail=50
```

3. Optional hardening: revoke SG inbound TCP 80/443 (tunnel does not need them). Keep SSH as you prefer.

### Switching an existing host to dual-stack probes

Only do this if you want the status page to check IPv4 as well (and accept the public IPv4 fee):

1. Allocate an Elastic IP and associate it with the instance’s primary ENI.
2. Ensure the security group allows **egress** to `0.0.0.0/0` (and an IPv4 default route via the IGW).
3. On the host:

```bash
cd /opt/currieping   # or /opt/curieping
sudo sed -i 's|^CHECK_IPV4=.*|CHECK_IPV4=True|' .env \
  || echo 'CHECK_IPV4=True' | sudo tee -a .env
sudo docker compose up -d --build --remove-orphans
```

4. Confirm the page header shows IPv4 ✓ and that IPv4-only targets flip to Operational (or show a mixed LIVE label if only one family fails).

### On the Ubuntu host after login

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

cd /opt/currieping   # or: cd /opt/curieping
sudo docker compose ps
sudo docker compose logs -f --tail=100
```

### Alternative: direct IPv6 only (no Cloudflare)

Skip the tunnel. Publish an **AAAA** to the instance IPv6, keep SG 80/443 open on `::/0`, set `STATUS_DOMAIN` for Caddy/Let’s Encrypt, leave `COMPOSE_PROFILES` empty. IPv4-only clients cannot open the page. Do **not** add a NAT Gateway just for IPv4 visitors — the tunnel is cheaper. Keep `CHECK_IPV4=False` unless you also attach a public IPv4 for probes.

-----

**Why tunnel:** AWS bills every public IPv4 (~$3.65/month). Auto-assigned (non-Elastic) IPv4 costs the same. Cloudflare Tunnel gives IPv4+IPv6 *visitors* with no public address on the VM. Outbound IPv4 *probes* still need a public IPv4 (or NAT) on the monitor host — set `CHECK_IPV4=True` and attach an Elastic IP only if you want that; otherwise leave `CHECK_IPV4=False` and monitor IPv6 paths only.
