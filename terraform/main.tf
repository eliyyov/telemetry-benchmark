# Two-VM testbed: VM-Data (agent under test) and VM-Mon (monitoring + experiment control).
# Only VM-Mon is reachable from the internet, on SSH, from allowed_ssh_cidr.
# VM-Data accepts SSH and the scrape ports from VM-Mon only.

data "http" "my_ip" {
  count = var.allowed_ssh_cidr == "" ? 1 : 0
  url   = "https://checkip.amazonaws.com"
}

locals {
  ssh_cidr = var.allowed_ssh_cidr != "" ? var.allowed_ssh_cidr : "${chomp(data.http.my_ip[0].response_body)}/32"
  # Both instance types must exist in the chosen AZ.
  azs = sort(tolist(setintersection(
    toset(data.aws_ec2_instance_type_offerings.data.locations),
    toset(data.aws_ec2_instance_type_offerings.mon.locations),
  )))
  az = local.azs[0]
}

data "aws_ec2_instance_type_offerings" "data" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.data_instance_type]
  }
}

data "aws_ec2_instance_type_offerings" "mon" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.mon_instance_type]
  }
}

# Ubuntu 24.04 LTS, amd64, from Canonical.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# ---------------- network ----------------
resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = local.az
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.project}-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------- security groups ----------------
resource "aws_security_group" "mon" {
  name        = "${var.project}-mon"
  description = "VM-Mon: SSH from the operator only"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "SSH from operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [local.ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "data" {
  name        = "${var.project}-data"
  description = "VM-Data: SSH and scrape ports from VM-Mon only"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "SSH from VM-Mon"
    from_port       = 22
    to_port         = 22
    protocol        = "tcp"
    security_groups = [aws_security_group.mon.id]
  }
  ingress {
    description     = "cAdvisor"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.mon.id]
  }
  ingress {
    description     = "OTel internal metrics"
    from_port       = 8888
    to_port         = 8888
    protocol        = "tcp"
    security_groups = [aws_security_group.mon.id]
  }
  ingress {
    description     = "Vector (9598) and OTel (9599) host-metric exporters"
    from_port       = 9598
    to_port         = 9599
    protocol        = "tcp"
    security_groups = [aws_security_group.mon.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------- keys ----------------
# Operator key: you -> VM-Mon (and VM-Data through VM-Mon).
resource "tls_private_key" "operator" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "operator" {
  key_name   = "${var.project}-operator"
  public_key = tls_private_key.operator.public_key_openssh
}

resource "local_sensitive_file" "operator_key" {
  content         = tls_private_key.operator.private_key_openssh
  filename        = "${path.module}/operator_key.pem"
  file_permission = "0600"
}

# Internal key: VM-Mon -> VM-Data only. Stored in VM-Mon's user data (visible to anyone on VM-Mon
# and in the Terraform state); it only opens SSH on VM-Data, which accepts SSH from VM-Mon only.
resource "tls_private_key" "internal" {
  algorithm = "ED25519"
}

# ---------------- instances ----------------
resource "aws_instance" "data" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.data_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.data.id]
  key_name               = aws_key_pair.operator.key_name

  user_data = templatefile("${path.module}/templates/cloudinit_data.yaml.tftpl", {
    internal_pub = trimspace(tls_private_key.internal.public_key_openssh)
  })

  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    volume_size = var.data_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }
  tags = { Name = "${var.project}-vm-data", Role = "data" }
}

resource "aws_instance" "mon" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.mon_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.mon.id]
  key_name               = aws_key_pair.operator.key_name

  user_data = templatefile("${path.module}/templates/cloudinit_mon.yaml.tftpl", {
    internal_key_b64 = base64encode(tls_private_key.internal.private_key_openssh)
    data_ip          = aws_instance.data.private_ip
  })

  metadata_options {
    http_tokens = "required"
  }
  root_block_device {
    volume_size = var.mon_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }
  tags = { Name = "${var.project}-vm-mon", Role = "mon" }
}

# ---------------- ship the benchmark bundle to VM-Mon ----------------
data "archive_file" "bench" {
  type        = "zip"
  source_dir  = "${path.module}/../bench"
  output_path = "${path.module}/build/bench.zip"
}

resource "terraform_data" "upload_bench" {
  triggers_replace = [aws_instance.mon.id, data.archive_file.bench.output_sha256]

  connection {
    type        = "ssh"
    host        = aws_instance.mon.public_ip
    user        = "ubuntu"
    private_key = tls_private_key.operator.private_key_openssh
  }

  provisioner "remote-exec" {
    inline = ["cloud-init status --wait > /dev/null || true"]
  }
  provisioner "file" {
    source      = data.archive_file.bench.output_path
    destination = "/home/ubuntu/bench.zip"
  }
  provisioner "remote-exec" {
    inline = ["rm -rf /home/ubuntu/bench && unzip -q -o /home/ubuntu/bench.zip -d /home/ubuntu/bench && chmod +x /home/ubuntu/bench/*.sh"]
  }
}
