### Deploy to AWS EC2 (Cloudflare Tunnel — recommended)

This is the supported production path: a `t4g.nano` with **no public IPv4** (avoids AWS’s ~$3.65/month address fee), plus a **Cloudflare Tunnel** so IPv4 and IPv6 visitors both reach the status page. Cloudflare terminates HTTPS; `cloudflared` dials out to Cloudflare and forwards to `app:8080` inside Docker. No inbound HTTP/S ports are required on the instance.

Afterwards, the box refreshes to the latest CurriePing from git every 15 minutes.

#### 1. Create the tunnel (Cloudflare Zero Trust)

1. Sign in at [one.dash.cloudflare.com](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels** → **Create a tunnel**.
2. Choose **Cloudflared** → name it e.g. `currieping` → **Save**.
3. Pick the **Docker** install tab and copy the token (the long string after `TUNNEL_TOKEN=` / in `cloudflared tunnel run --token …`).
4. Under **Public Hostname** add:
   - **Subdomain / Domain:** your `STATUS_DOMAIN` (e.g. `status` + `example.com`)
   - **Service:** `http://app:8080`  
     (`app` is the Compose service name; cloudflared resolves it on the Docker network.)
5. **DNS — the hostname’s zone should be on Cloudflare (Full Setup).**  
   A bare OpenSRS/Route53 CNAME to `<UUID>.cfargotunnel.com` is **not** enough for public browsers: that name only resolves to a private `fd10:` address, so `curl` / Chrome fail with “could not resolve host”.

   - **Recommended:** Add the domain in Cloudflare → copy existing records → switch the registrar nameservers to Cloudflare. Then create a **proxied (orange cloud) CNAME**:
     - name: `status` (or your host)
     - target: `<TUNNEL_UUID>.cfargotunnel.com`
   - **Alternative:** Put the status page on a hostname already in Cloudflare (e.g. `status.example-on-cf.com`) and point the tunnel’s public hostname there.
   - **Partial / CNAME setup** (domain stays on OpenSRS nameservers) is a paid Cloudflare feature and uses `hostname.cdn.cloudflare.net` targets — see Cloudflare’s Tunnel FAQ.

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

# 2. Default VPC + IPv6 (outbound + optional SSH). No public IPv4.
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
aws ec2 modify-subnet-attribute --region $REGION \
  --subnet-id "$SUBNET" --no-map-public-ip-on-launch

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

# 3. Security group: SSH on IPv6 only. No 80/443 — the tunnel is outbound.
SG=$(aws ec2 create-security-group --region $REGION \
  --group-name currieping --description "CurriePing (Cloudflare Tunnel)" \
  --vpc-id "$VPC" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region $REGION \
  --group-id "$SG" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,Ipv6Ranges=[{CidrIpv6=::/0}]"
aws ec2 authorize-security-group-egress --region $REGION \
  --group-id "$SG" --ip-permissions \
  "IpProtocol=-1,Ipv6Ranges=[{CidrIpv6=::/0}]" 2>/dev/null || true
aws ec2 revoke-security-group-egress --region $REGION --group-id "$SG" \
  --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]' 2>/dev/null || true

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
  -e 's|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=tunnel|' \\
  -e 's|^CLOUDFLARE_TUNNEL_TOKEN=.*|CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}|' \\
  .env
docker compose up -d --build --remove-orphans
cp scripts/auto-update.sh /usr/local/bin/currieping-auto-update
chmod +x /usr/local/bin/currieping-auto-update
echo '*/15 * * * * root /usr/local/bin/currieping-auto-update' \\
  > /etc/cron.d/currieping-auto-update
chmod 644 /etc/cron.d/currieping-auto-update
EOF

# 5. Launch without a public IPv4 / Elastic IP.
AMI=$(aws ssm get-parameter --region $REGION \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)
INSTANCE_ID=$(aws ec2 run-instances --region $REGION \
  --image-id "$AMI" --instance-type t4g.nano \
  --key-name "$KEYPAIR" \
  --network-interfaces "DeviceIndex=0,SubnetId=$SUBNET,Groups=$SG,AssociatePublicIpAddress=false,Ipv6AddressCount=1" \
  --user-data file://user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=currieping}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region $REGION --instance-ids "$INSTANCE_ID"
IPV6=$(aws ec2 describe-instances --region $REGION --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].NetworkInterfaces[0].Ipv6Addresses[0].Ipv6Address' \
  --output text)
PRIVATE_IP=$(aws ec2 describe-instances --region $REGION --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
echo "Instance $INSTANCE_ID  IPv6=$IPV6  private=$PRIVATE_IP"
echo "Public access is via Cloudflare Tunnel → https://$STATUS_DOMAIN"
```

#### 3. Verify

```bash
curl -I "https://$STATUS_DOMAIN"
docker compose -f /opt/currieping/docker-compose.yml ps   # on the host: app + tunnel (+ proxy)
```

SSH (laptop needs IPv6), or jump through another VPC host that still has a public IPv4:

```bash
ssh -6 -i /path/to/key.pem ubuntu@$IPV6

# IPv4-only laptop → jump host → private IP
ssh -i /path/to/key.pem \
  -o "ProxyCommand=ssh -i /path/to/key.pem -W %h:%p ubuntu@JUMP_PUBLIC_IPV4" \
  ubuntu@$PRIVATE_IP
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

sudo docker compose up -d --build --remove-orphans
sudo docker compose ps
sudo docker compose logs tunnel --tail=50
```

3. Optional hardening: revoke SG inbound TCP 80/443 (tunnel does not need them). Keep SSH as you prefer.

### On the Ubuntu host after login

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

cd /opt/currieping   # or: cd /opt/curieping
sudo docker compose ps
sudo docker compose logs -f --tail=100
```

### Alternative: direct IPv6 only (no Cloudflare)

Skip the tunnel. Publish an **AAAA** to the instance IPv6, keep SG 80/443 open on `::/0`, set `STATUS_DOMAIN` for Caddy/Let’s Encrypt, leave `COMPOSE_PROFILES` empty. IPv4-only clients cannot open the page. Do **not** add a NAT Gateway just for IPv4 visitors — the tunnel is cheaper.

-----

**Why tunnel:** AWS bills every public IPv4 (~$3.65/month). Auto-assigned (non-Elastic) IPv4 costs the same. Cloudflare Tunnel gives IPv4+IPv6 visitors with no public address on the VM. Outbound probes still need dual-stack/IPv6 targets (or IPv6 reachability to them); do not add NAT64/NAT Gateway for that.
