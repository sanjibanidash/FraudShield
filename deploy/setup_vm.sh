#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04/24.04 VM (Oracle Cloud Always Free, Ampere A1 ARM).
#   ssh ubuntu@<VM_IP>
#   curl -fsSL https://raw.githubusercontent.com/<you>/fraudshield/main/deploy/setup_vm.sh | bash
#   (or scp this file and run: bash setup_vm.sh)
set -euo pipefail

echo "==> Installing Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"

echo "==> Adding 4 GB swap (safety net for the 12 GB VM during image pulls / retraining)"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> Opening ports 8000 (API), 5000 (dashboard), 3000 (Grafana) in the OS firewall"
# Oracle's Ubuntu images ship with iptables rules that block everything except SSH.
for port in 8000 5000 3000; do
  sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
done
sudo apt-get install -y -qq netfilter-persistent iptables-persistent >/dev/null 2>&1 || true
sudo netfilter-persistent save || true

echo "==> Creating ~/fraudshield/.env with random secrets"
mkdir -p ~/fraudshield/data/raw ~/fraudshield/data/batch/input
cd ~/fraudshield
if [[ ! -f .env ]]; then
  PUBLIC_IP=$(curl -fsS https://ifconfig.me || echo "YOUR_VM_IP")
  rnd() { openssl rand -hex 16; }
  cat > .env <<EOF
REGISTRY=ghcr.io/CHANGE-ME/fraudshield
IMAGE_TAG=latest
POSTGRES_USER=fraud
POSTGRES_PASSWORD=$(rnd)
POSTGRES_DB=fraudshield
ADMIN_TOKEN=$(rnd)
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=$(rnd)
GRAFANA_USER=admin
GRAFANA_PASSWORD=$(rnd)
GRAFANA_ANONYMOUS=true
BIND_ADDR=127.0.0.1
PUBLIC_GRAFANA_URL=http://${PUBLIC_IP}:3000
PRODUCER_TPS=10
BATCH_INTERVAL=3600
EOF
  chmod 600 .env
  echo "Created .env - your passwords:"
  grep -E "DASHBOARD_PASSWORD|GRAFANA_PASSWORD" .env
fi

echo
echo "Done. Log out and back in (docker group), then push to main - GitHub Actions deploys automatically."
echo "Also open ports 8000, 5000, 3000 in the Oracle VCN Security List (see docs/DEPLOYMENT.md)."
