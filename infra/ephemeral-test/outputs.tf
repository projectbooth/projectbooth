output "droplet_ip" {
  description = "Public IPv4 of the test droplet."
  value       = digitalocean_droplet.this.ipv4_address
}

output "droplet_name" {
  description = "DO droplet name — useful for finding it in the control panel, or for `doctl compute droplet delete` if run.sh's teardown path is ever skipped."
  value       = digitalocean_droplet.this.name
}

output "ssh_private_key_pem" {
  description = "The freshly generated droplet-access private key (PEM/OpenSSH format). run.sh writes this to a gitignored local file rather than printing it — this output exists mainly so `terraform output -raw ssh_private_key_pem` works if you need it directly."
  value       = tls_private_key.droplet_access.private_key_openssh
  sensitive   = true
}
