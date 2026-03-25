#!/bin/bash
set -e

# Install Docker and AWS CLI
dnf install -y docker aws-cli
systemctl enable --now docker
usermod -aG docker ec2-user

# Install docker compose plugin
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Fetch secrets from SSM
OPENAI_API_KEY=$(aws ssm get-parameter --name "${openai_ssm_param}" --with-decryption --query Parameter.Value --output text --region ${aws_region})
API_KEY=$(aws ssm get-parameter --name "${api_key_ssm_param}" --with-decryption --query Parameter.Value --output text --region ${aws_region})

# Log into ECR
aws ecr get-login-password --region ${aws_region} | \
  docker login --username AWS --password-stdin $(echo "${ecr_web_url}" | cut -d/ -f1)

# Write docker-compose file
mkdir -p /opt/app
cat > /opt/app/docker-compose.yml <<EOF
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped

  web:
    image: ${ecr_web_url}:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      AWS_REGION: ${aws_region}
      S3_BUCKET_NAME: ${s3_bucket_name}
      DYNAMODB_TABLE_NAME: ${dynamodb_session_table}
      KB_DYNAMODB_TABLE_NAME: ${dynamodb_kb_table}
      REDIS_URL: redis://redis:6379
      OPENAI_API_KEY: "$${OPENAI_API_KEY}"
      API_KEY: "$${API_KEY}"
      ENABLE_API_KEY_AUTH: "${enable_api_key_auth}"
      KB_BRIEF_RECENT_SESSIONS: "${kb_brief_recent}"
      ENVIRONMENT: "${environment}"
      WEBSERVER_ERROR_CALLBACK: http://localhost:8000/ws/error
    depends_on:
      - redis

  worker:
    image: ${ecr_worker_url}:latest
    restart: unless-stopped
    environment:
      AWS_REGION: ${aws_region}
      S3_BUCKET_NAME: ${s3_bucket_name}
      DYNAMODB_TABLE_NAME: ${dynamodb_session_table}
      KB_DYNAMODB_TABLE_NAME: ${dynamodb_kb_table}
      REDIS_URL: redis://redis:6379
      OPENAI_API_KEY: "$${OPENAI_API_KEY}"
      KB_BRIEF_RECENT_SESSIONS: "${kb_brief_recent}"
      ENVIRONMENT: "${environment}"
      WEBSERVER_ERROR_CALLBACK: http://web:8000/ws/error
    depends_on:
      - redis
      - web
EOF

# Pull images and start
cd /opt/app
docker compose pull
docker compose up -d
