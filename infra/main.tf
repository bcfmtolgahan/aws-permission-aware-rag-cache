terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 6.18, < 7.0" }
    random = { source = "hashicorp/random", version = "~> 3.7" }
  }
}

variable "aws_profile" {
  type      = string
  sensitive = true
  validation {
    condition     = length(trimspace(var.aws_profile)) > 0
    error_message = "An explicit sandbox profile is required."
  }
}
variable "expected_account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "A pinned sandbox account ID is required."
  }
}
variable "run_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9]{6,10}$", var.run_id))
    error_message = "run_id must be 6-10 lowercase letters or digits."
  }
}
variable "region" {
  type    = string
  default = "eu-central-1"
  validation {
    condition     = var.region == "eu-central-1"
    error_message = "This sandbox lab is pinned to eu-central-1."
  }
}

provider "aws" {
  profile             = var.aws_profile
  region              = var.region
  allowed_account_ids = [var.expected_account_id]
  default_tags {
    tags = { Project = "permission-aware-rag-cache", RunId = var.run_id, ManagedBy = "Terraform", Environment = "sandbox" }
  }
}

locals {
  name     = "parc-${var.run_id}"
  model_id = "eu.amazon.nova-micro-v1:0"
  zip      = "${path.module}/../build/lambda.zip"
}

data "aws_availability_zones" "available" {
  state = "available"
  # Opted-in Local Zones do not support this lab's managed services.
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

resource "aws_vpc" "lab" {
  cidr_block           = "10.84.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.lab.id
  cidr_block        = "10.84.${count.index + 1}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  timeouts { delete = "30m" }
}
resource "aws_route_table" "private" { vpc_id = aws_vpc.lab.id }
resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
resource "aws_security_group" "app" {
  name   = "${local.name}-app"
  vpc_id = aws_vpc.lab.id
  # Lambda needs this role while asynchronously reclaiming Hyperplane ENIs.
  # Reverse destroy ordering retains the role until its security group is gone.
  depends_on = [aws_iam_role_policy.lambda]
  timeouts { delete = "30m" }
}
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}
resource "aws_security_group" "cache" {
  name   = "${local.name}-cache"
  vpc_id = aws_vpc.lab.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}
resource "aws_vpc_security_group_egress_rule" "cache" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.cache.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}
resource "aws_security_group" "endpoints" {
  name   = "${local.name}-endpoints"
  vpc_id = aws_vpc.lab.id
  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}
resource "aws_vpc_endpoint" "gateway" {
  for_each          = toset(["s3", "dynamodb"])
  vpc_id            = aws_vpc.lab.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
}
resource "aws_vpc_endpoint" "bedrock" {
  vpc_id              = aws_vpc.lab.id
  service_name        = "com.amazonaws.${var.region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
}

resource "random_password" "cache" {
  length  = 40
  special = false
}
resource "aws_elasticache_subnet_group" "lab" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_elasticache_parameter_group" "lab" {
  name   = local.name
  family = "valkey8"
  parameter {
    name  = "reserved-memory-percent"
    value = "30"
  }
}
resource "aws_elasticache_replication_group" "lab" {
  replication_group_id       = local.name
  description                = "Disposable permission-aware RAG cache lab"
  engine                     = "valkey"
  engine_version             = "8.2"
  node_type                  = "cache.t4g.small"
  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false
  parameter_group_name       = aws_elasticache_parameter_group.lab.name
  subnet_group_name          = aws_elasticache_subnet_group.lab.name
  security_group_ids         = [aws_security_group.cache.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.cache.result
  apply_immediately          = true
  snapshot_retention_limit   = 0
}

resource "aws_dynamodb_table" "authority" {
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
}
resource "aws_s3_bucket" "documents" {
  bucket        = "${local.name}-documents"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "tls" {
  bucket = aws_s3_bucket.documents.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}

resource "aws_iam_role" "lambda" {
  name = local.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy" "lambda" {
  name = local.name
  role = aws_iam_role.lambda.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], Resource = "*" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${var.region}:${var.expected_account_id}:log-group:/aws/lambda/${local.name}-*:*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:TransactGetItems"], Resource = aws_dynamodb_table.authority.arn },
    { Effect = "Allow", Action = ["dynamodb:UpdateItem"], Resource = aws_dynamodb_table.authority.arn, Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["TEST#*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.documents.arn}/documents/*" },
    { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = [
      "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0",
      "arn:aws:bedrock:${var.region}:${var.expected_account_id}:inference-profile/${local.model_id}",
      "arn:aws:bedrock:eu-*::foundation-model/amazon.nova-micro-v1:0"
    ] }
  ] })
}
resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = toset(["api", "test"])
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = 1
}
resource "aws_lambda_function" "app" {
  for_each         = toset(["api", "test"])
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.lambda.arn
  handler          = each.key == "api" ? "handler.lambda_handler" : "handler.test_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  filename         = local.zip
  source_code_hash = filebase64sha256(local.zip)
  timeout          = 60
  memory_size      = 512
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.app.id]
  }
  environment {
    variables = {
      TABLE_NAME           = aws_dynamodb_table.authority.name, BUCKET_NAME = aws_s3_bucket.documents.id,
      CACHE_HOST           = aws_elasticache_replication_group.lab.primary_endpoint_address,
      CACHE_PASSWORD       = random_password.cache.result, MODEL_ID = local.model_id,
      SIMILARITY_THRESHOLD = "0.75"
    }
  }
  depends_on = [aws_iam_role_policy.lambda, aws_cloudwatch_log_group.lambda, aws_vpc_endpoint.bedrock, aws_vpc_endpoint.gateway]
}

resource "aws_cognito_user_pool" "lab" {
  name = local.name
  admin_create_user_config { allow_admin_create_user_only = true }
  password_policy {
    minimum_length    = 16
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }
}
resource "aws_cognito_user_pool_client" "lab" {
  name                          = local.name
  user_pool_id                  = aws_cognito_user_pool.lab.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
}
resource "aws_apigatewayv2_api" "lab" {
  name          = local.name
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.lab.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.lab.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.lab.id}"
  }
}
resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.lab.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.app["api"].invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}
resource "aws_apigatewayv2_route" "query" {
  api_id             = aws_apigatewayv2_api.lab.id
  route_key          = "POST /query"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.lab.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}
resource "aws_lambda_permission" "gateway" {
  statement_id  = "AllowOwnedApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app["api"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.lab.execution_arn}/*/POST/query"
}

output "resources" {
  value = {
    name               = local.name, region = var.region, table = aws_dynamodb_table.authority.name,
    bucket             = aws_s3_bucket.documents.id, api_url = aws_apigatewayv2_api.lab.api_endpoint,
    api_id             = aws_apigatewayv2_api.lab.id, pool_id = aws_cognito_user_pool.lab.id,
    client_id          = aws_cognito_user_pool_client.lab.id, vpc_id = aws_vpc.lab.id,
    test_function      = aws_lambda_function.app["test"].function_name,
    api_function       = aws_lambda_function.app["api"].function_name,
    cache_id           = aws_elasticache_replication_group.lab.id,
    model_id           = local.model_id, role_name = aws_iam_role.lambda.name,
    endpoint_ids       = concat([aws_vpc_endpoint.bedrock.id], [for x in aws_vpc_endpoint.gateway : x.id]),
    security_group_ids = [aws_security_group.app.id, aws_security_group.cache.id, aws_security_group.endpoints.id]
  }
}
