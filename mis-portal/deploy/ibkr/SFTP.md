# IBKR SFTP delivery — runbook

Collecting a delivered file costs **zero Flex quota**. This is the structural fix for
the `1001 → 1025` throttle lockouts that the Flex Web Service path works around with
pacing, refetch floors and disk caches.

**Model: IB-hosted, we pull.** IBKR runs the SFTP server; this box connects *outbound*
and downloads. Nothing inbound is opened on the portal host — the box that holds the
whole portfolio database never accepts a third-party connection.

```
  portal host ──outbound :22──▶ IBKR SFTP        (we authenticate with our RSA key)
                (collect XML)
```

Code: `equity/brokers/ibkr_sftp.py`, wired into `equity/brokers/ibkr.py::_fetch_statement_xml`.

---

## Status

**Not yet live.** Everything is merged and inert. IBKR must provision the account
before anything happens; until `IBKR_SFTP_HOST/_USER/_KEY` are set in `.env`, every
login uses the existing Flex Web Service path exactly as before.

---

## 1. Provisioning (must be done by email — there is no Client Portal switch)

SFTP delivery is **not** self-service. Send this to
**filedelivery@interactivebrokers.com** (cc `reportingintegration@interactivebrokers.com`):

> Subject: SFTP credentials request — Flex Query delivery
>
> Hello,
>
> We would like to receive our daily Activity Flex Query statements via IBKR-hosted
> SFTP rather than polling the Flex Web Service.
>
> - Use: Flex Queries (daily Activity statement — Open Positions, Cash Report, Trades)
>     <account numbers — see the IBKR_SFTP scope block in mis-portal/.env>
>   (Please note we are deliberately NOT requesting delivery for the two accounts
>    listed as EXCLUDED in that block.)
> - Delivery format: XML
> - We will connect outbound to your SFTP server to collect the files.
>
> For key-based authentication, our RSA public key is:
>
> ssh-rsa AAAAB3NzaC1yc2EA...  iws-mis-portal-ibkr-sftp
> (paste the full contents of /home/SAdmin/.ssh/ibkr_sftp_rsa.pub)
>
> The source IP address we will connect from is: <see IBKR_SFTP block in .env>
>
> Please confirm the hostname, port, username, remote directory, the delivery
> schedule, and the SSH host key fingerprint we should expect.
>
> Also please confirm whether PGP encryption is applied to the delivered files; if it
> is mandatory we will send a PGP public key separately.

Ask them explicitly for **the host key fingerprint** — step 3 refuses to connect
without pinning it, by design.

### Scope requested

| Login | Account(s) |
|---|---|
The exact account numbers live in the `IBKR_SFTP` scope block in `mis-portal/.env`
(gitignored) — **this repo is public, so they are deliberately not written here.**

