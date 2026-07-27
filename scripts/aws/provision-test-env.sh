#!/usr/bin/env bash
set -euo pipefail

PROFILE="${AWS_PROFILE:-ziggy}"
REGION="${AWS_REGION:-us-east-1}"
NAME_PREFIX="${CAMWALL_AWS_NAME_PREFIX:-camwall-test}"
INSTANCE_TYPE="${CAMWALL_AWS_INSTANCE_TYPE:-t3.small}"
KEY_NAME="${CAMWALL_AWS_KEY_NAME:-camwall-codex}"
PUBLIC_KEY_FILE="${CAMWALL_AWS_PUBLIC_KEY_FILE:-$HOME/.ssh/github_codex_ed25519.pub}"
OWNER_CIDR="${CAMWALL_AWS_OWNER_CIDR:-$(curl -sS https://checkip.amazonaws.com)/32}"

aws_cli() {
  aws --profile "$PROFILE" --region "$REGION" "$@"
}

default_vpc="$(aws_cli ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
subnet_id="$(aws_cli ec2 describe-subnets --filters Name=vpc-id,Values="$default_vpc" --query 'Subnets[0].SubnetId' --output text)"
ami_id="$(aws_cli ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query 'Parameter.Value' --output text)"

if ! aws_cli ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  aws_cli ec2 import-key-pair --key-name "$KEY_NAME" --public-key-material "fileb://$PUBLIC_KEY_FILE" >/dev/null
fi

sg_id="$(aws_cli ec2 describe-security-groups --filters Name=group-name,Values="$NAME_PREFIX-sg" Name=vpc-id,Values="$default_vpc" --query 'SecurityGroups[0].GroupId' --output text)"
if [[ "$sg_id" == "None" ]]; then
  sg_id="$(aws_cli ec2 create-security-group --group-name "$NAME_PREFIX-sg" --description "CamWall test environment" --vpc-id "$default_vpc" --query GroupId --output text)"
  aws_cli ec2 authorize-security-group-ingress --group-id "$sg_id" --ip-permissions \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$OWNER_CIDR,Description=ssh}]" \
    "IpProtocol=tcp,FromPort=8090,ToPort=8091,IpRanges=[{CidrIp=$OWNER_CIDR,Description=camwall-ui-api}]" \
    "IpProtocol=tcp,FromPort=1984,ToPort=1984,IpRanges=[{CidrIp=$OWNER_CIDR,Description=go2rtc-api}]" \
    "IpProtocol=tcp,FromPort=8555,ToPort=8555,IpRanges=[{CidrIp=$OWNER_CIDR,Description=webrtc-tcp}]" \
    "IpProtocol=udp,FromPort=8555,ToPort=8555,IpRanges=[{CidrIp=$OWNER_CIDR,Description=webrtc-udp}]" \
    "IpProtocol=tcp,FromPort=554,ToPort=554,IpRanges=[{CidrIp=$OWNER_CIDR,Description=mock-rtsp}]" \
    "IpProtocol=tcp,FromPort=2020,ToPort=2020,IpRanges=[{CidrIp=$OWNER_CIDR,Description=mock-onvif}]" >/dev/null
fi

run_instance() {
  local role="$1"
  aws_cli ec2 run-instances \
    --image-id "$ami_id" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --subnet-id "$subnet_id" \
    --security-group-ids "$sg_id" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME_PREFIX-$role},{Key=Project,Value=CamWall},{Key=Purpose,Value=Test}]" \
    --query 'Instances[0].InstanceId' --output text
}

master_id="$(run_instance master)"
mock_id="$(run_instance mock-cameras)"
aws_cli ec2 wait instance-running --instance-ids "$master_id" "$mock_id"

aws_cli ec2 describe-instances --instance-ids "$master_id" "$mock_id" \
  --query 'Reservations[].Instances[].{id:InstanceId,name:Tags[?Key==`Name`]|[0].Value,public_ip:PublicIpAddress,private_ip:PrivateIpAddress,state:State.Name}' \
  --output table
