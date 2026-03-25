terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.37"
    }
  }

  backend "s3" {
    bucket         = "ambient-listning-tfstate"
    key            = "ambient-listning/terraform.tfstate"
    region         = "ap-south-1"
    encrypt        = true
    dynamodb_table = "ambient-listning-tfstate-lock"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "ambient-listning"
      Environment = var.environment
      ManagedBy   = "opentofu"
    }
  }
}
