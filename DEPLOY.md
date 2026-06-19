# Deploying Trading Buddy to an always-on free server

Goal: run Trading Buddy 24/7 on a free cloud VM (so it doesn't depend on your
laptop), with the dashboard reachable from any laptop/phone — **still in sandbox
(no real money).**

Work through it top to bottom. Ping me at any step and I'll help.

---

## Part 1 — Create the free Oracle Cloud account + VM

1. Go to **https://www.oracle.com/cloud/free/** → **Start for free**. Sign up
   (needs an email + a card for identity verification — the **Always Free** VM is
   genuinely ₹0; you won't be charged unless you manually upgrade to paid).
2. Pick the **home region** closest to you (e.g. **India South (Hyderabad)** or
   **India West (Mumbai)**).
3. In the console: **Menu → Compute → Instances → Create instance.**
   - **Image:** Canonical **Ubuntu** (22.04 or 24.04).
   - **Shape:** click *Change shape* → **Always Free eligible**:
     - First try **VM.Standard.A1.Flex** (ARM) with **1 OCPU / 6 GB** — generous and free.
     - If it says "out of capacity", use **VM.Standard.E2.1.Micro** (AMD, 1 GB) — always available.
   - **SSH keys:** choose **Generate a key pair for me** → **Download the private key**
     (save it safely — you need it to log in). 
   - Click **Create**. Wait ~1 min until it's **Running**. Note the **Public IP address**.

> Tell me which shape you got and your OS version, and I'll tailor the next commands.

---

## Part 2 — Connect and set up the server

From your Windows PC (PowerShell), connecting uses the downloaded key:

```powershell
# replace the key path and the IP
ssh -i "C:\path\to\your-key.key" ubuntu@YOUR_VM_PUBLIC_IP
```
(If it complains about key permissions, tell me — there's a one-line fix.)

Once logged in (you'll see `ubuntu@...:~$`):

```bash
# get the code onto the server — easiest is to copy your folder up (see Part 3),
# but if you put it on GitHub:  git clone <your-repo> Trading-Buddy
cd ~/Trading-Buddy
bash deploy/setup.sh        # installs Python, venv, all dependencies
```

---

## Part 3 — Get the project + secrets onto the server

**Option A — copy from your PC (no GitHub needed).** From PowerShell on your PC:
```powershell
# copies the whole project folder to the server's home dir as "Trading-Buddy"
scp -i "C:\path\to\your-key.key" -r "C:\Users\91890\Trading Buddy" ubuntu@YOUR_VM_IP:~/Trading-Buddy
```
Then on the server delete the Windows venv (it won't work on Linux) and re-run setup:
```bash
cd ~/Trading-Buddy && rm -rf .venv && bash deploy/setup.sh
```

**The `.env` file** (your secrets) must be on the server too. Because it's
git-ignored, copy it explicitly, or recreate it:
```bash
nano ~/Trading-Buddy/.env     # paste the same contents as your PC's .env, save (Ctrl-O, Enter, Ctrl-X)
```
**Add one new line for the dashboard password** (so it's not open on the internet):
```
DASHBOARD_PASSWORD=pick-a-strong-password
```

Verify everything is wired:
```bash
cd ~/Trading-Buddy
.venv/bin/python main.py --init     # should print READY ✅
```

---

## Part 4 — Run it as always-on services

```bash
cd ~/Trading-Buddy
bash deploy/install-services.sh
```
This starts two services that **auto-restart on crash and on reboot**:
- `trading-buddy` — the scheduler (auto-logs-in each morning, trades the sandbox)
- `trading-buddy-dashboard` — the web dashboard

Check them:
```bash
sudo systemctl status trading-buddy
journalctl -u trading-buddy -f      # live logs (Ctrl-C to stop watching)
```

The server's clock can be anything — the app forces IST internally, so market
hours (9:15–3:30 IST) are correct regardless of server timezone.

---

## Part 5 — Reach the dashboard from anywhere (securely)

**Recommended: Tailscale** (free, private, no open ports — only *your* devices can reach it).
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
Install the **Tailscale app** on your laptop + phone, log in with the same account.
Then open the dashboard at: `http://<server-tailscale-ip>:8000` from any of your
devices, anywhere — no public exposure.

**Simpler alternative (less secure): open the port.**
- Oracle console → your instance → **Virtual Cloud Network → Security List** →
  add an **Ingress rule**: source `0.0.0.0/0`, TCP port **8000**.
- On the server: `sudo ufw allow 8000`
- Open `http://YOUR_VM_PUBLIC_IP:8000` (the `DASHBOARD_PASSWORD` protects it).
- ⚠️ This is plain HTTP (password sent unencrypted). Fine for a quick start;
  use Tailscale, or ask me to add HTTPS (Caddy/Cloudflare Tunnel), for real use.

---

## Ongoing

- **Telegram alerts** keep working exactly as before (Tamil) — independent of the dashboard.
- **Update the code later:** copy changed files up (or `git pull`), then
  `sudo systemctl restart trading-buddy trading-buddy-dashboard`.
- **Daily login:** handled automatically by the scheduler (TOTP). Nothing to do.
- **Going LIVE (real money) later:** see `GO-LIVE-CHECKLIST.md` — needs the static
  IP whitelisted with the broker, `MODE="LIVE"`, and your explicit approval.

---

### What to do right now
1. Create the Oracle Cloud free account + VM (Part 1).
2. Tell me the **shape, OS version, and public IP** (not the private key — keep that secret).
3. I'll walk you through Parts 2–5 with exact commands for your setup.
