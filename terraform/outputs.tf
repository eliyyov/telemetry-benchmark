output "mon_public_ip" {
  value = aws_instance.mon.public_ip
}

output "data_private_ip" {
  value = aws_instance.data.private_ip
}

output "availability_zone" {
  value = local.az
}

output "ssh_mon" {
  description = "Log in to VM-Mon. Then run: ./bench/setup.sh (once) and ./bench/run.sh"
  value       = "ssh -i ${local_sensitive_file.operator_key.filename} ubuntu@${aws_instance.mon.public_ip}"
}

output "grafana_tunnel" {
  description = "Optional: open Grafana at http://localhost:3000 through an SSH tunnel"
  value       = "ssh -i ${local_sensitive_file.operator_key.filename} -L 3000:127.0.0.1:3000 ubuntu@${aws_instance.mon.public_ip}"
}
