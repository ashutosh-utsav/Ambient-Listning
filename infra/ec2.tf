# Latest Amazon Linux 2023 ARM64 AMI in ap-south-1
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

resource "aws_security_group" "app" {
  name        = "${var.app_name}-sg"
  description = "App server security group"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "App port"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  key_name               = var.key_pair_name
  iam_instance_profile   = aws_iam_instance_profile.app.name
  vpc_security_group_ids = [aws_security_group.app.id]

  # 20 GB is enough for Docker images + logs
  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  user_data = templatefile("${path.module}/userdata.sh.tpl", {
    aws_region             = var.aws_region
    ecr_web_url            = aws_ecr_repository.web.repository_url
    ecr_worker_url         = aws_ecr_repository.worker.repository_url
    s3_bucket_name         = var.s3_bucket_name
    dynamodb_session_table = var.dynamodb_session_table
    dynamodb_kb_table      = var.dynamodb_kb_table
    openai_ssm_param       = aws_ssm_parameter.openai_api_key.name
    api_key_ssm_param      = aws_ssm_parameter.api_key.name
    enable_api_key_auth    = tostring(var.enable_api_key_auth)
    kb_brief_recent        = tostring(var.kb_brief_recent_sessions)
    environment            = var.environment
  })

  tags = { Name = var.app_name }
}

resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
}
