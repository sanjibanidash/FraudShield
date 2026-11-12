# Deploying FraudShield for $0

| Option | What runs | Cost | Card needed? | Best for |
|---|---|---|---|---|
| **A. Oracle Cloud Always Free VM** | the **entire stack** 24/7 (Kafka, Postgres, all services, Grafana…) + automatic CD from GitHub Actions | $0 | yes, for identity verification only | the "real" cloud deployment (session 5 + 6) |
| **B. GitHub Codespaces** | the entire stack, while the codespace is running | $0 within the free monthly quota | no | demos, development, if you can't use Oracle |
| **C. Render free web service** | only the scoring API (model baked into the image) | $0 | no | a permanent public link for your CV / portfolio |

GitHub Actions (CI) and GitHub Container Registry (images) are free for public repositories.

> **Note on Oracle's free tier (changed June 2026):** Always Free Ampere A1 is now **2 OCPU + 12 GB RAM** in total
> (it used to be 4 OCPU + 24 GB; many older tutorials still say 4/24). Create a **2 OCPU / 12 GB** VM. Anything
> bigger is billed. The compose file is sized for this, with Kafka on a 512 MB heap and memory limits in `docker-compose.prod.yml`.

---

## A. Oracle Cloud Always Free (full stack, recommended)

### 1. Create the account and the VM (≈15 min, one time)
1. Sign up at **oracle.com/cloud/free**. Choose a home region close to you (e.g. Mumbai or Hyderabad). The home region can't be changed later.
   The card is only used to verify your identity. Stay on Always Free resources and you are never charged.
   To be extra safe, set a **budget alert at $1** under *Billing → Budgets*.
2. *Compute → Instances → Create instance*
   - **Image:** Canonical Ubuntu 24.04
   - **Shape:** Ampere → `VM.Standard.A1.Flex` → **2 OCPU, 12 GB** (it shows the *Always Free-eligible* label)
   - **Networking:** create a new VCN with a public subnet, and assign a public IPv4
   - **SSH keys:** "Generate a key pair" and **download the private key** (or paste your own public key)
   - **Boot volume:** default 50 GB is fine
   - If you see *"Out of capacity"*, try another availability domain, or try again later (capacity frees up often).
     A 1 OCPU / 6 GB VM also works if you set `PRODUCER_TPS=5` in `.env`.
3. Open the ports in Oracle's network firewall:
   *Networking → Virtual Cloud Networks → your VCN → Security Lists → Default → Add Ingress Rules*
   - Source `0.0.0.0/0`, TCP, destination ports **`8000,5000,3000`** (API, dashboard, Grafana)
   - Don't open 5432 / 9092 / 9090. Postgres, Kafka and Prometheus stay private (`BIND_ADDR=127.0.0.1`).

### 2. Prepare the VM
```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<VM_PUBLIC_IP>

# on the VM:
curl -fsSL https://raw.githubusercontent.com/<your-github-user>/fraudshield/main/deploy/setup_vm.sh -o setup_vm.sh
bash setup_vm.sh          # installs Docker, swap, opens the OS firewall, writes ~/fraudshield/.env with random passwords
exit                      # log out/in so the docker group applies
```
Then edit `~/fraudshield/.env` on the VM and set `REGISTRY=ghcr.io/<your-github-user-lowercase>/fraudshield`.
Save the `DASHBOARD_PASSWORD` and `GRAFANA_PASSWORD` that the script printed.

### 3. Connect GitHub → VM (CD)
In your GitHub repo: *Settings → Secrets and variables → Actions*
- **Secrets**
  - `VM_HOST`: the VM public IP
  - `VM_USER`: `ubuntu`
  - `VM_SSH_KEY`: the full contents of the private key file (including the `-----BEGIN…` lines)
- **Variables**
  - `DEPLOY_ENABLED`: `true`

Also create an environment named `production` (*Settings → Environments*). You can add yourself as a required reviewer for manual approval before each deploy.

### 4. Ship it
```bash
git push origin main
```
1. **CI** runs lint → unit tests → full docker-compose end-to-end test → builds **amd64 + arm64** images → pushes them to GHCR.
2. **Deploy** copies the compose and monitoring files to the VM over SSH, pulls the images tagged with the commit SHA, runs `docker compose up -d`, and checks `http://<VM_IP>:8000/health` from the internet.

Open:
- `http://<VM_IP>:5000`: dashboard (Controls/review buttons ask for the `DASHBOARD_PASSWORD`)
- `http://<VM_IP>:8000/docs`: interactive API docs
- `http://<VM_IP>:3000`: Grafana (anonymous read-only view; log in as admin to edit)

Useful on the VM:
```bash
cd ~/fraudshield
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f consumer
docker stats --no-stream            # memory per container
```

**Using the real PaySim dataset on the VM:**
1. `scp paysim.csv ubuntu@<IP>:~/fraudshield/data/raw/paysim.csv`
2. Delete the model volume so the trainer runs again: `docker compose -f docker-compose.yml -f docker-compose.prod.yml down && docker volume rm fraudshield_models fraudshield_streamdata`
3. Bring the stack back up (`up -d`).

**Rollback:**
```bash
bash deploy/release.sh ghcr.io/<you>/fraudshield <previous-commit-sha>
```
Or re-run an older *Deploy* workflow from the Actions tab.

---

## B. GitHub Codespaces (full stack, no card)
1. In the repo: *Code → Codespaces → Create codespace* (choose the **4-core** machine; the `.devcontainer` asks for it).
2. In the terminal: `make up`, then wait 2–4 minutes for the first build.
3. Open the **Ports** tab: 5000 dashboard, 8000 API, 3000 Grafana. You can set a port to *Public* to share a demo link.
4. **Stop the codespace when you're done** (*Codespaces → … → Stop*). The free monthly core-hour quota only counts while it runs.

---

## C. Render (public API only, no card)
1. Push the repo to GitHub.
2. Go to render.com → *New → Blueprint* → select the repo. Render reads `render.yaml` and builds `services/api/Dockerfile.render`, which trains a model at build time and bakes it in.
3. You get `https://fraudshield-api-xxxx.onrender.com/docs`.
   Free instances sleep after 15 minutes without traffic, and the first request then takes about a minute.

---

## Free alert channels
Set any of these in `.env` (on the VM: `~/fraudshield/.env`), then restart the dashboard:
- **Discord:** *Channel settings → Integrations → Webhooks → New* → set `DISCORD_WEBHOOK_URL`
- **Slack:** create an app → *Incoming Webhooks* → set `SLACK_WEBHOOK_URL`
- **Telegram:** talk to @BotFather → `/newbot` → set `TELEGRAM_BOT_TOKEN`. Message your bot once, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates` and set `TELEGRAM_CHAT_ID`.

Restart command:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d dashboard
```
