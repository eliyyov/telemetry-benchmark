terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.68" }
    tls     = { source = "hashicorp/tls", version = "~> 4.4" }
    local   = { source = "hashicorp/local", version = "~> 2.9" }
    http    = { source = "hashicorp/http", version = "~> 3.6" }
    archive = { source = "hashicorp/archive", version = "~> 2.8" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.project }
  }
}
