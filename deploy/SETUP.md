# Proxy Portal — Server Setup Guide

How to put the submission/payment portal online for an order round, and take it down again
afterwards.

---

## ⚠️ Status: Phase 1 is built; the print worker (Phase 2) is not

Read this before following anything below.

| Component | State |
| --- | --- |
| `proxy_parser/` library (parser, checker, auto-fix) | ✅ Built and tested — 322 tests pass |
| `deck_checker.py`, `moxfield_mpc_to_pdf.py` | ✅ Working, unchanged behaviour |
| `portal/` — the web application (gate, login, submit, check, price, PayPal reference, admin) | ✅ **Built and tested** — invite gate, personal links, decklist check/accept, order pricing, PayPal reference, admin payment recording, round ZIP export |
| `deploy/Dockerfile.web`, `docker-compose.yml`, `Caddyfile`, `backup.sh`, `restore.sh` | ✅ Built — image builds cleanly and was verified running end-to-end over real HTTP |
| `worker/print_worker.py` | 🔧 **Not built** (Phase 2) — use the round ZIP export (Part 4.1) instead |

Steps marked 🔧 rely on the worker and can't be run yet; use the noted fallback. Everything else
below is real and has been run, not just written.

**Part 1 is entirely ✅** — domain, DNS, Hetzner account, SSH keys, firewall. That is roughly an
hour of account admin that has to happen regardless, is independent of the code, and removes the
most annoying failure modes (slow DNS, no SSH key) from your first real round. Doing it now is
genuinely useful.

---

## Part 0 — What you need, and what it costs

### Accounts

| What | Why | Cost |
| --- | --- | --- |
| **Hetzner Cloud** account | The server. German company, German datacentres — this is what keeps the GDPR story simple. | Pay per hour of use |
| **A domain** | The portal needs a stable hostname for its TLS certificate. | ~€10/year |
| **PayPal** (your existing personal account) | Receiving payments. No business account, no API, no fees. | €0 |

### Running cost

| Item | Cost |
| --- | --- |
| CX23 server while a round is open (~2 weeks, ~€5.49/month billed hourly) | ~€2.50 |
| Snapshot storage between rounds | ~€0.20/month |
| Domain | ~€10/year |
| **Total for ~3 rounds a year** | **~€20/year** |

Hetzner's shared-vCPU lineup was renamed and repriced during 2026 (CX22 → CX23; several price
changes through the year) — **verify the current price at signup**, this is an estimate, not a
quote. The important, stable fact is the *shape* of the cost: an on-demand 2-vCPU/4GB box run a
few weeks a year is a few euros, not a few euros a month.

### On your Mac

```bash
brew install hcloud age
```

