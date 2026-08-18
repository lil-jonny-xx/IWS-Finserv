"""
IBKR SFTP delivery — read Flex statements IBKR drops on their own SFTP server.

WHY THIS EXISTS
    The Flex Web Service (ibkr.py) is hard-throttled: 1 req/sec, 10/min per token,
    and rapid same-query regeneration escalates 1001 → token-wide throttle → 1025
    lockout. IBKR's file-delivery service sidesteps that entirely — IBKR generates
    the statement on their schedule and leaves it on an SFTP server for us to
    collect. Collecting a file costs zero Flex quota.

MODEL — IB-hosted, we PULL (chosen 2026-08-18)
    IBKR runs the SFTP server; this box connects OUTBOUND and downloads. Nothing
    inbound is opened on the portal host. IBKR needs, from us:
      • an RSA public key (key-based auth; they do not issue passwords), and
      • the source IP address(es) we connect from, to whitelist.
    Provisioning is NOT self-service in Client Portal — it is arranged by email
    with filedelivery@interactivebrokers.com. See deploy/ibkr/SFTP.md.

PRECEDENCE (see ibkr._fetch_statement_xml)
    in-memory cache → SFTP delivered file → 6h disk refetch floor → live HTTP
    → stale disk fallback.

    The SFTP file only wins while it is FRESHER than _MAX_AGE (default: the same
    6h as the Flex refetch floor). That default is deliberate, not arbitrary:
    the 06:00 ET run finds IBKR's overnight file an hour or two old and uses it,
    spending no Flex quota; the 17:30 ET *provisional* run finds that same file
    ~11h old, rejects it, and falls through to a live pull — which is the whole
    point of the evening run, since it exists to surface the day's fills. A
    laxer gate would silently re-serve the morning snapshot every evening and
    quietly delete the provisional run's reason to exist.

DEPENDENCIES
    None added. Uses the system /usr/bin/sftp (OpenSSH, batch mode) and
    /usr/bin/gpg, both already present. paramiko/python-gnupg are deliberately
    NOT introduced into the production venv for this.

INERT UNTIL CONFIGURED
    Every entry point no-ops when the env is unset, so merging this changes
    nothing until IBKR provisions the account and .env is filled in. In
    particular a login is only ever served from SFTP once its own
    IBKR_{PREFIX}_SFTP_PATTERN is set — there is no filename guessing.

Env vars (global):
  IBKR_SFTP_HOST          — IBKR-provided hostname
  IBKR_SFTP_USER          — IBKR-provided username
  IBKR_SFTP_PORT          — default 22
  IBKR_SFTP_KEY           — path to our PRIVATE key (the RSA pair given to IBKR)
  IBKR_SFTP_KNOWN_HOSTS   — known_hosts file pinning IBKR's host key (required;
                            we do NOT accept unknown host keys)
  IBKR_SFTP_REMOTE_DIR    — remote directory to collect from (default: home dir)
  IBKR_SFTP_DROP_DIR      — local landing dir (default .ibkr_sftp next to the cache)
  IBKR_SFTP_MAX_AGE_SEC   — freshness gate (default: the Flex refetch floor, 6h)
  IBKR_SFTP_KEEP_DAYS     — prune downloaded files older than this (default 30)
  IBKR_SFTP_GPG_HOME      — GNUPGHOME holding our private key, if IBKR PGP-encrypts
  IBKR_SFTP_DISABLED      — set to 1 to bypass SFTP entirely (kill switch)

Env vars (per login prefix, e.g. DHR, DHR_2, HHR, SDR):
  IBKR_{PREFIX}_SFTP_PATTERN — glob matching that login's delivered file, e.g.
                               "U1234567.*.xml". Unset ⇒ that login never uses
                               SFTP. Run `python -m equity.brokers.ibkr_sftp --list`
                               once credentials exist to see the real filenames.
"""
import fnmatch
import logging
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

_SFTP_BIN = shutil.which("sftp") or "/usr/bin/sftp"
_GPG_BIN = shutil.which("gpg") or "/usr/bin/gpg"

# Suffixes IBKR may deliver when PGP encryption is enabled on the feed.
_PGP_SUFFIXES = (".gpg", ".pgp", ".asc")

# One sync per process: _fetch_statement_xml may be called once per login, but a
# single collection pass brings down every file. Re-syncing per login would open
# four SSH sessions for one worker run.
_synced = False


def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(f"IBKR_SFTP_{key}", default).strip()


def _drop_dir() -> Path:
    return Path(_cfg("DROP_DIR") or "/var/www/mis-portal/.ibkr_sftp")


def _max_age() -> float:
    """Freshness gate. Defaults to the Flex refetch floor so the evening
    provisional run still goes live — see the module docstring."""
    explicit = _cfg("MAX_AGE_SEC")
    if explicit:
        return float(explicit)
    return float(os.environ.get("IBKR_FLEX_MIN_REFETCH_SEC", str(6 * 3600)))


