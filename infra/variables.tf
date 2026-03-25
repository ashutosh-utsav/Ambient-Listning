variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "production"
}

variable "app_name" {
  description = "Short name used to prefix all resources"
  type        = string
  default     = "ambient-listning"
}

variable "instance_type" {
  description = "EC2 instance type for the app server"
  type        = string
  default     = "t4g.small"
}

variable "key_pair_name" {
  description = "Name of an existing EC2 key pair for SSH access"
  type        = string
}

variable "s3_bucket_name" {
  description = "S3 bucket for recordings, transcripts and KB briefs"
  type        = string
  default     = "ambient-listning-recordings"
}

variable "dynamodb_session_table" {
  description = "DynamoDB table name for session index"
  type        = string
  default     = "sessionIndex"
}

variable "dynamodb_kb_table" {
  description = "DynamoDB table name for client knowledge base"
  type        = string
  default     = "clientKB"
}

variable "openai_api_key" {
  description = "OpenAI API key stored in SSM SecureString"
  type        = string
  sensitive   = true
}

variable "enable_api_key_auth" {
  description = "Whether to require an API key on requests"
  type        = bool
  default     = false
}

variable "api_key" {
  description = "Application API key (only used if enable_api_key_auth is true)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "kb_brief_recent_sessions" {
  description = "How many recent sessions the KB brief considers"
  type        = number
  default     = 10
}

variable "allowed_cidr" {
  description = "CIDR allowed to reach port 8000 (default open, restrict for production)"
  type        = string
  default     = "0.0.0.0/0"
}
