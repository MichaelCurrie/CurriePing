### Deploy to AWS EC2

These commands launches a `t4g.nano` and installs CurriePing.

Afterwards, the box refreshes to the latest CurriePing from git every 15 minutes.

1. Install the `aws` CLI and get API credentials. You also need somewhere to publish an **AAAA** for `STATUS_DOMAIN` (OpenSRS, Route53, Cloudflare DNS, etc.).*

2. Run this on your local machine (ask any LLM to translate to PowerShell if needed):

```bash
# 1. Pick settings
REGION=ap-southeast-1
KEYPAIR=my-keypair
STATUS_DOMAIN=status.example.com
STATUS_TITLE='My Service Status'
TARGETS='microsoft=https://www.microsoft.com,google=https://www.google.com'

# 2. Default VPC + IPv6. Amazon gives the VPC a /56; we put a /64 on one subnet.
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
# e.g. 2406:da18:1356:3100::/56 → 2406:da18:1356:3100::/64
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
# No auto public IPv4 — that address is what AWS bills for.
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

# 3. Security group: SSH/HTTP/HTTPS (+ egress) on IPv6 only.
SG=$(aws ec2 create-security-group --region $REGION \
  --group-name currieping --description "CurriePing IPv6-only status page" \
  --vpc-id "$VPC" --query GroupId --output text)
for p in 22 80 443; do
  aws ec2 authorize-security-group-ingress --region $REGION \
    --group-id "$SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=$p,ToPort=$p,Ipv6Ranges=[{CidrIpv6=::/0}]"
done
aws ec2 authorize-security-group-egress --region $REGION \
  --group-id "$SG" --ip-permissions \
  "IpProtocol=-1,Ipv6Ranges=[{CidrIpv6=::/0}]" 2>/dev/null || true
aws ec2 revoke-security-group-egress --region $REGION --group-id "$SG" \
  --ip-permissions 'IpProtocol=-1,IpRanges=[{CidrIp=0.0.0.0/0}]' 2>/dev/null || true

# 4. Boot script: Docker IPv6 host publish, then CurriePing + auto-update cron.
#    Unquoted EOF so STATUS_* / TARGETS from step 1 are baked into the script.
cat > user-data.sh <<EOF
#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y docker.io docker-compose-v2 git unattended-upgrades
# Security updates continue to apply automatically after first boot.
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
  .env
docker compose up -d --build --remove-orphans
cp scripts/auto-update.sh /usr/local/bin/currieping-auto-update
chmod +x /usr/local/bin/currieping-auto-update
echo '*/15 * * * * root /usr/local/bin/currieping-auto-update' \\
  > /etc/cron.d/currieping-auto-update
chmod 644 /etc/cron.d/currieping-auto-update
EOF

# 5. Launch with IPv6, without a public IPv4 / Elastic IP.
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
echo "Instance $INSTANCE_ID public IPv6: $IPV6"
echo "Point AAAA for $STATUS_DOMAIN at that address (and delete any A record)."
```

3. **DNS — AAAA only** (OpenSRS for a name under `michaelcurrie.com`):

   - Create/update **AAAA** for `STATUS_DOMAIN` → the IPv6 printed above.
   - **Delete** any **A** record for that hostname.
   - Use a short TTL (e.g. 60) while testing.

   Verify from a dual-stack network:

   ```bash
   dig AAAA status.example.com +short
   curl -6 -I "https://$STATUS_DOMAIN"
   ```

   SSH over IPv6 (needs a working IPv6 path on your laptop):

   ```bash
   ssh -6 -i /path/to/key.pem ubuntu@$IPV6
   ```

   If your laptop is IPv4-only, jump through any other host in the same VPC that still has a public IPv4:

   ```bash
   ssh -i /path/to/key.pem \
     -o "ProxyCommand=ssh -i /path/to/key.pem -W %h:%p ubuntu@JUMP_PUBLIC_IPV4" \
     ubuntu@CURRIEPING_PRIVATE_IPV4
   ```

### On the Ubuntu host after login

Install path is `/opt/currieping` for new deploys. Older boxes may use the typo path `/opt/curieping` — check with `ls /opt`.

```bash
# Apply pending OS updates (also done in user-data on first boot for new installs)
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

cd /opt/currieping   # or: cd /opt/curieping
sudo docker compose ps
sudo docker compose logs -f --tail=100
```

### Migrating an existing IPv4 / Elastic IP box

1. Associate an Amazon-provided IPv6 `/56` to the VPC, a `/64` to the instance subnet, route `::/0` → the Internet Gateway, enable assign-IPv6-on-creation, and set **Map public IP = No** on that subnet.
2. Open the security group for TCP 22/80/443 from `::/0`, and allow egress to `::/0`.
3. Assign one IPv6 address to the instance ENI; publish that address as the hostname’s **AAAA** and remove the **A**.
4. On the instance, enable Docker IPv6 and recreate the stack:

   ```bash
   sudo apt-get update
   sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade
   APP_DIR=/opt/currieping
   [ -d /opt/curieping ] && APP_DIR=/opt/curieping
   sudo bash "$APP_DIR/scripts/enable-docker-ipv6.sh"
   cd "$APP_DIR" && sudo docker compose up -d --build --remove-orphans
   ```

5. Disassociate and release the Elastic IP. Turn **Map public IP** off on the subnet first. A stop/start *should* drop auto-assigned public IPv4; if AWS still attaches one, relaunch from an AMI with `AssociatePublicIpAddress=false` and `Ipv6AddressCount=1`, then terminate the old instance (that is what actually ends the IPv4 charge).

-----

* NOTE: These setup steps use a **public IPv6 only** (no public IPv4 / Elastic IP) to avoid AWS’s public IPv4 charge (~$3.65/month — often more than the instance). DNS for the status page is an **AAAA** record only.

**Trade-offs:** IPv4-only clients cannot open the status page. Outbound probes only succeed for dual-stack / IPv6 targets (Google, Microsoft, and most major sites are fine). Do **not** add a NAT Gateway for IPv4 compatibility — it costs far more than the IPv4 address fee.