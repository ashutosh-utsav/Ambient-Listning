output "public_ip" {
  description = "Public IP of the app server"
  value       = aws_eip.app.public_ip
}

output "app_url" {
  description = "URL to reach the web server"
  value       = "http://${aws_eip.app.public_ip}:8000"
}

output "ecr_web_url" {
  description = "ECR repo URL for the web image"
  value       = aws_ecr_repository.web.repository_url
}

output "ecr_worker_url" {
  description = "ECR repo URL for the worker image"
  value       = aws_ecr_repository.worker.repository_url
}

output "s3_bucket" {
  description = "S3 bucket name"
  value       = aws_s3_bucket.recordings.bucket
}
