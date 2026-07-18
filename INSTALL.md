### Deploy on a fresh AWS box (public HTTPS via your own domain)

Run this on your local machine (needs the AWS CLI, logged in). It creates a `t4g.nano`, installs everything, and starts the stack. The box refreshes from the git repo every 15 minutes.

```bash
# 1. Pick a region, key pair, and the three .env settings the box will boot with
REGION=us-west-1
KEYPAIR=my-keypair
STATUS_DOMAIN=status.example.com
STATUS_TITLE='Service Status'
TARGETS='microsoft=https://www.microsoft.com,google=https://www.google.com'

# 2. Create a security group that allows SSH + HTTP + HTTPS
VPC=$(aws ec2 describe-vpcs --region $REGION \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --region $REGION \
  --group-name currieping --description "status page" \
  --vpc-id $VPC --query GroupId --output text)
for p in 22 80 443; do
  aws ec2 authorize-security-group-ingress --region $REGION \
    --group-id $SG --protocol tcp --port $p --cidr 0.0.0.0/0
done

# 3. Write the boot script (clones repo, writes .env, starts Docker, installs 15-min auto-update cron)
#    Unquoted EOF so STATUS_* / TARGETS from step 1 are baked into the script.
cat > user-data.sh <<EOF
#!/bin/bash
set -eux
apt-get update
apt-get install -y docker.io docker-compose-v2 git
systemctl enable --now docker
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

# 4. Launch the instance
AMI=$(aws ssm get-parameter --region $REGION \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)
aws ec2 run-instances --region $REGION \
  --image-id $AMI --instance-type t4g.nano \
  --key-name $KEYPAIR --security-group-ids $SG \
  --user-data file://user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=currieping}]'

# 5. Point DNS at STATUS_DOMAIN with cloudflared
#    (Cloudflare proxy OFF — DNS only / grey cloud; Caddy needs a direct path for the cert)
cloudflared tunnel route dns currieping "$STATUS_DOMAIN"
```

