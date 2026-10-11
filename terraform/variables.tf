variable "region" {
  description = "AWS region. Both VMs are placed in one availability zone of this region."
  type        = string
  default     = "ap-southeast-1"
}

variable "project" {
  description = "Name prefix and Project tag for every resource."
  type        = string
  default     = "telemetry-bench"
}

variable "data_instance_type" {
  description = "VM-Data: runs the agent under test, loggen, the sampler and cAdvisor."
  type        = string
  default     = "c6a.xlarge"
}

variable "mon_instance_type" {
  description = "VM-Mon: runs Prometheus, Grafana and the experiment scripts."
  type        = string
  default     = "c6a.large"
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH into VM-Mon, e.g. 203.0.113.7/32. Leave empty to use your current public IP."
  type        = string
  default     = ""
}

variable "data_volume_gb" {
  type    = number
  default = 30
}

variable "mon_volume_gb" {
  type    = number
  default = 20
}