def configured() -> bool:
    """True only when the transport is fully specified AND not kill-switched."""
    if _cfg("DISABLED") in ("1", "true", "yes"):
        return False
    return bool(_cfg("HOST") and _cfg("USER") and _cfg("KEY"))


def pattern_for(acct_prefix: str) -> str:
    """Glob for this login's delivered file; empty ⇒ this login opts out of SFTP."""
    return os.environ.get(f"IBKR_{acct_prefix}_SFTP_PATTERN", "").strip()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _sftp_batch(commands: str, timeout: int = 180) -> str:
    """Run a batch of sftp commands against IBKR, non-interactively.

    BatchMode=yes guarantees we never block on a password/passphrase prompt in
    cron. StrictHostKeyChecking=yes + an explicit known_hosts means an unknown or
    CHANGED IBKR host key is a hard failure rather than a silent trust-on-first-use
    — this is a financial data feed, so a MITM must fail loudly, not warn.
    """
    host, user, key = _cfg("HOST"), _cfg("USER"), _cfg("KEY")
    port = _cfg("PORT") or "22"
    known_hosts = _cfg("KNOWN_HOSTS")

    opts = [
        "-b", "-",
        "-P", port,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ConnectTimeout={min(timeout, 30)}",
        "-o", "NumberOfPasswordPrompts=0",
    ]
    if key:
        opts += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if known_hosts:
        opts += ["-o", f"UserKnownHostsFile={known_hosts}"]
    else:
        # No pinned host key => refuse rather than fall back to blind trust.
        raise RuntimeError(
            "IBKR_SFTP_KNOWN_HOSTS is not set. Pin IBKR's host key first:\n"
            f"  ssh-keyscan -p {port} {host or '<ibkr-host>'} > /var/www/mis-portal/.ibkr_sftp_known_hosts\n"
            "then verify the fingerprint with IBKR before trusting it."
        )

    proc = subprocess.run(
        [_SFTP_BIN, *opts, f"{user}@{host}"],
        input=commands, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"sftp exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc.stdout


def list_remote() -> list[str]:
    """Filenames currently sitting on IBKR's SFTP server.

    Use this once IBKR provisions the account to discover the real delivered
    filenames, then set IBKR_{PREFIX}_SFTP_PATTERN accordingly.
    """
    if not configured():
        return []
    remote_dir = _cfg("REMOTE_DIR")
    cmds = (f"cd {remote_dir}\n" if remote_dir else "") + "ls -1\n" + "bye\n"
    out = _sftp_batch(cmds)
    names = []
    for line in out.splitlines():
        line = line.strip()
        # Skip the echoed prompts/commands sftp prints in batch mode.
        if not line or line.startswith("sftp>") or line in ("ls -1", "bye") or line.startswith("cd "):
            continue
        names.append(line.rsplit("/", 1)[-1])
    return names


def sync(force: bool = False) -> int:
    """Collect newly-delivered files into the local drop dir. Returns file count.

    Never raises into the caller's path: a delivery outage or a network blip must
    degrade to the existing HTTP Flex pull, not break the worker run.
    """
    global _synced
    if not configured():
        return 0
    if _synced and not force:
        return 0
    _synced = True

    drop = _drop_dir()
    try:
        drop.mkdir(parents=True, exist_ok=True)
        remote_dir = _cfg("REMOTE_DIR")
        # -p preserves the remote mtime, which is our freshness signal: it says
        # when IBKR produced the file, not when we happened to collect it.
        cmds = (f"cd {remote_dir}\n" if remote_dir else "") + f"get -p * {drop}\n" + "bye\n"
        _sftp_batch(cmds)
    except Exception as e:
        logger.warning(f"IBKR SFTP: collection failed ({e}); falling back to Flex Web Service")
        return 0

    n = _decrypt_pending(drop)
    _prune(drop)
    logger.info(f"IBKR SFTP: drop dir holds {n} statement file(s) at {drop}")
    return n


def _decrypt_pending(drop: Path) -> int:
    """Decrypt any PGP-encrypted deliveries in place, leaving plain .xml beside them.

    IBKR's file-delivery service can PGP-encrypt to a public key we supply. If the
    feed is configured without encryption this loop simply finds nothing to do.
    """
    count = 0
    for f in drop.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() in _PGP_SUFFIXES:
            plain = f.with_suffix("")
            if not plain.exists():
                try:
                    env = dict(os.environ)
                    if _cfg("GPG_HOME"):
                        env["GNUPGHOME"] = _cfg("GPG_HOME")
                    subprocess.run(
                        [_GPG_BIN, "--batch", "--yes", "--quiet",
                         "--output", str(plain), "--decrypt", str(f)],
                        capture_output=True, text=True, timeout=120, env=env, check=True,
                    )
                    # Carry the delivery time onto the plaintext so the freshness
                    # gate measures IBKR's generation time, not our decrypt time.
                    st = f.stat()
                    os.utime(plain, (st.st_atime, st.st_mtime))
                    logger.info(f"IBKR SFTP: decrypted {f.name}")
                except Exception as e:
                    logger.warning(f"IBKR SFTP: could not decrypt {f.name}: {e}")
                    continue
            count += 1
        elif f.suffix.lower() in (".xml", ".csv"):
            count += 1
    return count


def _prune(drop: Path) -> None:
    """Drop collected files past the retention window so the dir cannot grow without
    bound. Purely local housekeeping — nothing is deleted on IBKR's server."""
    try:
        keep = float(_cfg("KEEP_DAYS") or "30") * 86400
        if keep <= 0:
            return
        now = time.time()
        for f in drop.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > keep:
                f.unlink()
    except Exception as e:
        logger.debug(f"IBKR SFTP: prune skipped ({e})")


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def latest_statement(acct_prefix: str, query_id: str = ""):
    """Newest fresh, valid Flex XML delivered for this login → (root, age_seconds).

    Returns None — meaning "fall through to the HTTP Flex path" — when SFTP is
    unconfigured, this login has no pattern, nothing matches, everything matching
    is past the freshness gate, or the file does not parse as a Flex statement.
    """
    if not configured():
        return None
    pattern = pattern_for(acct_prefix)
    if not pattern:
        return None

    sync()
    drop = _drop_dir()
    if not drop.is_dir():
        return None

    max_age = _max_age()
    now = time.time()
    candidates = []
    for f in drop.iterdir():
        if not f.is_file() or f.suffix.lower() in _PGP_SUFFIXES:
            continue
        if not fnmatch.fnmatch(f.name, pattern):
            continue
        candidates.append((f.stat().st_mtime, f))

    if not candidates:
        logger.debug(f"[{acct_prefix}] IBKR SFTP: nothing matching {pattern!r} in {drop}")
        return None

    # Newest first — IBKR keeps prior days' files, and we want today's.
    for mtime, f in sorted(candidates, reverse=True):
        age = now - mtime
        if age > max_age:
            logger.info(
                f"[{acct_prefix}] IBKR SFTP: newest delivered file {f.name} is "
                f"{age/3600:.1f}h old (> {max_age/3600:.1f}h gate) — going to the "
                f"Flex Web Service for fresher data"
            )
            return None
        try:
            root = ET.fromstring(f.read_bytes())
        except Exception as e:
            logger.warning(f"[{acct_prefix}] IBKR SFTP: {f.name} is not parseable XML ({e})")
            continue
        # Same validity bar the HTTP path applies: a ready statement is rooted at
        # FlexQueryResponse. Anything else is an error/still-generating envelope.
        if root.tag != "FlexQueryResponse":
            logger.warning(
                f"[{acct_prefix}] IBKR SFTP: {f.name} is <{root.tag}>, not a ready "
                f"FlexQueryResponse — ignoring"
            )
            continue
        logger.info(
            f"[{acct_prefix}] IBKR SFTP: using delivered statement {f.name} "
            f"({age/3600:.1f}h old) — no Flex quota spent"
        )
        return root, age

    return None


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, "/var/www/mis-portal")
    from dotenv import load_dotenv
    load_dotenv("/var/www/mis-portal/.env", override=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ap = argparse.ArgumentParser(description="IBKR SFTP delivery helper")
    ap.add_argument("--list", action="store_true",
                    help="list filenames on IBKR's SFTP server (use to build the per-login patterns)")
    ap.add_argument("--sync", action="store_true", help="collect files into the local drop dir")
    ap.add_argument("--check", action="store_true",
                    help="report, per configured login, which delivered file would be used")
    args = ap.parse_args()

    if not configured():
        print("IBKR SFTP is not configured (need IBKR_SFTP_HOST, _USER, _KEY). Nothing to do.")
        sys.exit(0)

    if args.list:
        for name in list_remote():
            print(name)
    if args.sync:
        print(f"{sync(force=True)} usable file(s) in {_drop_dir()}")
    if args.check:
        from equity.brokers import ibkr
        for code in ("DHR", "DHR_2", "HHR", "SDR"):
            pat = pattern_for(code)
            if not pat:
                print(f"{code:8s} no IBKR_{code}_SFTP_PATTERN — stays on the Flex Web Service")
                continue
            got = latest_statement(code)
            print(f"{code:8s} {pat:28s} " + ("MISS — would use HTTP" if not got
                                             else f"HIT — {got[1]/3600:.1f}h old"))
