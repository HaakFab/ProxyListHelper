# Account & Domain Setup — Walkthrough

The one-time groundwork: domain, DNS, Hetzner account, SSH key, firewall. None of it depends on
the portal code, so it can all be done before the software exists — and doing it early removes
the slowest failure modes (DNS propagation, Hetzner identity checks) from your first real round.

Budget about an hour, most of it waiting.

> Secrets are **not** written into this file. They live in `~/.mtg-portal/env`, outside the
> repository. Never commit them.

---

## ✅ Already done on this Mac

| Thing | Location | Notes |
| --- | --- | --- |
| `hcloud` CLI | `/opt/homebrew/bin/hcloud` | manages the server from the terminal |
| `age` | `/opt/homebrew/bin/age` | encrypts database backups |
| SSH keypair | `~/.ssh/mtg_portal` (+`.pub`) | dedicated to the portal, separate from your personal key |
| Secrets file | `~/.mtg-portal/env` (chmod 600) | `SECRET_KEY`, `WORKER_TOKEN`, `ADMIN_PASSWORD`, `INVITE_CODE` |
| Backup key | `~/.mtg-portal/backup-key.txt` (chmod 600) | decrypts your backups |

The SSH **public** key (safe to share — this is the one you paste into Hetzner):

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC4LQLTHWmwI31hDVCSKAckz7pKxEcYQivfHKqHXnC4r mtg-portal
```

### ⚠️ Do this before anything else

**Copy `~/.mtg-portal/backup-key.txt` somewhere off this Mac** — password manager, USB stick,
anywhere that isn't this disk. It is the only thing that can decrypt your backups. If the Mac
dies and that key is gone, every payment record goes with it. There is no recovery path.

To see your invite code and admin password at any time:

```bash
cat ~/.mtg-portal/env
```

---

## Step 1 — Register a domain (~€5–10/year)

Register anywhere; DNS moves to Hetzner in Step 5, so the registrar hardly matters.

| Registrar | Notes |
| --- | --- |
| [INWX](https://www.inwx.de) | German, ~€5/yr for `.de` |
| [Netcup](https://www.netcup.de/domains/) | German, cheap |
| [Porkbun](https://porkbun.com) | International, at-cost, good panel |

Choose something unremarkable — this must not read as a shop. `haak-proxies.de` or similar.
The portal will live at `proxies.<yourdomain>`.

## Step 2 — Hetzner account

1. Sign up at **https://accounts.hetzner.com/signUp**
2. Verify email, add a payment method.
3. **New accounts go through an identity check that can take hours.** This is the main reason to
   do this step well before you need the server.
4. Open **https://console.hetzner.com** → **New Project** → name it `mtg-proxies`.
   (`console.hetzner.cloud` still redirects here, but this is the current canonical address —
   Hetzner merged the separate Cloud and DNS consoles into this one.)

## Step 3 — Add the SSH key

In the project: **Security** → **SSH Keys** → **Add SSH Key**.

Paste the public key from the top of this file. Name it `fabian` (the deploy commands refer to
it by that name).

## Step 4 — Create an API token

**Security** → **API Tokens** → **Generate API Token** → **Read & Write** → name `hcloud-cli`.

**Shown exactly once.** Copy it immediately, then:

```bash
hcloud context create mtg-proxies     # paste the token at the prompt
hcloud server list                    # empty list = working; an error = wrong token
```

## Step 5 — Move DNS to Hetzner

Hetzner DNS is free and lives inside the same unified console as your Cloud project (the
standalone `dns.hetzner.com` site no longer creates new zones — Hetzner folded DNS into
`console.hetzner.com`). It also has an API, which means the per-round A-record update can
eventually be scripted instead of being a manual step you have to remember at the worst moment.

1. At **https://console.hetzner.com**, open your `mtg-proxies` project → **DNS** in the left
   sidebar → **Add zone**.
2. Enter your domain, then choose **Create an empty zone** (not "Import zone file" or
   "Secondary" — those are for migrating DNS that already exists elsewhere). Hetzner generates
   the NS/SOA records and displays **three nameservers**.
3. At your registrar, replace its nameservers with those three. Propagation: minutes to hours.
4. In the Hetzner zone, add:

| Field | Value |
| --- | --- |
| Name | `proxies` |
| Type | `A` |
| Value | `192.0.2.1` *(placeholder; replaced each round)* |
| **TTL** | **300** |

### Why the TTL matters

TTL tells the world how long to cache the record. The common default is 3600 — one hour. Set it
on the morning you open a round and the new server's IP takes an hour to become visible, during
which the site looks broken and you go looking for a bug that isn't there.

At 300 it's five minutes. Set it now, so the old value has expired everywhere long before you
need it.

## Step 6 — Create the firewall

Once `hcloud` is logged in:

```bash
hcloud firewall create --name portal-fw
hcloud firewall add-rule portal-fw --direction in --protocol tcp --port 22 \
  --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall add-rule portal-fw --direction in --protocol tcp --port 80 \
  --source-ips 0.0.0.0/0 --source-ips ::/0
hcloud firewall add-rule portal-fw --direction in --protocol tcp --port 443 \
  --source-ips 0.0.0.0/0 --source-ips ::/0
```

Port 80 must stay open — Let's Encrypt uses it to issue the certificate. SSH is protected by
key-only authentication; narrow port 22 to your own IP only if it is static.

## Step 7 — Sign the data processing agreement

Hetzner account settings → **Data processing agreement / AVV** → accept.

One click. It is what makes Hetzner a lawful processor for the personal data you'll store.
Do it before any real person's data reaches the server.

## Step 8 — Fill in the two remaining values

```bash
# replace CHANGEME with your real values
nano ~/.mtg-portal/env
```

| Key | Value |
| --- | --- |
| `PAYPAL_ME_HANDLE` | the name in your `paypal.me/<handle>` link |
| `PORTAL_DOMAIN` | `proxies.yourdomain.de` |

---

## Verify

```bash
hcloud server list                        # authenticated, empty
hcloud firewall list                      # portal-fw exists
hcloud ssh-key list                       # "fabian" listed
dig +short proxies.yourdomain.de          # 192.0.2.1 (the placeholder)
dig +short proxies.yourdomain.de | wc -l  # 1
grep -c CHANGEME ~/.mtg-portal/env        # 0
```

All five behaving as described → account setup is complete.

---

## What's still missing

The portal application itself (`portal/`, `deploy/docker-compose.yml`, `deploy/Caddyfile`) is not
built yet. See [`portal_implementation.md`](../portal_implementation.md) for the build spec, and
[`SETUP.md`](SETUP.md) for the per-round spin-up and teardown procedure once it exists.

Nothing above needs revisiting when the code lands — this groundwork is done for good.
