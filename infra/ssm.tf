resource "aws_ssm_parameter" "openai_api_key" {
  name        = "/${var.app_name}/openai_api_key"
  description = "OpenAI API key for transcription and summarization"
  type        = "SecureString"
  value       = var.openai_api_key
}

resource "aws_ssm_parameter" "api_key" {
  name        = "/${var.app_name}/api_key"
  description = "Application API key for request authentication"
  type        = "SecureString"
  value       = var.api_key == "" ? "unused" : var.api_key
}