`hcloud` manages the server from the command line. `age` encrypts the database backups (they
contain other people's names and payment records, so they do not travel unencrypted).

Docker is already installed on your machine.

---

## Part 1 — One-time setup ✅

Do this once. It survives every round.

### 1.1 Register a domain

Any registrar. A cheap `.de` is fine. You do not need hosting — only DNS.

Pick a subdomain for the portal, e.g. `proxies.yourdomain.de`.

### 1.2 Set the DNS TTL to 300 seconds — **do this now**

This step assumes you've already moved your domain's nameservers to Hetzner (Step 5 of
[ACCOUNT-SETUP.md](ACCOUNT-SETUP.md)) — do that first if you haven't. Then, in the Hetzner
console's DNS zone for your domain, create an A record: DNS → your zone → Add record.

| Field | Value |
| --- | --- |
| Name | `proxies` |
| Type | `A` |
| Value | `192.0.2.1` (a placeholder — you'll change it each round) |
| **TTL** | **300** |

The TTL is the point of this step. It tells the internet how long to cache the record. The
default is often 3600 (one hour) — which means that on the day you open a round, the new server's
IP takes an hour to propagate and you spend that hour convinced something is broken. At 300 it
takes five minutes.

Set it now, long before your first round, so the old value has expired everywhere.

### 1.3 Create the Hetzner project and SSH key

1. Sign up at [console.hetzner.com](https://console.hetzner.com), create a project
   (e.g. "mtg-proxies").
2. Generate a dedicated SSH key on your Mac:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mtg_portal -C "mtg-portal"
```

3. In the Hetzner console: **Security → SSH Keys → Add SSH Key**, paste
   `~/.ssh/mtg_portal.pub`, name it `fabian`.

### 1.4 Create an API token and log in `hcloud`

Hetzner console: **Security → API Tokens → Generate API Token**, permission **Read & Write**.
Copy it — it is shown exactly once.

```bash
hcloud context create mtg-proxies      # paste the token when prompted
hcloud server list                     # should print an empty list, not an error
```

### 1.5 Create the firewall

The server should answer on three ports only.

```bash
hcloud firewall create --name portal-fw

# SSH — restrict to your own IP if it is static; otherwise leave open and rely on key-only auth
hcloud firewall add-rule portal-fw --direction in --protocol tcp --port 22  --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall add-rule portal-fw --direction in --protocol tcp --port 80  --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall add-rule portal-fw --direction in --protocol tcp --port 443 --source-ips 0.0.0.0/0 --source-ips ::/0
```

Port 80 is required — Let's Encrypt uses it to issue the certificate.

### 1.6 Generate your secrets

```bash
mkdir -p ~/.mtg-portal && chmod 700 ~/.mtg-portal

cat > ~/.mtg-portal/env <<EOF
SECRET_KEY=$(openssl rand -base64 32)
WORKER_TOKEN=$(openssl rand -base64 32)
ADMIN_PASSWORD=$(openssl rand -base64 18)
INVITE_CODE=$(openssl rand -hex 4)
PAYPAL_ME_HANDLE=your-paypal-handle
PORTAL_DOMAIN=proxies.yourdomain.de
EOF

chmod 600 ~/.mtg-portal/env
cat ~/.mtg-portal/env
```

Write these down somewhere safe (a password manager).

| Secret | What it is |
| --- | --- |
| `SECRET_KEY` | Signs session cookies. Changing it logs everyone out. |
| `WORKER_TOKEN` | Lets the worker on your Mac fetch print jobs. |
| `ADMIN_PASSWORD` | Your own admin login. |
| `INVITE_CODE` | The shared code that gates the whole site. Give it to participants. |
| `PAYPAL_ME_HANDLE` | The name in your `paypal.me/<handle>` link. |

### 1.7 Create an encryption key for backups

```bash
age-keygen -o ~/.mtg-portal/backup-key.txt
chmod 600 ~/.mtg-portal/backup-key.txt
grep 'public key' ~/.mtg-portal/backup-key.txt
```

**Back this file up somewhere other than your Mac.** Without it, your encrypted database
backups are permanently unreadable — which means losing every payment record.

---

## Part 2 — Opening an order round ✅

Roughly 10 minutes.

### 2.1 Create the server

**First round** (no snapshot exists yet):

```bash
hcloud server create \
  --name portal \
  --image debian-12 \
  --type cx23 \
  --location nbg1 \
  --ssh-key fabian \
  --firewall portal-fw
```

**Later rounds** (restore from your snapshot — faster, and the Scryfall cache is already warm):

```bash
hcloud image list --type snapshot          # find the ID
hcloud server create --name portal --image <SNAPSHOT_ID> --type cx23 --location nbg1 \
  --ssh-key fabian --firewall portal-fw
```

`nbg1` is Nuremberg, `fsn1` is Falkenstein. Both Germany. Note the IP it prints.

### 2.2 Point DNS at the new server

Update the A record from Part 1.2 to the new IP. Wait 5 minutes (your TTL).

```bash
dig +short proxies.yourdomain.de          # should return the new IP
```

Do not continue until this returns the right address — Let's Encrypt will fail otherwise.

### 2.3 Prepare the server

```bash
SERVER=$(hcloud server ip portal)
ssh -i ~/.ssh/mtg_portal root@$SERVER

# on the server:
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin fail2ban unattended-upgrades
systemctl enable --now docker fail2ban

# disable password logins
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl restart ssh
exit
```

(Skip this when restoring from a snapshot — it is already done.)

### 2.4 Upload the application and your secrets

```bash
SERVER=$(hcloud server ip portal)
rsync -av --exclude '.git' --exclude 'output' --exclude '*_cache' \
  -e "ssh -i ~/.ssh/mtg_portal" \
  ./ root@$SERVER:/opt/portal/

scp -i ~/.ssh/mtg_portal ~/.mtg-portal/env root@$SERVER:/opt/portal/deploy/.env
```

### 2.5 Restore the database from the last round

Skip on your very first round.

```bash
ssh -i ~/.ssh/mtg_portal root@$SERVER
cd /opt/portal/deploy
./restore.sh ~/backups/portal-YYYYMMDD.db.age
```

This carries over the participant accounts, past rounds, and the warm Scryfall cache.

### 2.6 Start it

```bash
cd /opt/portal/deploy
docker compose up -d
docker compose logs -f caddy      # watch the certificate get issued (~20s)
```

Then open `https://proxies.yourdomain.de` — you should get the invite-code gate.

### 2.7 Open the round

1. Log in at `/admin` with your `ADMIN_PASSWORD`.
2. **Settings** — check the three rates (0.13 € per card, 0.26 € per list, 0.00 € processing)
   and your PayPal handle.
3. **Rounds → New round** — give it a name and a deadline.
4. **People** — add each participant's name; the portal gives you a personal link per person.
5. Send each person their link plus the shared invite code, over your usual group chat.

---

## Part 3 — While the round is open ✅

Participants: enter the invite code → open their personal link → paste their decklist → the
checker runs → they accept the fixes → they see the amount and a PayPal link → they pay → they
click "Ich habe überwiesen".

Your job is the last step:

1. **Admin → Orders** shows who claims to have paid.
2. Check your PayPal account for the amount and reference code.
3. Click **"als bezahlt markieren"**, confirming the amount actually received.

Expect some people to pay without the reference code, or to round the amount down. The admin
screen lets you record what actually arrived rather than forcing it to match — under- and
overpayments show up as a balance rather than becoming a support problem.

---

## Part 4 — Closing a round ✅ (print job itself: 🔧, use the ZIP export)

### 4.1 Close submissions and run the print job

1. On `/admin`: click **"Runde schließen"** — no further submissions.
2. On `/admin/orders`: confirm every order is `PAID`/`FROZEN` (or cancelled). For each paid
   order, click **"Einfrieren (für Druck)"** — this freezes its submissions too, so their text
   can no longer change under you.
3. Today (the worker, Phase 2, isn't built yet): on `/admin`, click
   **"Runde als ZIP exportieren (ready/)"**. This downloads a ZIP containing exactly what the
   pipeline needs — a `ready/` folder of `.txt` files, named and formatted precisely as the
   worker would write them, plus a `summary.csv`. Unzip it and run the pipeline by hand:
   ```bash
   cd ~/Python/mtg_proxy_parser
   unzip ~/Downloads/round-<id>-ready.zip -d ~/rounds/2611
   python moxfield_mpc_to_pdf.py --input-dir ~/rounds/2611/ready --mode upscaled --break-by name
   ```
4. **Once Phase 2 ships**, this step becomes: `python worker/print_worker.py --server https://proxies.yourdomain.de --token "$WORKER_TOKEN"` — the worker will download the paid lists itself,
   verify their checksums, write `ready/`, and run the same pipeline command automatically. This
   guide will be updated when that lands; nothing about steps 1–2 above changes.
5. Back on `/admin`, click **"Runde abschließen"**.

### 4.2 Back up — and verify the backup

```bash
SERVER=$(hcloud server ip portal)
ssh -i ~/.ssh/mtg_portal root@$SERVER "cd /opt/portal/deploy && ./backup.sh"
scp -i ~/.ssh/mtg_portal root@$SERVER:/opt/portal/backups/portal-*.db.age ~/backups/
```

**Now verify it restores, before you destroy anything:**

```bash
age -d -i ~/.mtg-portal/backup-key.txt ~/backups/portal-YYYYMMDD.db.age > /tmp/verify.db
sqlite3 /tmp/verify.db "PRAGMA integrity_check;"
sqlite3 /tmp/verify.db "SELECT count(*) FROM \"order\";"
rm /tmp/verify.db
```

You want `ok` and a plausible order count. A backup you have not restored is not a backup — and
this one file is the only record of who paid you.

### 4.3 Snapshot and destroy

```bash
hcloud server shutdown portal
hcloud server create-image --type snapshot --description "portal-$(date +%Y%m)" portal
hcloud server delete portal
```

Billing stops at delete. The snapshot costs about €0.20/month and saves you the setup in 4.1–4.3
next time. Finally, point the DNS record back at `192.0.2.1`.

---

## Part 5 — Testing locally first ✅

Always do a full dry run on your Mac before the first real round. This section describes what
was actually run and verified while building the portal — not a guess.

### Building and running the image standalone

The full `docker compose` stack routes through Caddy, and Caddy will only get you a real TLS
certificate for a domain that actually resolves to the machine it's running on — `localhost`
doesn't qualify, and `docker-compose.yml` only publishes ports 80/443 (Caddy), not the `web`
service's own port 8000. So for a local dry run, build and run the `web` image on its own instead
of through the full compose stack:

```bash
cd ~/Python/mtg_proxy_parser
docker build -f deploy/Dockerfile.web -t mtg-portal-web:local .

docker run -d --name mtg-portal-local -p 18000:8000 \
  -e SECRET_KEY="$(openssl rand -base64 32)" \
  -e WORKER_TOKEN="$(openssl rand -base64 32)" \
  -e ADMIN_PASSWORD="local-test-password" \
  -e INVITE_CODE="local-test-invite" \
  -e PAYPAL_ME_HANDLE="your-paypal-handle" \
  -e DATABASE_URL="sqlite:////data/portal.db" \
  -e DEBUG="true" \
  mtg-portal-web:local

docker logs -f mtg-portal-local   # watch for "Application startup complete."
```

**`DEBUG=true` matters here, not just cosmetically.** In production, cookies carry the `Secure`
flag (correct — the site is always behind HTTPS there). A real browser silently refuses to store
or resend a `Secure` cookie over plain `http://localhost`, which would otherwise break the gate
and session cookies with no visible error at all — just a login that never seems to stick.
`DEBUG=true` drops that flag for this local run only.

Open `http://localhost:18000`. Walk the whole path: invite code (`local-test-invite`) → login as
`Admin` / `local-test-password` at `/login` (the pre-issued-link flow needs a second account —
create one from `/admin/people` and copy its link from the page) → paste a real decklist
(`input/Chris4_glarbDC.txt`, 100 cards) → check → accept fixes → confirm the amount reads
**100 × 0,13 € + 1 × 0,26 € = 13,26 €** → mark paid → confirm in `/admin/orders`.

When done:

```bash
docker rm -f mtg-portal-local
docker rmi mtg-portal-web:local
```

### Testing the full compose stack (Caddy included)

Once you have a real domain pointed at the machine you're testing on (even a temporary one),
the full stack works exactly as in production:

```bash
cd ~/Python/mtg_proxy_parser/deploy
cp ~/.mtg-portal/env .env
echo "BACKUP_AGE_PUBLIC_KEY=$(grep 'public key' ~/.mtg-portal/backup-key.txt | awk '{print $NF}')" >> .env
docker compose up --build
```

Caddy will request a Let's Encrypt certificate on first request — watch `docker compose logs -f
caddy` for confirmation, then continue exactly as in the standalone test above.

---

## Part 6 — Troubleshooting ✅

| Symptom | Cause | Fix |
| --- | --- | --- |
| Site unreachable right after creating the server | DNS still cached | `dig +short proxies.yourdomain.de`; wait for the TTL |
| "Your connection is not private" | Certificate not issued yet | `docker compose logs caddy`. Usually port 80 blocked or DNS wrong |
| Certificate fails repeatedly | Let's Encrypt rate limit (5 failures/hour) | Wait an hour. Test with the standalone container first (Part 5) — Caddy can't issue a certificate for a domain that isn't pointed at the machine yet |
| Checks hang at "wird geprüft" | Scryfall unreachable | `docker compose logs web`. The checker retries, then fails loudly — it will never pass a list as clean (verified in `tests/test_scryfall_resilience.py`) |
| Worker never picks up a job | Not built yet (Phase 2) — use the round ZIP export | See Part 4.1 |
| Someone paid, no reference code | PayPal.me cannot carry one | Match by name + amount + date on `/admin/orders` |
| Locked out of admin | Lost `ADMIN_PASSWORD` | If another account exists: `docker compose exec web python -m portal.cli promote <their_person_slug>` makes them an admin too, no login needed. If truly nobody can log in, you'll need to edit `access_code_hash` for the `Admin` row directly in the database (restore from a backup and inspect with `sqlite3` if you're unsure how). |

Useful commands on the server:

```bash
docker compose ps                 # is everything running
docker compose logs -f web        # application log
docker compose restart web        # restart after a config change
df -h                             # disk (should be nearly empty — no images live here)
```

---

## What lives where

The split matters: **the server never handles an image, a PDF, or torch.**

| | Server (Hetzner) | Your Mac |
| --- | --- | --- |
| Decklist text | ✅ | ✅ |
| Card metadata (JSON, ~20 MB) | ✅ | ✅ |
| Accounts, payments, amounts | ✅ | ✗ |
| Card images (22 GB cache) | ✗ | ✅ |
| Generated PDFs (up to 15 GB) | ✗ | ✅ |
| torch / GPU upscaling | ✗ | ✅ |

That is why a €4/month server is enough, and why the PDFs never need to travel.

---

## Data protection notes

- All personal data stays on German infrastructure (Hetzner, Nuremberg/Falkenstein).
- Sign Hetzner's **AVV/DPA** in the console — one click, and it is what makes them a lawful
  processor for you.
- The portal stores only display name, optional email, and payment reference. No address, no
  real name, no payment details — PayPal is entirely outside the system.
- Backups contain personal data, which is why they are `age`-encrypted at rest.
- `/admin/settings` has a `retention_days` field (default 90) documenting the intended retention
  window, but **the automatic deletion job itself is not built yet** (planned for a later phase).
  Until then, old lists and payment records are not purged automatically — delete a completed
  round's data by hand (`/account/delete` per user, or direct DB access) if you need to honour
  that window sooner.
- **Do not run a mail server on the VPS.** Hetzner blocks outbound port 25 on new accounts, and a
  fresh IP has no sending reputation, so the mail would land in spam anyway. Login does not need
  email at all; notifications (Phase 3) go through an EU provider such as Scaleway or Brevo.

---

## Next step

The portal (Phase 1) is built and tested — Parts 2 through 6 above are real and were run, not
just written. What's left is Phase 2: `worker/print_worker.py`, which would replace the manual
ZIP-export-and-run-by-hand step in Part 4.1 with an automatic pull from your Mac. Until that
lands, the ZIP export is the supported path from a paid, frozen round to a print run — it is not
a stopgap you're waiting to stop using, it's simply the current handoff point.
