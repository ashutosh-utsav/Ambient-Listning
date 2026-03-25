resource "aws_dynamodb_table" "session_index" {
  name         = var.dynamodb_session_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "client_kb" {
  name         = var.dynamodb_kb_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "client_id"
  range_key    = "record_type"

  attribute {
    name = "client_id"
    type = "S"
  }

  attribute {
    name = "record_type"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