| Login | Scope |
|---|---|
| DHR (1st) | pending — not stored in the portal; read from the next `.ibkr_statements/DHR_*.xml` or Client Portal |
| DHR (2nd), HHR | one account each |
| SDR | four of six accounts |
| SDR — **excluded** | two accounts deliberately out of scope (owner's instruction, 2026-08-18) |

A ready-to-send draft with the numbers filled in is generated at
`deploy/ibkr/email-to-ibkr.txt`, which is gitignored for the same reason.

### The IP whitelist

The source IP (recorded in the `IBKR_SFTP` block in `.env`, not here — public repo)
is **confirmed reserved** for this VM with the hosting provider (2026-08-18). It is
still DHCP-assigned on `eth0`, so if the VM is ever rebuilt or
migrated, re-confirm the reservation before assuming delivery still works: a changed
IP means IBKR's whitelist rejects us silently and every login degrades to the HTTP
Flex path — degraded, not broken, and easy to miss.

### Worth raising in the same email

One SDR account returns zero `OpenPositions` over the Flex Web Service — the known
IBKR-side gap in SDR's direct equity. Since you are already opening a ticket with
their reporting team, ask whether the SFTP-delivered statement for that account
includes the positions the Web Service omits.

---

## 2. Keys

Already generated, deliberately **outside** the repo (this repo is public):

| Path | Mode | Purpose |
|---|---|---|
| `/home/SAdmin/.ssh/ibkr_sftp_rsa` | `600` | private key — never leaves this box |
| `/home/SAdmin/.ssh/ibkr_sftp_rsa.pub` | `644` | **this is what you send IBKR** |

Fingerprint: `SHA256:ZtolInM9JQ3Egq+7gnG8g1MrIZWL+vXQ93CJcZKC9MQ`

No passphrase — the systemd unit runs unattended under cron and OpenSSH batch mode
cannot answer a prompt. The key's only capability is reading our own statements off
IBKR's server.

---

## 3. Once IBKR replies

```bash
# a) Pin IBKR's host key, then VERIFY the fingerprint against what IBKR told you.
ssh-keyscan -p 22 <ibkr-host> > /home/SAdmin/.ssh/ibkr_sftp_known_hosts
ssh-keygen -lf /home/SAdmin/.ssh/ibkr_sftp_known_hosts     # compare with IBKR's email

# b) Fill in the IBKR_SFTP_* block in mis-portal/.env (already there, commented out).

# c) Discover the REAL delivered filenames — do not guess them.
/var/www/.venv/bin/python -m equity.brokers.ibkr_sftp --list

# d) Set one IBKR_{PREFIX}_SFTP_PATTERN per login from what (c) printed, e.g.
#    IBKR_DHR_SFTP_PATTERN=U1234567.*.xml

# e) Collect and confirm which file each login would use.
/var/www/.venv/bin/python -m equity.brokers.ibkr_sftp --sync
/var/www/.venv/bin/python -m equity.brokers.ibkr_sftp --check
```

A login is served from SFTP **only** when its own pattern is set, so you can migrate
one account at a time and leave the rest on HTTP.

### Dry run before trusting it

```bash
cd /var/www/mis-portal && /var/www/.venv/bin/python workers/ibkr_holdings_cash_worker.py
```

(no `--commit`). Look for `IBKR SFTP: using delivered statement ... no Flex quota spent`.

---

## 4. Why the freshness gate is 6h

`IBKR_SFTP_MAX_AGE_SEC` defaults to `IBKR_FLEX_MIN_REFETCH_SEC` (6h). This is load-bearing:

- **06:00 ET run** (authoritative) — IBKR's overnight file is ~1–2h old → used, no Flex call.
- **17:30 ET run** (provisional) — that same file is ~11h old → **rejected**, falls through
  to a live pull, which is the entire reason the evening run exists: to surface the day's
  fills. Raising the gate above ~9h would silently re-serve the morning snapshot every
  evening and quietly cancel that run's purpose.

If IBKR also delivers an intraday/evening file, it will be fresh and used automatically.

---

## 5. Failure behaviour

Every failure degrades to the existing HTTP path rather than breaking the run:

| Situation | Result |
|---|---|
| SFTP unconfigured | HTTP path, unchanged |
| Host unreachable / auth rejected / IP no longer whitelisted | warning logged, HTTP path |
| Host key unknown or **changed** | hard connection failure, warning, HTTP path |
| Delivered file older than the gate | HTTP path (logged with the age) |
| File is an error envelope or unparseable | skipped with a warning, HTTP path |
| Login has no `_SFTP_PATTERN` | HTTP path |

**Kill switch:** `IBKR_SFTP_DISABLED=1` in `.env` forces every login back to HTTP
immediately, no code change and no redeploy.

Host-key checking is strict on purpose (`StrictHostKeyChecking=yes`, no trust-on-first-use).
For a financial data feed a MITM must fail loudly and fall back, not warn and continue.

---

## 6. What is NOT changed

- No new Python dependency — uses the system `sftp` and `gpg` binaries, not paramiko.
- Date-ranged backfill queries (`ibkr_backfill_inception.py`) never use SFTP; a delivered
  file covers IBKR's own fixed period and cannot satisfy an arbitrary `fd`/`td` window.
- A statement sourced from SFTP is still saved to `.ibkr_statements/`, so the existing
  stale-disk fallback keeps working identically.
- The drop directory `mis-portal/.ibkr_sftp/` is gitignored — it holds real positions,
  cash and trades, and this repo is public.
