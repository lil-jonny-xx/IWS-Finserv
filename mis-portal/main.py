from fastapi import FastAPI, HTTPException, Header, Request, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
import json
import re
import jwt
import bcrypt
from datetime import datetime, timedelta, date
import os
import uuid
import mimetypes
from dotenv import load_dotenv
import psycopg2
import psycopg2.pool  # required: psycopg2.pool is a submodule, not exposed by `import psycopg2` alone
from psycopg2.extras import RealDictCursor
import redis
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
import logging
import hashlib
import hmac
import atexit

from assistant import engine as assistant_engine
from assistant import persistence as assistant_persistence

# Load .env file
load_dotenv('/var/www/mis-portal/.env')

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="IWS MIS Portal API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Redis connection
try:
    redis_client = redis.Redis(
        host='localhost',
        port=6379,
        db=0,
        password=os.getenv("REDIS_PASSWORD", ""),
        decode_responses=True
    )
    redis_client.ping()
    logger.info("Redis connection successful")
except Exception as e:
    logger.error(f"Redis connection failed: {e}")
    redis_client = None

# Database connection pool
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
        connect_timeout=5
    )
    logger.info("Database connection pool created successfully")
except Exception as e:
    logger.error(f"Failed to create database pool: {e}")
    db_pool = None

def shutdown_handler():
    """Gracefully close connections on shutdown."""
    if db_pool:
        db_pool.closeall()
        logger.info("Database connection pool closed gracefully")
    if redis_client:
        redis_client.close()
        logger.info("Redis connection closed gracefully")

atexit.register(shutdown_handler)

def real_ip(request: Request):
    # CF-Connecting-IP is set by Cloudflare and cannot be spoofed by clients
    # (nginx allowlist ensures only Cloudflare IPs reach this server)
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    return request.client.host

limiter = Limiter(
    key_func=real_ip,
    storage_uri="redis://:{}@localhost:6379".format(os.getenv("REDIS_PASSWORD", ""))
)

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://iwsfinserv.com",
        "https://www.iwsfinserv.com",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise ValueError("FATAL: JWT_SECRET not set.")

# Access-token lifetime. Was a hardcoded 15 minutes, which logged active users out
# mid-session (the token expires and there is no refresh — _require_auth just 401s).
# Default to an 8-hour working session; override via env without a code change.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
ACCESS_TOKEN_EXPIRE_SECONDS = ACCESS_TOKEN_EXPIRE_MINUTES * 60

# Optional shared secret for the Dhan order-update webhook. Dhan postbacks carry no
# provider signature and the endpoint is necessarily unauthenticated (no JWT), so when
# this is set we require the caller to present the same secret (header or ?token=) and
# reject anything else — preventing forged order events once the handler does real work.
# Left unset = backward-compatible (endpoint stays open, logs a warning).
DHAN_POSTBACK_SECRET = os.getenv("DHAN_POSTBACK_SECRET")

def get_db_connection():
    """Get connection from pool with query timeout."""
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable.")
    try:
        conn = db_pool.getconn()
        cursor = conn.cursor()
        cursor.execute("SET statement_timeout = '30000'")
        cursor.close()
        return conn
    except Exception as e:
        logger.error(f"Failed to get DB connection from pool: {e}")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable.")

def release_db_connection(conn):
    """Return connection to pool."""
    if db_pool and conn:
        db_pool.putconn(conn)

def write_audit_log(conn, user_id, action, table_name, record_id=None, details=None):
    """Write to audit_log table."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO audit_log (user_id, action, table_name, record_id, new_value, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (user_id, action, table_name, record_id, details, datetime.utcnow())
        )
        cursor.close()
    except Exception as e:
        conn.rollback()
        logger.error(f"Audit log write failed: {e}")

def _token_key(token: str) -> str:
    # Hash the JWT before using as Redis key — prevents large-key DoS
    # and avoids storing raw tokens in Redis memory/logs.
    return "blacklist:" + hashlib.sha256(token.encode()).hexdigest()

def is_token_blacklisted(token: str) -> bool:
    if redis_client is None:
        logger.error("Redis unavailable - denying token as precaution")
        return True
    try:
        return redis_client.exists(_token_key(token)) > 0
    except Exception as e:
        logger.error(f"Redis blacklist check failed: {e}")
        return True

def blacklist_token(token: str, expiry_seconds: Optional[int] = None) -> bool:
    if redis_client is None:
        logger.error("Redis unavailable - cannot revoke token")
        return False
    try:
        if expiry_seconds is None:
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"],
                                     options={"verify_exp": False})
                exp = payload.get("exp")
                if exp:
                    ttl = int(exp - datetime.utcnow().timestamp())
                    expiry_seconds = max(ttl, 1)
                else:
                    expiry_seconds = ACCESS_TOKEN_EXPIRE_SECONDS
            except Exception:
                expiry_seconds = ACCESS_TOKEN_EXPIRE_SECONDS
        redis_client.setex(_token_key(token), expiry_seconds, "1")
        return True
    except Exception as e:
        logger.error(f"Redis blacklist set failed: {e}")
        return False

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False

def get_token_from_request(request: Request, authorization: Optional[str] = None) -> str:
    """Extract token from cookie or Authorization header."""
    token = request.cookies.get("access_token")
    if token:
        return token
    if authorization:
        try:
            return authorization.split(" ")[1]
        except IndexError:
            pass
    raise HTTPException(status_code=401, detail="Not authenticated")

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)

@app.api_route("/api/ping", methods=["GET", "HEAD"])
def ping():
    """Public health check for uptime monitoring (GET + HEAD for UptimeRobot)."""
    return {"status": "ok"}

@app.get("/api/v1/health")
def health_check(request: Request, authorization: Optional[str] = Header(None)):
    """Detailed health check - requires authentication."""
    try:
        token = get_token_from_request(request, authorization)
        if is_token_blacklisted(token):
            raise HTTPException(status_code=401, detail="Token has been revoked")
        jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    db_ok = True
    redis_ok = True
    conn = None

    try:
        conn = get_db_connection()
    except Exception:
        db_ok = False
    finally:
        release_db_connection(conn)

    try:
        if redis_client:
            redis_client.ping()
    except Exception:
        redis_ok = False

    return {
        "status": "healthy" if db_ok and redis_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "cache": "ok" if redis_ok else "error",
        "version": "1.0.0",
        "environment": "production"
    }

def _login_impl(request: Request, login_request: LoginRequest, response: Response):
    email = login_request.email.lower()
    password = login_request.password

    conn = get_db_connection()

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email, password_hash, role, failed_attempts, locked_until FROM users WHERE email = %s AND is_active = TRUE",
            (email,)
        )
        user_row = cursor.fetchone()
        cursor.close()

        if not user_row:
            logger.info(f"Failed login attempt for: {email}")
            raise HTTPException(status_code=401, detail="Invalid email or password")

        if user_row.get("locked_until") and user_row["locked_until"] > datetime.utcnow():
            raise HTTPException(
                status_code=401,
                detail="Invalid email or password"
            )

        if not verify_password(password, user_row["password_hash"]):
            cursor2 = conn.cursor()
            cursor2.execute(
                """UPDATE users
                   SET
                     failed_attempts = failed_attempts + 1,
                     locked_until = CASE
                       WHEN failed_attempts + 1 >= 5
                       THEN NOW() AT TIME ZONE 'UTC' + INTERVAL '30 minutes'
                       ELSE NULL
                     END
                   WHERE id = %s
                   RETURNING failed_attempts, locked_until""",
                (user_row["id"],)
            )
            updated = cursor2.fetchone()
            conn.commit()
            cursor2.close()

            new_attempts = updated["failed_attempts"]
            if updated["locked_until"]:
                logger.warning(f"Account locked: {email} after {new_attempts} attempts")
            else:
                logger.info(f"Wrong password for: {email} (attempt {new_attempts})")
            raise HTTPException(status_code=401, detail="Invalid email or password")

        cursor2 = conn.cursor()
        cursor2.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login = %s WHERE id = %s",
            (datetime.utcnow(), user_row["id"])
        )
        cursor2.close()

        write_audit_log(conn, user_row["id"], "LOGIN", "users", user_row["id"], f"Login: {email}")
        conn.commit()

        payload = {
            "user_id": user_row["id"],
            "email": email,
            "role": user_row["role"],
            "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        logger.info(f"Successful login: {email}")

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="strict",
            # No max_age/expires -> a SESSION cookie: the browser clears it on close, so a
            # closed browser ends the session. The JWT's own exp (ACCESS_TOKEN_EXPIRE_MINUTES,
            # default 8h) remains the server-side hard cap while the browser stays open.
        )

        return {
            "message": "Login successful",
            "user_id": user_row["id"],
            "email": email,
            "role": user_row["role"]
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)

@app.post("/api/v1/auth/login")
@limiter.limit("5/minute")
def login(request: Request, login_request: LoginRequest, response: Response):
    """Login - 5 attempts per minute per IP."""
    return _login_impl(request, login_request, response)

@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    """Logout - revokes JWT token and clears cookie."""
    conn = None
    try:
        token = get_token_from_request(request, authorization)
        # Decode without exp verification so logout works even on expired tokens
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"],
                             options={"verify_exp": False})
        blacklist_token(token)
        response.delete_cookie(key="access_token", httponly=True, secure=True, samesite="strict")

        conn = get_db_connection()
        write_audit_log(conn, payload.get("user_id"), "LOGOUT", "users", payload.get("user_id"), "User logged out")
        conn.commit()

        return {"message": "Logged out successfully"}

    except jwt.InvalidTokenError:
        # Token is malformed — still clear the cookie so the browser isn't stuck
        response.delete_cookie(key="access_token", httponly=True, secure=True, samesite="strict")
        return {"message": "Logged out successfully"}
    except (KeyError, IndexError):
        raise HTTPException(status_code=401, detail="Invalid token format")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)

@app.get("/api/v1/me")
def get_current_user(request: Request, authorization: Optional[str] = Header(None)):
    """Get current user from JWT cookie or header."""
    conn = None
    try:
        token = get_token_from_request(request, authorization)

        if is_token_blacklisted(token):
            raise HTTPException(status_code=401, detail="Token has been revoked")

        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        email = payload.get("email")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email, full_name, role, entity_id FROM users WHERE email = %s AND is_active = TRUE",
            (email,)
        )
        user_row = cursor.fetchone()
        cursor.close()

        if not user_row:
            raise HTTPException(status_code=401, detail="User not found")

        return {
            "id": user_row["id"],
            "email": user_row["email"],
            "full_name": user_row["full_name"],
            "role": user_row["role"],
            "entity_id": user_row["entity_id"]
        }

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/me: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)

@app.get("/api/v1/entities")
def get_entities(request: Request, authorization: Optional[str] = Header(None)):
    """Get all entities - admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        if _live_role(cursor, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cursor.execute(
            """SELECT e.id, e.entity_name, p.pan_name
               FROM entity e
               JOIN pan_group p ON e.pan_group_id = p.id
               ORDER BY e.id"""
        )
        entities = cursor.fetchall()
        cursor.close()

        return [
            {
                "id": entity["id"],
                "name": entity["entity_name"],
                "pan_group": entity["pan_name"]
            }
            for entity in entities
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/entities: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)

def _require_auth(request: Request, authorization: Optional[str]) -> dict:
    """Validate token and return JWT payload. Raises 401 on failure."""
    token = get_token_from_request(request, authorization)
    if is_token_blacklisted(token):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _live_role(cursor, email: str) -> str:
    """Re-query DB for current role — prevents stale JWT role persisting after revocation."""
    cursor.execute(
        "SELECT role FROM users WHERE email = %s AND is_active = TRUE",
        (email,),
    )
    row = cursor.fetchone()
    return row["role"] if row else "member"


def _compute_realized_gains(conn, entity_id: Optional[int] = None) -> dict:
    """
    Returns realized capital gains per (entity_id, security_id, folio_number)
    using the average cost method.
    Inflows (PURCHASE, PURCHASE_SIP, SWITCH_IN, units>0 & amount>0) add to
    running units + cost. STAMP_DUTY_TAX adds to cost only. REDEMPTION and
    SWITCH_OUT crystallise gain = proceeds - redeemed_units * avg_cost.
    """
    cur = conn.cursor()
    if entity_id is not None:
        cur.execute(
            """
            SELECT entity_id, security_id, folio_number,
                   transaction_date, transaction_type, amount, units
            FROM   mf_transaction
            WHERE  entity_id = %s
            ORDER  BY entity_id, security_id, folio_number, transaction_date, id
            """,
            (entity_id,),
        )
    else:
        cur.execute(
            """
            SELECT entity_id, security_id, folio_number,
                   transaction_date, transaction_type, amount, units
            FROM   mf_transaction
            ORDER  BY entity_id, security_id, folio_number, transaction_date, id
            """
        )
    rows = cur.fetchall()
    cur.close()

    gains: dict = {}
    # running state per key
    state: dict = {}

    INFLOW_TYPES  = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN"}
    OUTFLOW_TYPES = {"REDEMPTION", "SWITCH_OUT"}

    for r in rows:
        key    = (r["entity_id"], r["security_id"], r["folio_number"])
        txtype = r["transaction_type"] or ""
        amt    = float(r["amount"] or 0)
        units  = float(r["units"] or 0)

        if key not in state:
            state[key] = {"units": 0.0, "cost": 0.0}
            gains[key] = 0.0

        s = state[key]

        if txtype == "STAMP_DUTY_TAX":
            s["cost"] += abs(amt)

        elif txtype in INFLOW_TYPES or (units > 0 and amt > 0):
            s["units"] += abs(units)
            s["cost"]  += abs(amt)

        elif txtype in OUTFLOW_TYPES:
            redeemed = abs(units)
            proceeds = abs(amt)
            if s["units"] > 0:
                avg_cost = s["cost"] / s["units"]
                gains[key] += proceeds - redeemed * avg_cost
                s["cost"]  -= redeemed * avg_cost
                s["units"] -= redeemed
                # clamp to avoid floating-point drift below zero
                if s["units"] < 0:
                    s["units"] = 0.0
                if s["cost"] < 0:
                    s["cost"] = 0.0

    return gains


# Realized-gains are derived purely from mf_transaction, which only changes on the
# daily CAS run. Cache the result in Redis keyed by a cheap (row-count, max-id)
# version stamp so new transactions auto-invalidate the entry; the TTL is only a
# memory backstop. Falls back to a direct compute whenever Redis is unavailable.
_REALIZED_GAINS_TTL = 24 * 3600  # seconds


def _realized_gains_version(conn, entity_id: Optional[int]) -> str:
    """Cheap version stamp of mf_transaction (changes on any insert/delete)."""
    cur = conn.cursor()
    if entity_id is not None:
        cur.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m "
            "FROM mf_transaction WHERE entity_id = %s",
            (entity_id,),
        )
    else:
        cur.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM mf_transaction")
    row = cur.fetchone()
    cur.close()
    return f"{row['n']}-{row['m']}"


def _compute_realized_gains_cached(conn, entity_id: Optional[int] = None) -> dict:
    """Redis-cached wrapper around _compute_realized_gains (same return shape)."""
    if redis_client is None:
        return _compute_realized_gains(conn, entity_id)

    scope = entity_id if entity_id is not None else "all"
    cache_key = None
    try:
        version   = _realized_gains_version(conn, entity_id)
        cache_key = f"realized_gains:{scope}:{version}"
        cached    = redis_client.get(cache_key)
        if cached:
            data = json.loads(cached)
            return {(rec[0], rec[1], rec[2]): rec[3] for rec in data}
    except Exception as e:
        logger.warning(f"realized_gains cache read failed: {e}")
        return _compute_realized_gains(conn, entity_id)

    gains = _compute_realized_gains(conn, entity_id)
    try:
        payload = json.dumps([[e, s, f, g] for (e, s, f), g in gains.items()])
        redis_client.setex(cache_key, _REALIZED_GAINS_TTL, payload)
    except Exception as e:
        logger.warning(f"realized_gains cache write failed: {e}")
    return gains


@app.get("/api/v1/holdings")
def get_holdings(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Return MF holdings for the requesting user's entity.
    Admin users may pass ?entity_id=N to view any entity.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cursor = conn.cursor()
        user_role = _live_role(cursor, payload["email"])

        # Resolve the entity_id to query
        if user_role == "admin":
            # Admin with explicit entity_id param → single entity; no param → all entities.
            # Do NOT fall back to the admin's own users.entity_id — that would hide other
            # entities when the admin account happens to be linked to one.
            eid = entity_id  # None when no param was given
        else:
            cursor.execute(
                "SELECT entity_id FROM users WHERE email = %s AND is_active = TRUE",
                (payload["email"],)
            )
            row = cursor.fetchone()
            if not row or not row["entity_id"]:
                raise HTTPException(status_code=404, detail="No entity linked to this user")
            eid = row["entity_id"]

        # Admin with no entity filter → return all holdings across all entities
        if eid is None and user_role == "admin":
            cursor.execute("""
                SELECT
                    h.id,
                    h.entity_id,
                    h.security_id,
                    h.folio_number,
                    h.quantity,
                    h.cost_basis,
                    h.avg_cost,
                    h.invested_amount,
                    h.first_invested_date,
                    h.last_updated_nav     AS nav,
                    h.current_value,
                    h.last_updated,
                    h.prev_week_value,
                    h.market_value_as_on,
                    h.as_of_date,
                    h.exposure_pct,
                    h.weekly_change,
                    h.pnl_ytd,
                    h.pnl_inception,
                    h.pnl_weekly_change,
                    h.returns_ytd_pct,
                    h.returns_inception_pct,
                    h.cagr_inception_pct,
                    h.xirr_inception_pct,
                    h.remarks,
                    sm.isin,
                    sm.security_name,
                    sm.security_type,
                    sm.asset_class,
                    sm.amfi_code,
                    e.entity_name,
                    pg.pan_name AS pan_group_name
                FROM holding h
                JOIN security_master sm ON sm.id = h.security_id
                JOIN entity e ON e.id = h.entity_id
                JOIN pan_group pg ON pg.id = e.pan_group_id
                ORDER BY sm.asset_class, sm.security_name, h.folio_number
            """)
            rows = cursor.fetchall()
            cursor.close()

            realized_gains = _compute_realized_gains_cached(conn)

            holdings = []
            total_invested = 0.0
            for r in rows:
                invested = float(r["invested_amount"]) if r["invested_amount"] else 0.0
                nav_val  = float(r["nav"]) if r["nav"] else None
                qty      = float(r["quantity"]) if r["quantity"] else 0.0
                cur_val  = float(r["current_value"]) if r["current_value"] else (
                    round(qty * nav_val, 2) if nav_val else None
                )
                total_invested += invested
                rg_key = (r["entity_id"], r["security_id"], r["folio_number"])
                holdings.append({
                    "id":                   r["id"],
                    "isin":                 r["isin"],
                    "security_name":        r["security_name"],
                    "security_type":        r["security_type"],
                    "asset_class":          r["asset_class"],
                    "amfi_code":            r["amfi_code"],
                    "folio_number":         r["folio_number"],
                    "quantity":             qty,
                    "avg_cost":             float(r["avg_cost"]) if r["avg_cost"] else None,
                    "cost_basis":           float(r["cost_basis"]) if r["cost_basis"] else None,
                    "invested_amount":      invested,
                    "nav":                  nav_val,
                    "current_value":        cur_val,
                    "first_invested_date":  str(r["first_invested_date"]) if r["first_invested_date"] else None,
                    "last_updated":         r["last_updated"].isoformat() if r["last_updated"] else None,
                    "entity_name":          r["entity_name"],
                    "pan_group_name":       r["pan_group_name"],
                    "realized_gain":        realized_gains.get(rg_key, 0.0),
                    "prev_week_value":      float(r["prev_week_value"])    if r["prev_week_value"]    else None,
                    "market_value_as_on":   float(r["market_value_as_on"]) if r["market_value_as_on"] else None,
                    "as_of_date":           str(r["as_of_date"])           if r["as_of_date"]         else None,
                    "exposure_pct":         float(r["exposure_pct"])       if r["exposure_pct"]       else None,
                    "weekly_change":        float(r["weekly_change"])      if r["weekly_change"]      else None,
                    "pnl_ytd":              float(r["pnl_ytd"])            if r["pnl_ytd"]            else None,
                    "pnl_inception":        float(r["pnl_inception"])      if r["pnl_inception"]      else None,
                    "pnl_weekly_change":    float(r["pnl_weekly_change"])  if r["pnl_weekly_change"]  else None,
                    "returns_ytd_pct":      float(r["returns_ytd_pct"])    if r["returns_ytd_pct"]    else None,
                    "returns_inception_pct":float(r["returns_inception_pct"]) if r["returns_inception_pct"] else None,
                    "cagr_inception_pct":   float(r["cagr_inception_pct"]) if r["cagr_inception_pct"] else None,
                    "xirr_inception_pct":   float(r["xirr_inception_pct"]) if r["xirr_inception_pct"] else None,
                    "remarks":              r["remarks"],
                })

            return {
                "entity_id":       0,
                "entity_name":     "All Entities",
                "total_holdings":  len(holdings),
                "total_invested":  round(total_invested, 2),
                "holdings":        holdings,
            }

        cursor.execute(
            "SELECT entity_name FROM entity WHERE id = %s",
            (eid,)
        )
        entity_row = cursor.fetchone()
        if not entity_row:
            raise HTTPException(status_code=404, detail="Entity not found")

        cursor.execute("""
            SELECT
                h.id,
                h.security_id,
                h.folio_number,
                h.quantity,
                h.cost_basis,
                h.avg_cost,
                h.invested_amount,
                h.first_invested_date,
                h.last_updated_nav     AS nav,
                h.current_value,
                h.last_updated,
                h.prev_week_value,
                h.market_value_as_on,
                h.as_of_date,
                h.exposure_pct,
                h.weekly_change,
                h.pnl_ytd,
                h.pnl_inception,
                h.pnl_weekly_change,
                h.returns_ytd_pct,
                h.returns_inception_pct,
                h.cagr_inception_pct,
                h.xirr_inception_pct,
                h.remarks,
                sm.isin,
                sm.security_name,
                sm.security_type,
                sm.asset_class,
                sm.amfi_code,
                pg.pan_name AS pan_group_name
            FROM holding h
            JOIN security_master sm ON sm.id = h.security_id
            JOIN entity e ON e.id = h.entity_id
            JOIN pan_group pg ON pg.id = e.pan_group_id
            WHERE h.entity_id = %s
            ORDER BY sm.asset_class, sm.security_name, h.folio_number
        """, (eid,))
        rows = cursor.fetchall()
        cursor.close()

        realized_gains = _compute_realized_gains_cached(conn, entity_id=eid)

        holdings = []
        total_invested = 0.0
        for r in rows:
            invested = float(r["invested_amount"]) if r["invested_amount"] else 0.0
            nav_val  = float(r["nav"]) if r["nav"] else None
            qty      = float(r["quantity"]) if r["quantity"] else 0.0
            cur_val  = float(r["current_value"]) if r["current_value"] else (
                round(qty * nav_val, 2) if nav_val else None
            )
            total_invested += invested
            rg_key = (eid, r["security_id"], r["folio_number"])
            holdings.append({
                "id":                   r["id"],
                "isin":                 r["isin"],
                "security_name":        r["security_name"],
                "security_type":        r["security_type"],
                "asset_class":          r["asset_class"],
                "amfi_code":            r["amfi_code"],
                "folio_number":         r["folio_number"],
                "quantity":             qty,
                "avg_cost":             float(r["avg_cost"]) if r["avg_cost"] else None,
                "cost_basis":           float(r["cost_basis"]) if r["cost_basis"] else None,
                "invested_amount":      invested,
                "nav":                  nav_val,
                "current_value":        cur_val,
                "first_invested_date":  str(r["first_invested_date"]) if r["first_invested_date"] else None,
                "last_updated":         r["last_updated"].isoformat() if r["last_updated"] else None,
                "pan_group_name":       r["pan_group_name"],
                "realized_gain":        realized_gains.get(rg_key, 0.0),
                "prev_week_value":      float(r["prev_week_value"])    if r["prev_week_value"]    else None,
                "market_value_as_on":   float(r["market_value_as_on"]) if r["market_value_as_on"] else None,
                "as_of_date":           str(r["as_of_date"])           if r["as_of_date"]         else None,
                "exposure_pct":         float(r["exposure_pct"])       if r["exposure_pct"]       else None,
                "weekly_change":        float(r["weekly_change"])      if r["weekly_change"]      else None,
                "pnl_ytd":              float(r["pnl_ytd"])            if r["pnl_ytd"]            else None,
                "pnl_inception":        float(r["pnl_inception"])      if r["pnl_inception"]      else None,
                "pnl_weekly_change":    float(r["pnl_weekly_change"])  if r["pnl_weekly_change"]  else None,
                "returns_ytd_pct":      float(r["returns_ytd_pct"])    if r["returns_ytd_pct"]    else None,
                "returns_inception_pct":float(r["returns_inception_pct"]) if r["returns_inception_pct"] else None,
                "cagr_inception_pct":   float(r["cagr_inception_pct"]) if r["cagr_inception_pct"] else None,
                "xirr_inception_pct":   float(r["xirr_inception_pct"]) if r["xirr_inception_pct"] else None,
                "remarks":              r["remarks"],
            })

        return {
            "entity_id":       eid,
            "entity_name":     entity_row["entity_name"],
            "total_holdings":  len(holdings),
            "total_invested":  round(total_invested, 2),
            "holdings":        holdings,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/holdings/combined")
def get_combined_holdings(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """
    MF holdings merged by security across all entities.
    Units summed, cost weighted-averaged, XIRR from pooled transactions.
    Admin only.
    """
    from collections import OrderedDict, defaultdict
    from datetime import date as _date
    conn = None
    try:
        payload   = _require_auth(request, authorization)
        conn      = get_db_connection()
        cursor    = conn.cursor()
        user_role = _live_role(cursor, payload["email"])
        if user_role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        cursor.execute("""
            SELECT
                h.id, h.entity_id, h.security_id, h.folio_number,
                h.quantity, h.avg_cost, h.invested_amount,
                h.first_invested_date,
                h.last_updated_nav     AS nav,
                h.current_value, h.prev_week_value, h.market_value_as_on,
                h.as_of_date, h.exposure_pct, h.weekly_change,
                h.pnl_ytd, h.pnl_inception, h.pnl_weekly_change,
                h.returns_ytd_pct, h.returns_inception_pct,
                h.cagr_inception_pct, h.xirr_inception_pct, h.remarks,
                sm.isin, sm.security_name, sm.security_type,
                sm.asset_class, sm.amfi_code,
                e.entity_name
            FROM holding h
            JOIN security_master sm ON sm.id = h.security_id
            JOIN entity e ON e.id = h.entity_id
            ORDER BY sm.asset_class, sm.security_name, e.entity_name
        """)
        rows = cursor.fetchall()

        cursor.execute("""
            SELECT security_id, transaction_date, amount, units
            FROM mf_transaction
            WHERE amount IS NOT NULL
            ORDER BY security_id, transaction_date
        """)
        txn_rows = cursor.fetchall()
        cursor.close()

        realized_gains = _compute_realized_gains_cached(conn)

        # Pool transactions per security for XIRR
        txn_by_sec: dict = defaultdict(list)
        for t in txn_rows:
            amt   = float(t["amount"])
            units = float(t["units"]) if t["units"] is not None else 0.0
            cf    = -abs(amt) if units >= 0 else abs(amt)
            txn_by_sec[t["security_id"]].append((t["transaction_date"], cf))

        # First pass — accumulate per security
        sec_map: dict = OrderedDict()
        for r in rows:
            sid = r["security_id"]
            if sid not in sec_map:
                sec_map[sid] = {
                    "security_id":    sid,
                    "isin":           r["isin"],
                    "security_name":  r["security_name"],
                    "security_type":  r["security_type"],
                    "asset_class":    r["asset_class"],
                    "amfi_code":      r["amfi_code"],
                    "nav":            float(r["nav"]) if r["nav"] else None,
                    "as_of_date":     str(r["as_of_date"]) if r["as_of_date"] else None,
                    "_total_qty":     0.0,
                    "_total_inv":     0.0,
                    "_total_cur":     0.0,
                    "_total_mkt":     0.0,
                    "_wavg_num":      0.0,
                    "_prev_week":     0.0,
                    "_weekly_chg":    0.0,
                    "_pnl_ytd":       0.0,
                    "_pnl_inc":       0.0,
                    "_pnl_wkly":      0.0,
                    "_exp_pct":       0.0,
                    "_realized":      0.0,
                    "_first_dates":   [],
                    "_entities":      set(),
                    "rows":           [],
                }

            qty      = float(r["quantity"])      if r["quantity"]      else 0.0
            invested = float(r["invested_amount"]) if r["invested_amount"] else 0.0
            nav_val  = float(r["nav"])             if r["nav"]            else None
            cur_val  = float(r["current_value"])   if r["current_value"]  else (
                round(qty * nav_val, 2) if nav_val else 0.0
            )
            mkt_val  = float(r["market_value_as_on"]) if r["market_value_as_on"] else cur_val
            avg_cost = float(r["avg_cost"]) if r["avg_cost"] else None
            rg_key   = (r["entity_id"], sid, r["folio_number"])
            realized = realized_gains.get(rg_key, 0.0)

            s = sec_map[sid]
            s["_total_qty"]  += qty
            s["_total_inv"]  += invested
            s["_total_cur"]  += cur_val
            s["_total_mkt"]  += mkt_val
            if avg_cost is not None:
                s["_wavg_num"] += qty * avg_cost
            s["_prev_week"]  += float(r["prev_week_value"])   if r["prev_week_value"]   else 0.0
            s["_weekly_chg"] += float(r["weekly_change"])     if r["weekly_change"]     else 0.0
            s["_pnl_ytd"]    += float(r["pnl_ytd"])           if r["pnl_ytd"]           else 0.0
            s["_pnl_inc"]    += float(r["pnl_inception"])     if r["pnl_inception"]     else 0.0
            s["_pnl_wkly"]   += float(r["pnl_weekly_change"]) if r["pnl_weekly_change"] else 0.0
            s["_exp_pct"]    += float(r["exposure_pct"])      if r["exposure_pct"]      else 0.0
            s["_realized"]   += realized
            if r["first_invested_date"]:
                s["_first_dates"].append(str(r["first_invested_date"]))
            s["_entities"].add(r["entity_name"])
            s["rows"].append({
                "entity_name":        r["entity_name"],
                "folio_number":       r["folio_number"],
                "quantity":           qty,
                "avg_cost":           avg_cost,
                "invested_amount":    invested,
                "nav":                nav_val,
                "current_value":      cur_val,
                "market_value_as_on": mkt_val,
                "pnl_inception":      float(r["pnl_inception"])      if r["pnl_inception"]      else None,
                "xirr_inception_pct": float(r["xirr_inception_pct"]) if r["xirr_inception_pct"] else None,
                "cagr_inception_pct": float(r["cagr_inception_pct"]) if r["cagr_inception_pct"] else None,
                "first_invested_date": str(r["first_invested_date"]) if r["first_invested_date"] else None,
                "realized_gain":      realized,
            })

        # Second pass — compute derived metrics and build response
        from workers.mf_metrics_worker import xirr as _xirr
        today      = _date.today()
        combined   = []
        total_inv_all = 0.0

        for sid, s in sec_map.items():
            tqty = s["_total_qty"]
            tinv = s["_total_inv"]
            tmkt = s["_total_mkt"]

            wavg_cost      = s["_wavg_num"] / tqty if tqty > 0 else None
            first_date_str = min(s["_first_dates"]) if s["_first_dates"] else None

            returns_inc = round((tmkt - tinv) / tinv * 100, 4) if tinv > 0 else None

            cagr = None
            if first_date_str and tinv > 0 and tmkt > 0:
                fd    = _date.fromisoformat(first_date_str)
                years = (today - fd).days / 365.25
                if years > 0.01:
                    cagr = round(((tmkt / tinv) ** (1 / years) - 1) * 100, 4)

            xirr_val = None
            txn_flows = list(txn_by_sec.get(sid, []))
            if txn_flows and tmkt > 0:
                xirr_val = _xirr(txn_flows + [(today, tmkt)])

            total_inv_all += tinv
            combined.append({
                "security_id":          sid,
                "isin":                 s["isin"],
                "security_name":        s["security_name"],
                "security_type":        s["security_type"],
                "asset_class":          s["asset_class"],
                "amfi_code":            s["amfi_code"],
                "quantity":             round(tqty, 6),
                "avg_cost":             round(wavg_cost, 4) if wavg_cost else None,
                "invested_amount":      round(tinv, 2),
                "nav":                  s["nav"],
                "current_value":        round(s["_total_cur"], 2),
                "market_value_as_on":   round(tmkt, 2),
                "first_invested_date":  first_date_str,
                "as_of_date":           s["as_of_date"],
                "exposure_pct":         round(s["_exp_pct"], 4) if s["_exp_pct"] else None,
                "weekly_change":        round(s["_weekly_chg"], 2) if s["_weekly_chg"] else None,
                "prev_week_value":      round(s["_prev_week"], 2)  if s["_prev_week"]  else None,
                "pnl_ytd":              round(s["_pnl_ytd"], 2)    if s["_pnl_ytd"]    else None,
                "pnl_inception":        round(s["_pnl_inc"], 2)    if s["_pnl_inc"]    else None,
                "pnl_weekly_change":    round(s["_pnl_wkly"], 2)   if s["_pnl_wkly"]  else None,
                "returns_ytd_pct":      None,
                "returns_inception_pct": returns_inc,
                "cagr_inception_pct":   cagr,
                "xirr_inception_pct":   xirr_val,
                "realized_gain":        round(s["_realized"], 2),
                "entities":             sorted(s["_entities"]),
                "rows":                 s["rows"],
            })

        return {
            "total_combined": len(combined),
            "total_invested":  round(total_inv_all, 2),
            "holdings":        combined,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/holdings/combined: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


def _resolve_entity(cursor, payload: dict, entity_id_param: Optional[int]) -> Optional[int]:
    """
    Returns the entity_id to query.
    - Admin + entity_id_param  → use the param
    - Admin + no param         → None (all entities)
    - Member                   → their own entity_id from users table
    Uses live DB role to prevent stale JWT role from persisting after revocation.
    """
    role = _live_role(cursor, payload["email"])
    if role == "admin":
        return entity_id_param  # None → all entities, int → filtered
    cursor.execute(
        "SELECT entity_id FROM users WHERE email = %s AND is_active = TRUE",
        (payload["email"],),
    )
    row = cursor.fetchone()
    if row and row["entity_id"]:
        return row["entity_id"]
    raise HTTPException(status_code=404, detail="No entity linked to this user")


# ---------------------------------------------------------------------------
# Equity holdings
# ---------------------------------------------------------------------------

def _fmt(v) -> Optional[float]:
    return float(v) if v is not None else None


_EQUITY_HOLDING_COLS = """
    eh.id,
    eh.entity_id,
    e.entity_name,
    eh.broker,
    eh.symbol,
    eh.symbol_override,
    eh.isin,
    eh.exchange,
    eh.quantity,
    eh.avg_cost,
    eh.cost,
    eh.current_price,
    eh.current_market_value,
    eh.currency,
    eh.fx_rate,
    eh.avg_cost_native,
    eh.cost_native,
    eh.current_price_native,
    eh.current_market_value_native,
    eh.market_value_as_on,
    eh.as_of_date,
    eh.prev_week_value,
    eh.exposure_pct,
    eh.weekly_change,
    eh.pnl_ytd,
    eh.pnl_inception,
    eh.pnl_weekly_change,
    eh.returns_ytd_pct,
    eh.returns_inception_pct,
    eh.cagr_inception_pct,
    eh.xirr_inception_pct,
    eh.first_invested_date,
    eh.sector,
    eh.asset_class,
    eh.remarks,
    eh.updated_at
"""


def _row_to_holding(r: dict) -> dict:
    return {
        "id":                    r["id"],
        "entity_id":             r["entity_id"],
        "entity_name":           r.get("entity_name"),
        "broker":                r["broker"],
        "symbol":                r["symbol_override"] or r["symbol"],
        "isin":                  r["isin"],
        "exchange":              r["exchange"],
        "quantity":              _fmt(r["quantity"]),
        "avg_cost":              _fmt(r["avg_cost"]),
        "cost":                  _fmt(r["cost"]),
        "current_price":         _fmt(r["current_price"]),
        "current_market_value":  _fmt(r["current_market_value"]),
        "currency":              r.get("currency") or "INR",
        "fx_rate":               _fmt(r.get("fx_rate")),
        "avg_cost_native":             _fmt(r.get("avg_cost_native")),
        "cost_native":                 _fmt(r.get("cost_native")),
        "current_price_native":        _fmt(r.get("current_price_native")),
        "current_market_value_native": _fmt(r.get("current_market_value_native")),
        "market_value_as_on":    _fmt(r["market_value_as_on"]),
        "as_of_date":            str(r["as_of_date"]) if r["as_of_date"] else None,
        "prev_week_value":       _fmt(r["prev_week_value"]),
        "exposure_pct":          _fmt(r["exposure_pct"]),
        "weekly_change":         _fmt(r["weekly_change"]),
        "pnl_ytd":               _fmt(r["pnl_ytd"]),
        "pnl_inception":         _fmt(r["pnl_inception"]),
        "pnl_weekly_change":     _fmt(r["pnl_weekly_change"]),
        "returns_ytd_pct":       _fmt(r["returns_ytd_pct"]),
        "returns_inception_pct": _fmt(r["returns_inception_pct"]),
        "cagr_inception_pct":    _fmt(r["cagr_inception_pct"]),
        "xirr_inception_pct":    _fmt(r.get("xirr_inception_pct")),
        "first_invested_date":   str(r["first_invested_date"]) if r["first_invested_date"] else None,
        "sector":                r["sector"],
        "asset_class":           r.get("asset_class") or "equity",
        "remarks":               r["remarks"],
        "updated_at":            r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _equity_totals(rows: list[dict]) -> dict:
    def s(key):
        return round(sum(r[key] or 0 for r in rows), 2)
    return {
        "total_cost":             s("cost"),
        "total_current_market_value": s("current_market_value"),
        "total_prev_week_value":  s("prev_week_value"),
        "total_weekly_change":    s("weekly_change"),
        "total_pnl_inception":    s("pnl_inception"),
        "total_pnl_ytd":          s("pnl_ytd"),
        "total_pnl_weekly_change":s("pnl_weekly_change"),
    }


@app.get("/api/v1/equity/holdings")
def get_equity_holdings(
    request: Request,
    entity_id: Optional[int] = None,
    broker: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Equity holdings with all portfolio metrics.
    Admin: optional ?entity_id=N to filter by entity, ?broker=zerodha|angel_one|dhan
    Member: always returns their own entity only.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        # Build WHERE clause
        conditions = []
        params     = []
        # Gold/silver/commodity holdings moved to the dedicated Gold/Silver page
        # (the 2026-06-26 split) — keep the Equity page to actual equity.
        conditions.append(
            "COALESCE(eh.asset_class, 'equity') NOT IN ('gold','silver','commodity')")
        if eid is not None:
            conditions.append("eh.entity_id = %s")
            params.append(eid)
        if broker:
            conditions.append("eh.broker = %s")
            params.append(broker)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(
            f"""
            SELECT {_EQUITY_HOLDING_COLS}
            FROM   equity_holding eh
            JOIN   entity e ON e.id = eh.entity_id
            {where}
            ORDER BY e.entity_name, eh.broker, eh.symbol
            """,
            params,
        )
        rows = cur.fetchall()

        # Per-broker cash balances for the same scope (entity + optional broker).
        cash_conditions, cash_params = [], []
        if eid is not None:
            cash_conditions.append("bc.entity_id = %s")
            cash_params.append(eid)
        if broker:
            cash_conditions.append("bc.broker = %s")
            cash_params.append(broker)
        cash_where = ("WHERE " + " AND ".join(cash_conditions)) if cash_conditions else ""
        cur.execute(
            f"""
            SELECT bc.entity_id, e.entity_name, bc.broker, bc.balance,
                   bc.currency, bc.balance_native, bc.updated_at
            FROM   broker_cash bc
            JOIN   entity e ON e.id = bc.entity_id
            {cash_where}
            ORDER BY e.entity_name, bc.broker
            """,
            cash_params,
        )
        cash_rows = cur.fetchall()

        # Portfolio money-weighted return (XIRR) from real external cash flows (ledger-
        # derived). Entity-level only: not meaningful per-broker or aggregated across
        # entities, so it's surfaced only when a single entity is in scope.
        pr_row = None
        if not broker and eid is not None:
            cur.execute(
                """SELECT xirr_pct, income_inr, coverage FROM portfolio_returns
                   WHERE entity_id = %s ORDER BY as_of_date DESC LIMIT 1""",
                (eid,),
            )
            pr_row = cur.fetchone()
        cur.close()

        holdings = [_row_to_holding(r) for r in rows]
        totals   = _equity_totals(rows)

        cash_total = round(sum(float(c["balance"] or 0) for c in cash_rows), 2)
        totals["cash_balance"] = cash_total
        totals["value_plus_cash"] = round(
            float(totals.get("total_current_market_value") or 0) + cash_total, 2
        )

        totals["portfolio_xirr_pct"] = (
            float(pr_row["xirr_pct"]) if pr_row and pr_row["xirr_pct"] is not None else None
        )
        totals["portfolio_income"] = (
            float(pr_row["income_inr"]) if pr_row and pr_row["income_inr"] is not None else None
        )
        totals["portfolio_coverage"] = pr_row["coverage"] if pr_row else None

        entity_name = "All Entities" if eid is None else (
            rows[0]["entity_name"] if rows else ""
        )

        return {
            "entity_id":      eid or 0,
            "entity_name":    entity_name,
            "broker":         broker,
            "total_holdings": len(holdings),
            "totals":         totals,
            "holdings":       holdings,
            "cash_balance":   cash_total,
            "cash_by_broker": [
                {
                    "entity_id":   c["entity_id"],
                    "entity_name": c["entity_name"],
                    "broker":      c["broker"],
                    "balance":     float(c["balance"] or 0),
                    "currency":    c.get("currency") or "INR",
                    "balance_native": float(c["balance_native"]) if c.get("balance_native") is not None else None,
                    "updated_at":  c["updated_at"].isoformat() if c["updated_at"] else None,
                }
                for c in cash_rows
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/equity/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Foreign equity holdings — multi-currency (IBKR/Vested USD, DBS SGD), shown on
# the Foreign Equity page in native currency with a currency switcher.
# ---------------------------------------------------------------------------

# Keep in sync with equity_sync_worker.FOREIGN_BROKER_LABELS and the migration.
FOREIGN_BROKERS = ("ibkr", "vested", "dbs")


def _latest_fx_rates(conn) -> dict:
    """Latest INR-per-unit rate for every tracked currency (INR itself = 1).

    Drives the Foreign Equity currency switcher: to show a value in currency T,
    the frontend computes value_native * rate[native] / rate[T].
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT ON (from_currency) from_currency, rate, rate_date
        FROM   fx_rate
        WHERE  to_currency = 'INR'
        ORDER  BY from_currency, rate_date DESC
        """
    )
    rates = {"INR": 1.0}
    for r in cur.fetchall():
        rates[r["from_currency"]] = float(r["rate"])
    cur.close()
    return rates


@app.get("/api/v1/foreign-equity/holdings")
def get_foreign_equity_holdings(
    request: Request,
    entity_id: Optional[int] = None,
    broker: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Foreign (multi-currency) equity holdings from foreign_equity_holding.
    Each row carries both native (USD/SGD/…) and INR-converted figures plus
    currency/fx_rate; the response also includes the latest fx_rates map so the
    frontend can convert every row to a single chosen display currency.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        conditions, params = [], []
        # Gold/silver/commodity (e.g. IBKR uranium) moved to the dedicated
        # Gold/Silver page (2026-06-26 split) — exclude from Foreign Equity too.
        conditions.append(
            "COALESCE(eh.asset_class, 'equity') NOT IN ('gold','silver','commodity')")
        if eid is not None:
            conditions.append("eh.entity_id = %s")
            params.append(eid)
        if broker:
            conditions.append("eh.broker = %s")
            params.append(broker)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(
            f"""
            SELECT {_EQUITY_HOLDING_COLS}
            FROM   foreign_equity_holding eh
            JOIN   entity e ON e.id = eh.entity_id
            {where}
            ORDER BY e.entity_name, eh.broker, eh.symbol
            """,
            params,
        )
        rows = cur.fetchall()

        # Foreign broker cash for the same scope.
        cash_conditions = ["bc.broker = ANY(%s)"]
        cash_params: list = [list(FOREIGN_BROKERS)]
        if eid is not None:
            cash_conditions.append("bc.entity_id = %s")
            cash_params.append(eid)
        if broker:
            cash_conditions.append("bc.broker = %s")
            cash_params.append(broker)
        cur.execute(
            f"""
            SELECT bc.entity_id, e.entity_name, bc.broker, bc.balance,
                   bc.currency, bc.balance_native, bc.updated_at
            FROM   broker_cash bc
            JOIN   entity e ON e.id = bc.entity_id
            WHERE  {" AND ".join(cash_conditions)}
            ORDER BY e.entity_name, bc.broker
            """,
            cash_params,
        )
        cash_rows = cur.fetchall()
        fx_rates  = _latest_fx_rates(conn)
        cur.close()

        holdings = [_row_to_holding(r) for r in rows]
        totals   = _equity_totals(rows)

        cash_total = round(sum(float(c["balance"] or 0) for c in cash_rows), 2)
        totals["cash_balance"] = cash_total
        totals["value_plus_cash"] = round(
            float(totals.get("total_current_market_value") or 0) + cash_total, 2
        )

        # Most recent snapshot / refresh time across the returned holdings.
        as_of_dates = [r["as_of_date"] for r in rows if r["as_of_date"]]
        updated_ats = [r["updated_at"] for r in rows if r["updated_at"]]
        as_of_date  = str(max(as_of_dates)) if as_of_dates else None
        last_updated = max(updated_ats).isoformat() if updated_ats else None

        entity_name = "All Entities" if eid is None else (
            rows[0]["entity_name"] if rows else ""
        )

        return {
            "entity_id":      eid or 0,
            "entity_name":    entity_name,
            "broker":         broker,
            "total_holdings": len(holdings),
            "totals":         totals,
            "holdings":       holdings,
            "fx_rates":       fx_rates,
            "as_of_date":     as_of_date,
            "last_updated":   last_updated,
            "cash_balance":   cash_total,
            "cash_by_broker": [
                {
                    "entity_id":   c["entity_id"],
                    "entity_name": c["entity_name"],
                    "broker":      c["broker"],
                    "balance":     float(c["balance"] or 0),
                    "currency":    c.get("currency") or "INR",
                    "balance_native": float(c["balance_native"]) if c.get("balance_native") is not None else None,
                    "updated_at":  c["updated_at"].isoformat() if c["updated_at"] else None,
                }
                for c in cash_rows
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/foreign-equity/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Gold / Silver / Commodities — instruments split out of the Equity & Foreign
# pages (asset_class in gold/silver/commodity), unioned across both holding
# tables. New broker buys of a known instrument land here automatically via the
# daily sync's asset-class stamping. See equity/asset_class.py.
# ---------------------------------------------------------------------------

@app.get("/api/v1/gold-silver/holdings")
def get_gold_silver_holdings(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Gold ETFs / sovereign gold bonds, silver ETFs, and tracked commodities
    (e.g. IBKR uranium), grouped into precious metals vs commodities. Rows carry
    both native (USD/SGD/…) and INR figures plus the latest fx_rates map so the
    frontend can show native values where they exist (URNU in USD, SGBs in INR).
    Admin sees all entities (or ?entity_id=N); a member sees only their entity.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        conds  = ["COALESCE(eh.asset_class, 'equity') IN ('gold','silver','commodity')"]
        params: list = []
        if eid is not None:
            conds.append("eh.entity_id = %s")
            params.append(eid)
        where = "WHERE " + " AND ".join(conds)

        # Union the domestic + foreign holding tables (identical relevant columns).
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT {_EQUITY_HOLDING_COLS}
                FROM   equity_holding eh
                JOIN   entity e ON e.id = eh.entity_id
                {where}
                UNION ALL
                SELECT {_EQUITY_HOLDING_COLS}
                FROM   foreign_equity_holding eh
                JOIN   entity e ON e.id = eh.entity_id
                {where}
            ) u
            ORDER BY entity_name, asset_class, symbol
            """,
            params + params,
        )
        rows = cur.fetchall()
        fx_rates = _latest_fx_rates(conn)
        cur.close()

        holdings    = [_row_to_holding(r) for r in rows]
        metals      = [h for h in holdings if h["asset_class"] in ("gold", "silver")]
        commodities = [h for h in holdings if h["asset_class"] == "commodity"]
        totals      = _equity_totals(rows)

        def _mv(items):
            return round(sum(float(h["current_market_value"] or 0) for h in items), 2)

        entity_name = "All Entities" if eid is None else (
            rows[0]["entity_name"] if rows else ""
        )

        return {
            "entity_id":         eid or 0,
            "entity_name":       entity_name,
            "total_holdings":    len(holdings),
            "totals":            totals,
            "holdings":          holdings,
            "metals":            metals,
            "commodities":       commodities,
            "metals_total":      _mv(metals),
            "commodities_total": _mv(commodities),
            "fx_rates":          fx_rates,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/gold-silver/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Nuvama PMS holdings — broken-out holdings with equity / cash / combined totals
# ---------------------------------------------------------------------------

@app.get("/api/v1/pms/holdings")
def get_pms_holdings(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Nuvama PMS holdings parsed from the WealthSpectrum report, with an equity
    total, a cash total, and a combined total.
    Admin: optional ?entity_id=N to filter. Member: own entity only.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        conditions = []
        params     = []
        if eid is not None:
            conditions.append("p.entity_id = %s")
            params.append(eid)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(
            f"""
            SELECT p.entity_id, e.entity_name, p.holding_type, p.security_name,
                   p.isin, p.quantity, p.avg_cost, p.cost, p.current_price,
                   p.market_value, p.weight_pct, p.as_on_date
            FROM   pms_holding p
            JOIN   entity e ON e.id = p.entity_id
            {where}
            ORDER BY e.entity_name, p.holding_type, p.market_value DESC
            """,
            params,
        )
        rows = cur.fetchall()
        cur.close()

        def _f(v):
            return float(v) if v is not None else None

        holdings = [{
            "entity_id":     r["entity_id"],
            "entity_name":   r["entity_name"],
            "holding_type":  r["holding_type"],
            "security_name": r["security_name"],
            "isin":          r["isin"],
            "quantity":      _f(r["quantity"]),
            "avg_cost":      _f(r["avg_cost"]),
            "cost":          _f(r["cost"]),
            "current_price": _f(r["current_price"]),
            "market_value":  _f(r["market_value"]) or 0.0,
            "weight_pct":    _f(r["weight_pct"]),
        } for r in rows]

        equity_total = sum(h["market_value"] for h in holdings if h["holding_type"] == "equity")
        cash_total   = sum(h["market_value"] for h in holdings if h["holding_type"] == "cash")
        equity_cost  = sum((h["cost"] or 0.0) for h in holdings if h["holding_type"] == "equity")
        # Total capital put into PMS = cost of equity holdings + cash parked in
        # the account. Cash is uninvested principal, so it counts toward invested.
        invested_cost = equity_cost + cash_total

        # Per-entity breakdown of invested cost, so the admin "All Entities" view
        # can show each user individually alongside the combined grand total.
        by_entity: dict[int, dict] = {}
        for h in holdings:
            e = by_entity.setdefault(h["entity_id"], {
                "entity_id":    h["entity_id"],
                "entity_name":  h["entity_name"],
                "equity_cost":  0.0,
                "cash_total":   0.0,
                "equity_total": 0.0,
            })
            if h["holding_type"] == "equity":
                e["equity_cost"]  += h["cost"] or 0.0
                e["equity_total"] += h["market_value"] or 0.0
            elif h["holding_type"] == "cash":
                e["cash_total"]   += h["market_value"] or 0.0
        by_entity_list = sorted(
            (
                {
                    "entity_id":     e["entity_id"],
                    "entity_name":   e["entity_name"],
                    "equity_cost":   round(e["equity_cost"], 2),
                    "cash_total":    round(e["cash_total"], 2),
                    "equity_total":  round(e["equity_total"], 2),
                    "invested_cost": round(e["equity_cost"] + e["cash_total"], 2),
                    "total":         round(e["equity_total"] + e["cash_total"], 2),
                }
                for e in by_entity.values()
            ),
            key=lambda x: x["entity_name"],
        )

        entity_name = "All Entities" if eid is None else (rows[0]["entity_name"] if rows else "")
        as_on = rows[0]["as_on_date"].isoformat() if rows and rows[0]["as_on_date"] else None

        return {
            "entity_id":   eid or 0,
            "entity_name": entity_name,
            "as_on_date":  as_on,
            "totals": {
                "equity_total":  round(equity_total, 2),
                "cash_total":    round(cash_total, 2),
                "total":         round(equity_total + cash_total, 2),
                "equity_cost":   round(equity_cost, 2),
                "invested_cost": round(invested_cost, 2),
                "equity_pnl":    round(equity_total - equity_cost, 2),
                "equity_count":  sum(1 for h in holdings if h["holding_type"] == "equity"),
                "cash_count":    sum(1 for h in holdings if h["holding_type"] == "cash"),
            },
            "by_entity": by_entity_list,
            "holdings":  holdings,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/pms/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Equity summary — aggregate totals per entity, broken down by broker
# ---------------------------------------------------------------------------

@app.get("/api/v1/equity/summary")
def get_equity_summary(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Aggregated equity portfolio totals.
    Returns one row per (entity, broker) with summed cost, value, P&L, returns.
    Admin: all entities unless ?entity_id=N is passed.
    Member: their entity only.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        where  = "WHERE eh.entity_id = %s" if eid is not None else ""
        params = [eid] if eid is not None else []

        cur.execute(
            f"""
            SELECT
                e.id                                          AS entity_id,
                e.entity_name,
                eh.broker,
                COUNT(*)                                      AS holding_count,
                SUM(eh.cost)                                  AS total_cost,
                SUM(eh.current_market_value)                  AS total_current_value,
                SUM(eh.prev_week_value)                       AS total_prev_week_value,
                SUM(eh.weekly_change)                         AS total_weekly_change,
                SUM(eh.pnl_inception)                         AS total_pnl_inception,
                SUM(eh.pnl_ytd)                               AS total_pnl_ytd,
                SUM(eh.pnl_weekly_change)                     AS total_pnl_weekly_change,
                CASE
                    WHEN SUM(eh.cost) > 0
                    THEN ROUND(SUM(eh.pnl_inception) / SUM(eh.cost) * 100, 4)
                END                                           AS returns_inception_pct,
                CASE
                    WHEN SUM(eh.prev_week_value) > 0
                    THEN ROUND(SUM(eh.pnl_ytd) / SUM(eh.prev_week_value) * 100, 4)
                END                                           AS returns_ytd_pct,
                MAX(eh.as_of_date)                            AS as_of_date,
                MAX(eh.updated_at)                            AS last_updated
            FROM equity_holding eh
            JOIN entity e ON e.id = eh.entity_id
            {where}
            GROUP BY e.id, e.entity_name, eh.broker
            ORDER BY e.entity_name, eh.broker
            """,
            params,
        )
        rows = cur.fetchall()

        # Also compute grand total across all brokers for the entity/entities
        cur.execute(
            f"""
            SELECT
                SUM(eh.cost)                 AS total_cost,
                SUM(eh.current_market_value) AS total_current_value,
                SUM(eh.prev_week_value)      AS total_prev_week_value,
                SUM(eh.weekly_change)        AS total_weekly_change,
                SUM(eh.pnl_inception)        AS total_pnl_inception,
                SUM(eh.pnl_ytd)              AS total_pnl_ytd
            FROM equity_holding eh
            {where}
            """,
            params,
        )
        grand = cur.fetchone()

        # Per-(entity, broker) cash balances for the same scope.
        cur.execute(
            f"""
            SELECT bc.entity_id, bc.broker, bc.balance
            FROM   broker_cash bc
            {("WHERE bc.entity_id = %s" if eid is not None else "")}
            """,
            ([eid] if eid is not None else []),
        )
        cash_map   = {(c["entity_id"], c["broker"]): float(c["balance"] or 0) for c in cur.fetchall()}
        cash_total = round(sum(cash_map.values()), 2)
        cur.close()

        grand_value = float(grand["total_current_value"] or 0)

        return {
            "entity_id":   eid or 0,
            "entity_name": "All Entities" if eid is None else (rows[0]["entity_name"] if rows else ""),
            "grand_total": {
                "total_cost":            _fmt(grand["total_cost"]),
                "total_current_value":   _fmt(grand["total_current_value"]),
                "total_prev_week_value": _fmt(grand["total_prev_week_value"]),
                "total_weekly_change":   _fmt(grand["total_weekly_change"]),
                "total_pnl_inception":   _fmt(grand["total_pnl_inception"]),
                "total_pnl_ytd":         _fmt(grand["total_pnl_ytd"]),
                "cash_balance":          cash_total,
                "value_plus_cash":       round(grand_value + cash_total, 2),
            },
            "by_broker": [
                {
                    "entity_id":             r["entity_id"],
                    "entity_name":           r["entity_name"],
                    "broker":                r["broker"],
                    "holding_count":         r["holding_count"],
                    "total_cost":            _fmt(r["total_cost"]),
                    "total_current_value":   _fmt(r["total_current_value"]),
                    "total_prev_week_value": _fmt(r["total_prev_week_value"]),
                    "total_weekly_change":   _fmt(r["total_weekly_change"]),
                    "total_pnl_inception":   _fmt(r["total_pnl_inception"]),
                    "total_pnl_ytd":         _fmt(r["total_pnl_ytd"]),
                    "total_pnl_weekly_change":_fmt(r["total_pnl_weekly_change"]),
                    "returns_inception_pct": _fmt(r["returns_inception_pct"]),
                    "returns_ytd_pct":       _fmt(r["returns_ytd_pct"]),
                    "cash_balance":          cash_map.get((r["entity_id"], r["broker"]), 0.0),
                    "as_of_date":            str(r["as_of_date"]) if r["as_of_date"] else None,
                    "last_updated":          r["last_updated"].isoformat() if r["last_updated"] else None,
                }
                for r in rows
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/equity/summary: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Portfolio overview — aggregate MF + equity across all entities
# ---------------------------------------------------------------------------

# Maps each manual_input category to the broad asset_class used by the
# dashboard donut / per-entity bars. Keys mirror security_master.asset_class
# (EQUITY / FIXED_INCOME / ALTERNATES / DIRECT_EQUITY) so manual entries fold
# into the same buckets as automated holdings. CASH covers below-the-line
# cash-like balances (mirrors the report's E/F/G sections).
MANUAL_ASSET_CLASS = {
    "ppf":            "FIXED_INCOME",
    "liquid_fund":    "FIXED_INCOME",
    "debt_fund":      "FIXED_INCOME",
    "arbitrage_fund": "FIXED_INCOME",
    "pms":            "PMS",
    "aif":            "EQUITY",
    "direct_equity":  "DIRECT_EQUITY",
    "overseas_fund":   "ALTERNATES",
    "overseas_equity": "ALTERNATES",
    "forex":           "ALTERNATES",
    "gold_etf":        "GOLD_SILVER",
    "unlisted":        "ALTERNATES",
    "startup":         "ALTERNATES",
    "art":             "ART",
    "properties":      "REAL_ESTATE",
    "funds_transit":   "CASH",
    "broker_balance":  "CASH",
    "bank":            "CASH",
}


def _fetch_manual_overview_rows(conn, entity_id: Optional[int] = None):
    """
    Latest manual_input per (entity, category, label), shaped to match the
    row dicts the /overview aggregator consumes from holding / equity_holding.
    cost / current_value / prev_week_value are already stored in INR by the
    manual-data form, so no FX conversion is needed here. Manual entries have
    no transaction ledger, so cagr/xirr are left as None and pnl is the simple
    current_value - cost difference.
    """
    cur   = conn.cursor()
    where = "WHERE m.entity_id = %s" if entity_id else ""
    params = [entity_id] if entity_id else []
    cur.execute(f"""
        SELECT DISTINCT ON (m.entity_id, m.category, m.label)
            m.entity_id, e.entity_name, m.category,
            m.cost, m.current_value, m.prev_week_value, m.updated_at
        FROM manual_input m
        JOIN entity e ON e.id = m.entity_id
        {where}
        ORDER BY m.entity_id, m.category, m.label, m.updated_at DESC
    """, params)
    rows = cur.fetchall()
    cur.close()

    out = []
    for r in rows:
        cost     = float(r["cost"])            if r["cost"]            is not None else None
        mkt      = float(r["current_value"])   if r["current_value"]   is not None else 0.0
        prev     = float(r["prev_week_value"]) if r["prev_week_value"] is not None else None
        invested = cost if cost is not None else 0.0
        pnl      = (mkt - cost) if cost is not None else 0.0
        weekly   = (mkt - prev) if prev is not None else 0.0
        out.append({
            "entity_id":          r["entity_id"],
            "entity_name":        r["entity_name"],
            "asset_class":        MANUAL_ASSET_CLASS.get(r["category"], "ALTERNATES"),
            "security_type":      "MANUAL",
            "invested":           invested,
            "mkt_value":          mkt,
            "pnl_inception":      pnl,
            "pnl_ytd":            0.0,
            "weekly_change":      weekly,
            "cagr_inception_pct": None,
            "weight":             mkt,
        })
    return out


def _fetch_pms_overview_rows(conn, entity_id: Optional[int] = None):
    """
    PMS holdings (pms_holding) shaped to match the row dicts the /overview
    aggregator consumes from holding / equity_holding. PMS equity is reported
    in its own PMS asset class (kept OUT of the EQUITY bucket so the dashboard
    shows PMS as a distinct slice); PMS cash folds into CASH. There is no
    transaction ledger, so cagr/xirr and ytd/weekly are left empty; pnl is the
    simple market_value - cost.
    """
    cur    = conn.cursor()
    where  = "WHERE p.entity_id = %s" if entity_id else ""
    params = [entity_id] if entity_id else []
    cur.execute(f"""
        SELECT p.entity_id, e.entity_name, p.holding_type,
               p.cost, p.market_value
        FROM pms_holding p
        JOIN entity e ON e.id = p.entity_id
        {where}
    """, params)
    rows = cur.fetchall()
    cur.close()

    out = []
    for r in rows:
        cost     = float(r["cost"])         if r["cost"]         is not None else None
        mkt      = float(r["market_value"]) if r["market_value"] is not None else 0.0
        invested = cost if cost is not None else 0.0
        pnl      = (mkt - cost) if cost is not None else 0.0
        out.append({
            "entity_id":          r["entity_id"],
            "entity_name":        r["entity_name"],
            "asset_class":        "CASH" if r["holding_type"] == "cash" else "PMS",
            "security_type":      "PMS",
            "invested":           invested,
            "mkt_value":          mkt,
            "pnl_inception":      pnl,
            "pnl_ytd":            0.0,
            "weekly_change":      0.0,
            "cagr_inception_pct": None,
            "weight":             mkt,
        })
    return out


def _fetch_broker_cash_overview_rows(conn, entity_id: Optional[int] = None):
    """
    Broker-account cash (broker_cash) shaped for the /overview aggregator. The
    free cash in the Zerodha / Angel One / Dhan equity accounts folds into the
    CASH bucket so the dashboard total and allocation include it. Cash has no
    cost basis, so pnl/ytd/weekly are 0. Rajani Corp's Zerodha PMS cash lives in
    pms_holding (handled by _fetch_pms_overview_rows), not here — no double count.
    """
    cur    = conn.cursor()
    where  = "WHERE bc.entity_id = %s" if entity_id else ""
    params = [entity_id] if entity_id else []
    cur.execute(f"""
        SELECT bc.entity_id, e.entity_name, bc.broker, bc.balance
        FROM broker_cash bc
        JOIN entity e ON e.id = bc.entity_id
        {where}
    """, params)
    rows = cur.fetchall()
    cur.close()

    out = []
    for r in rows:
        bal = float(r["balance"]) if r["balance"] is not None else 0.0
        out.append({
            "entity_id":          r["entity_id"],
            "entity_name":        r["entity_name"],
            "asset_class":        "CASH",
            "security_type":      "BROKER_CASH",
            "invested":           bal,
            "mkt_value":          bal,
            "pnl_inception":      0.0,
            "pnl_ytd":            0.0,
            "weekly_change":      0.0,
            "cagr_inception_pct": None,
            "weight":             bal,
        })
    return out


def _fetch_bank_cash_overview_rows(conn, entity_id: Optional[int] = None):
    """
    Bank-account cash (bank_account) shaped for the /overview aggregator. Balances
    are held in native currency, so each is converted to INR at the latest fx_rate
    before folding into the CASH bucket — keeping the dashboard total and allocation
    inclusive of bank holdings. Cash has no cost basis, so pnl/ytd/weekly are 0.
    A balance whose currency has no fx_rate yet is skipped (can't value it in INR).
    """
    cur    = conn.cursor()
    fx     = _latest_fx_to_inr(cur)
    where  = "WHERE ba.entity_id = %s" if entity_id else ""
    params = [entity_id] if entity_id else []
    cur.execute(f"""
        SELECT ba.entity_id, e.entity_name, ba.currency, ba.balance
        FROM bank_account ba
        JOIN entity e ON e.id = ba.entity_id
        {where}
    """, params)
    rows = cur.fetchall()
    cur.close()

    out = []
    for r in rows:
        rate = fx.get(r["currency"])
        if rate is None:
            continue
        bal = (float(r["balance"]) if r["balance"] is not None else 0.0) * rate
        out.append({
            "entity_id":          r["entity_id"],
            "entity_name":        r["entity_name"],
            "asset_class":        "BANK_CASH",   # own slice in the dashboard cash breakdown
            "security_type":      "BANK_CASH",
            "invested":           bal,
            "mkt_value":          bal,
            "pnl_inception":      0.0,
            "pnl_ytd":            0.0,
            "weekly_change":      0.0,
            "cagr_inception_pct": None,
            "weight":             bal,
        })
    return out


@app.get("/api/v1/overview")
def get_overview(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """
    Aggregate portfolio overview across ALL entities, all asset classes.
    Returns:
      - summary: totals across MF + equity
      - asset_class_breakdown: combined allocation
      - entities: per-entity breakdown with MF + equity subtotals
    """
    conn = None
    try:
        payload   = _require_auth(request, authorization)
        conn      = get_db_connection()
        cursor    = conn.cursor()
        if _live_role(cursor, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required.")

        cursor.execute("""
            SELECT
                h.entity_id,
                e.entity_name,
                sm.asset_class,
                sm.security_type,
                COALESCE(h.invested_amount, 0)                         AS invested,
                COALESCE(h.market_value_as_on, h.current_value, 0)    AS mkt_value,
                COALESCE(h.pnl_inception, 0)                           AS pnl_inception,
                COALESCE(h.pnl_ytd, 0)                                 AS pnl_ytd,
                COALESCE(h.weekly_change, 0)                           AS weekly_change,
                h.cagr_inception_pct,
                COALESCE(h.market_value_as_on, h.current_value, 0)    AS weight
            FROM holding h
            JOIN entity e ON e.id = h.entity_id
            JOIN security_master sm ON sm.id = h.security_id
        """)
        mf_rows = cursor.fetchall()

        cursor.execute("""
            SELECT
                eh.entity_id,
                e.entity_name,
                CASE
                    WHEN COALESCE(eh.asset_class,'equity') IN ('gold','silver') THEN 'GOLD_SILVER'
                    WHEN COALESCE(eh.asset_class,'equity') = 'commodity'         THEN 'COMMODITIES'
                    ELSE 'DIRECT_EQUITY'
                END                                   AS asset_class,
                'DIRECT_EQUITY'                       AS security_type,
                COALESCE(eh.cost, 0)                  AS invested,
                COALESCE(eh.current_market_value, 0)  AS mkt_value,
                COALESCE(eh.pnl_inception, 0)         AS pnl_inception,
                COALESCE(eh.pnl_ytd, 0)               AS pnl_ytd,
                COALESCE(eh.weekly_change, 0)         AS weekly_change,
                eh.cagr_inception_pct,
                COALESCE(eh.current_market_value, 0)  AS weight
            FROM equity_holding eh
            JOIN entity e ON e.id = eh.entity_id
            UNION ALL
            SELECT
                eh.entity_id,
                e.entity_name,
                CASE
                    WHEN COALESCE(eh.asset_class,'equity') IN ('gold','silver') THEN 'GOLD_SILVER'
                    WHEN COALESCE(eh.asset_class,'equity') = 'commodity'         THEN 'COMMODITIES'
                    ELSE 'DIRECT_EQUITY'
                END                                   AS asset_class,
                'DIRECT_EQUITY'                       AS security_type,
                COALESCE(eh.cost, 0)                  AS invested,
                COALESCE(eh.current_market_value, 0)  AS mkt_value,
                COALESCE(eh.pnl_inception, 0)         AS pnl_inception,
                COALESCE(eh.pnl_ytd, 0)               AS pnl_ytd,
                COALESCE(eh.weekly_change, 0)         AS weekly_change,
                eh.cagr_inception_pct,
                COALESCE(eh.current_market_value, 0)  AS weight
            FROM foreign_equity_holding eh
            JOIN entity e ON e.id = eh.entity_id
        """)
        eq_rows = cursor.fetchall()
        cursor.close()

        # Manual inputs (PPF, PMS/AIF, unlisted equity, startups, overseas,
        # cash balances, …) folded into the same asset-class buckets so the
        # dashboard portfolio matches the generated reports.
        manual_rows = _fetch_manual_overview_rows(conn)

        # Nuvama PMS holdings (equity → EQUITY bucket, cash → CASH) so the
        # dashboard totals and allocation include the PMS portfolio.
        pms_rows = _fetch_pms_overview_rows(conn)

        # Broker-account cash (Zerodha / Angel One / Dhan) → CASH bucket.
        broker_cash_rows = _fetch_broker_cash_overview_rows(conn)

        # Bank-account cash (HSBC / DBS / FAB / …), native ccy → INR → CASH bucket.
        bank_cash_rows = _fetch_bank_cash_overview_rows(conn)

        all_rows = (list(mf_rows) + list(eq_rows) + manual_rows + pms_rows
                    + broker_cash_rows + bank_cash_rows)

        def row_val(r, key):
            v = r[key]
            return float(v) if v is not None else 0.0

        total_invested = sum(row_val(r, "invested")      for r in all_rows)
        total_value    = sum(row_val(r, "mkt_value")     for r in all_rows)
        total_pnl      = sum(row_val(r, "pnl_inception") for r in all_rows)
        total_pnl_ytd  = sum(row_val(r, "pnl_ytd")      for r in all_rows)
        total_weekly   = sum(row_val(r, "weekly_change") for r in all_rows)

        w_sum, w_cagr = 0.0, 0.0
        for r in all_rows:
            if r["cagr_inception_pct"] is not None:
                w = row_val(r, "weight")
                w_cagr += float(r["cagr_inception_pct"]) * w
                w_sum  += w
        weighted_cagr = round(w_cagr / w_sum, 4) if w_sum > 0 else None

        class_totals: dict = {}
        for r in all_rows:
            cls = r["asset_class"]
            class_totals.setdefault(cls, {"invested": 0.0, "value": 0.0, "pnl": 0.0})
            class_totals[cls]["invested"] += row_val(r, "invested")
            class_totals[cls]["value"]    += row_val(r, "mkt_value")
            class_totals[cls]["pnl"]      += row_val(r, "pnl_inception")

        # Drop empty buckets (no value AND nothing invested) so a category that
        # exists but is currently empty doesn't render as a 0% slice on the pie /
        # bar charts.
        asset_class_breakdown = [
            {
                "asset_class": cls,
                "invested":    round(v["invested"], 2),
                "value":       round(v["value"],    2),
                "pnl":         round(v["pnl"],      2),
                "pct":         round(v["value"] / total_value * 100, 2) if total_value else 0,
            }
            for cls, v in sorted(class_totals.items(), key=lambda x: -x[1]["value"])
            if round(v["value"], 2) > 0 or round(v["invested"], 2) > 0
        ]

        entity_map: dict = {}
        for r in all_rows:
            eid   = r["entity_id"]
            ename = r["entity_name"]
            cls   = r["asset_class"]
            if eid not in entity_map:
                entity_map[eid] = {
                    "entity_id":     eid,
                    "entity_name":   ename,
                    "total_invested": 0.0,
                    "total_value":    0.0,
                    "total_pnl":      0.0,
                    "total_pnl_ytd":  0.0,
                    "total_weekly":   0.0,
                    "asset_classes":  {},
                }
            em = entity_map[eid]
            em["total_invested"] += row_val(r, "invested")
            em["total_value"]    += row_val(r, "mkt_value")
            em["total_pnl"]      += row_val(r, "pnl_inception")
            em["total_pnl_ytd"]  += row_val(r, "pnl_ytd")
            em["total_weekly"]   += row_val(r, "weekly_change")

            # Keep each real asset_class distinct (EQUITY / FIXED_INCOME /
            # ALTERNATES / DIRECT_EQUITY / HYBRID / ARBITRAGE / CASH) so manual
            # entries surface in the per-entity bars instead of collapsing into
            # a single "MF" bucket — consistent with the top-level donut.
            broad = cls
            em["asset_classes"].setdefault(broad, {"invested": 0.0, "value": 0.0, "pnl": 0.0})
            em["asset_classes"][broad]["invested"] += row_val(r, "invested")
            em["asset_classes"][broad]["value"]    += row_val(r, "mkt_value")
            em["asset_classes"][broad]["pnl"]      += row_val(r, "pnl_inception")

        entities_out = []
        for em in sorted(entity_map.values(), key=lambda x: -x["total_value"]):
            ev      = em["total_value"]
            classes = [
                {
                    "asset_class": broad,
                    "invested":    round(v["invested"], 2),
                    "value":       round(v["value"],    2),
                    "pnl":         round(v["pnl"],      2),
                    "pct":         round(v["value"] / ev * 100, 2) if ev else 0,
                }
                for broad, v in sorted(em["asset_classes"].items(), key=lambda x: -x[1]["value"])
                if round(v["value"], 2) > 0 or round(v["invested"], 2) > 0
            ]
            entities_out.append({
                "entity_id":      em["entity_id"],
                "entity_name":    em["entity_name"],
                "total_invested": round(em["total_invested"], 2),
                "total_value":    round(em["total_value"],    2),
                "total_pnl":      round(em["total_pnl"],      2),
                "total_pnl_ytd":  round(em["total_pnl_ytd"],  2),
                "total_weekly":   round(em["total_weekly"],   2),
                "asset_classes":  classes,
            })

        return {
            "summary": {
                "total_invested": round(total_invested, 2),
                "total_value":    round(total_value,    2),
                "total_pnl":      round(total_pnl,      2),
                "total_pnl_ytd":  round(total_pnl_ytd,  2),
                "total_weekly":   round(total_weekly,   2),
                "weighted_cagr":  weighted_cagr,
            },
            "asset_class_breakdown": asset_class_breakdown,
            "entities": entities_out,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/overview: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/transactions")
def get_transactions(
    request: Request,
    entity_id: Optional[int] = None,
    txn_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    authorization: Optional[str] = Header(None),
):
    """Return MF transactions for the requesting user's entity."""
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cursor = conn.cursor()
        user_role = _live_role(cursor, payload["email"])

        if entity_id is not None and user_role == "admin":
            eid = entity_id
        else:
            cursor.execute(
                "SELECT entity_id FROM users WHERE email = %s AND is_active = TRUE",
                (payload["email"],)
            )
            row = cursor.fetchone()
            if not row or not row["entity_id"]:
                if user_role == "admin":
                    eid = None  # all-entities view
                else:
                    raise HTTPException(status_code=404, detail="No entity linked to this user")
            else:
                eid = row["entity_id"]

        limit  = max(1, min(limit, 500))
        offset = max(0, offset)

        type_filter     = txn_type.strip() if txn_type else None
        type_clause     = "AND t.transaction_type ILIKE %s" if type_filter else ""
        type_count_clause = "WHERE transaction_type ILIKE %s" if type_filter else ""

        if eid is None:
            # Admin all-entities view
            params = [type_filter, limit, offset] if type_filter else [limit, offset]
            cursor.execute(f"""
                SELECT
                    t.id, t.transaction_date, t.description, t.transaction_type,
                    t.amount, t.units, t.nav, t.balance_units, t.folio_number,
                    sm.security_name, sm.isin, e.entity_name
                FROM mf_transaction t
                JOIN security_master sm ON sm.id = t.security_id
                JOIN entity e ON e.id = t.entity_id
                WHERE 1=1 {type_clause}
                ORDER BY t.transaction_date DESC, t.id DESC
                LIMIT %s OFFSET %s
            """, params)
            rows = cursor.fetchall()
            count_params = [type_filter] if type_filter else []
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM mf_transaction {type_count_clause}",
                count_params
            )
            total = cursor.fetchone()["total"]
            cursor.close()
            return {
                "entity_id": 0,
                "total":     total,
                "limit":     limit,
                "offset":    offset,
                "transactions": [
                    {
                        "id":            r["id"],
                        "date":          str(r["transaction_date"]),
                        "description":   r["description"],
                        "type":          r["transaction_type"],
                        "amount":        float(r["amount"]) if r["amount"] else None,
                        "units":         float(r["units"])  if r["units"]  else None,
                        "nav":           float(r["nav"])    if r["nav"]    else None,
                        "balance_units": float(r["balance_units"]) if r["balance_units"] else None,
                        "folio_number":  r["folio_number"],
                        "security_name": r["security_name"],
                        "isin":          r["isin"],
                        "entity_name":   r["entity_name"],
                    }
                    for r in rows
                ],
            }

        params = [eid, type_filter, limit, offset] if type_filter else [eid, limit, offset]
        cursor.execute(f"""
            SELECT
                t.id,
                t.transaction_date,
                t.description,
                t.transaction_type,
                t.amount,
                t.units,
                t.nav,
                t.balance_units,
                t.folio_number,
                sm.security_name,
                sm.isin
            FROM mf_transaction t
            JOIN security_master sm ON sm.id = t.security_id
            WHERE t.entity_id = %s {type_clause}
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT %s OFFSET %s
        """, params)
        rows = cursor.fetchall()

        count_params = [eid, type_filter] if type_filter else [eid]
        cursor.execute(
            f"SELECT COUNT(*) AS total FROM mf_transaction t WHERE t.entity_id = %s {type_clause}",
            count_params
        )
        total = cursor.fetchone()["total"]
        cursor.close()

        return {
            "entity_id":  eid,
            "total":      total,
            "limit":      limit,
            "offset":     offset,
            "transactions": [
                {
                    "id":               r["id"],
                    "date":             str(r["transaction_date"]),
                    "description":      r["description"],
                    "type":             r["transaction_type"],
                    "amount":           float(r["amount"]) if r["amount"] else None,
                    "units":            float(r["units"])  if r["units"]  else None,
                    "nav":              float(r["nav"])    if r["nav"]    else None,
                    "balance_units":    float(r["balance_units"]) if r["balance_units"] else None,
                    "folio_number":     r["folio_number"],
                    "security_name":    r["security_name"],
                    "isin":             r["isin"],
                }
                for r in rows
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/transactions: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Manual Inputs
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {
    "liquid_fund", "debt_fund", "arbitrage_fund", "ppf",
    "pms", "direct_equity", "aif",
    "overseas_fund", "overseas_equity", "forex", "gold_etf",
    "unlisted", "startup", "properties", "art",
    "funds_transit", "broker_balance", "bank",
}

VALID_CURRENCIES = {"INR", "USD", "GBP", "EUR", "AED", "SGD", "HKD"}


class ManualInputItem(BaseModel):
    entity_id:       int
    category:        str
    label:           str       = Field(min_length=1, max_length=200)
    cost:            Optional[float] = None
    current_value:   Optional[float] = None
    prev_week_value: Optional[float] = None
    currency:        str = "INR"
    raw_amount:      Optional[float] = None
    fx_rate:         Optional[float] = None
    inception_date:  Optional[str]   = None
    notes:           Optional[str]   = None


class ManualInputsRequest(BaseModel):
    password: str = Field(min_length=6, max_length=72)
    inputs:   List[ManualInputItem]


# --- Bank accounts (cash-only, statement-fed) ------------------------------

VALID_BANK_ACCOUNT_TYPES = {"savings", "current", "nre", "other"}


class BankAccountCreate(BaseModel):
    entity_id:    int
    bank_name:    str = Field(min_length=1, max_length=120)
    account_type: str = "savings"
    currency:     str = "INR"
    notes:        Optional[str] = None


class BankBalanceUpdate(BaseModel):
    balance:       float
    balance_as_of: Optional[str] = None   # YYYY-MM-DD
    statement_id:  Optional[int] = None   # mark this uploaded statement as committed
    notes:         Optional[str] = None


@app.get("/api/v1/manual-inputs")
def get_manual_inputs(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()

        role = _live_role(cur, payload["email"])
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        where  = "WHERE m.entity_id = %s" if entity_id else ""
        params = [entity_id] if entity_id else []

        cur.execute(f"""
            SELECT DISTINCT ON (m.entity_id, m.category, m.label)
                m.id, m.entity_id, e.entity_name, m.category, m.label,
                m.cost, m.current_value, m.prev_week_value,
                m.currency, m.raw_amount, m.fx_rate,
                m.inception_date, m.notes, m.updated_at,
                u.full_name AS updated_by_name
            FROM manual_input m
            JOIN entity e ON e.id = m.entity_id
            LEFT JOIN users u ON u.id = m.updated_by
            {where}
            ORDER BY m.entity_id, m.category, m.label, m.updated_at DESC
        """, params)
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "id":              r["id"],
                "entity_id":       r["entity_id"],
                "entity_name":     r["entity_name"],
                "category":        r["category"],
                "label":           r["label"],
                "cost":            float(r["cost"])            if r["cost"]            else None,
                "current_value":   float(r["current_value"])   if r["current_value"]   else None,
                "prev_week_value": float(r["prev_week_value"]) if r["prev_week_value"] else None,
                "currency":        r["currency"],
                "raw_amount":      float(r["raw_amount"])      if r["raw_amount"]      else None,
                "fx_rate":         float(r["fx_rate"])         if r["fx_rate"]         else None,
                "inception_date":  str(r["inception_date"])    if r["inception_date"]  else None,
                "notes":           r["notes"],
                "updated_at":      r["updated_at"].isoformat() if r["updated_at"]      else None,
                "updated_by":      r["updated_by_name"],
            }
            for r in rows
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/manual-inputs: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/manual-inputs")
@limiter.limit("20/minute")
def save_manual_inputs(
    request: Request,
    body: ManualInputsRequest,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()

        role = _live_role(cur, payload["email"])
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        # Re-authenticate
        cur.execute(
            "SELECT id, password_hash FROM users WHERE email = %s AND is_active = TRUE",
            (payload["email"],)
        )
        user_row = cur.fetchone()
        if not user_row or not verify_password(body.password, user_row["password_hash"]):
            raise HTTPException(status_code=401, detail="Incorrect password")

        user_id = user_row["id"]

        # Validate and insert
        saved = []
        for item in body.inputs:
            if item.category not in VALID_CATEGORIES:
                raise HTTPException(status_code=422, detail=f"Invalid category: {item.category}")
            if item.currency not in VALID_CURRENCIES:
                raise HTTPException(status_code=422, detail=f"Invalid currency: {item.currency}")

            inception = None
            if item.inception_date:
                try:
                    inception = date.fromisoformat(item.inception_date)
                except ValueError:
                    raise HTTPException(status_code=422, detail=f"Invalid inception_date: {item.inception_date}")

            cur.execute("""
                INSERT INTO manual_input
                    (entity_id, category, label, cost, current_value, prev_week_value,
                     currency, raw_amount, fx_rate, inception_date, notes, updated_by, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                RETURNING id
            """, (
                item.entity_id, item.category, item.label,
                item.cost, item.current_value, item.prev_week_value,
                item.currency, item.raw_amount, item.fx_rate,
                inception, item.notes, user_id,
            ))
            new_id = cur.fetchone()["id"]
            saved.append(new_id)

        write_audit_log(conn, user_id, "MANUAL_INPUT_SAVE", "manual_input",
                        None, f"Saved {len(saved)} manual input(s) by {payload['email']}")
        conn.commit()
        cur.close()

        return {"saved": len(saved), "ids": saved}

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/manual-inputs: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.delete("/api/v1/manual-inputs")
def delete_manual_input(
    request: Request,
    entity_id: int,
    category: str,
    label: str,
    authorization: Optional[str] = Header(None),
):
    """Delete a manual asset entirely (all versioned rows for the stable
    (entity_id, category, label) key) plus its art details and attachments +
    files. Admin (IWS) only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        # Remove attachment files from disk, then their rows.
        cur.execute(
            "SELECT stored_path, thumb_path FROM manual_attachment "
            "WHERE entity_id = %s AND category = %s AND label = %s",
            (entity_id, category, label),
        )
        for a in cur.fetchall():
            for rel in (a["stored_path"], a["thumb_path"]):
                if not rel:
                    continue
                try:
                    p = _uploads_abspath(rel)
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    logger.warning(f"could not remove attachment file {rel}: {e}")
        cur.execute(
            "DELETE FROM manual_attachment WHERE entity_id = %s AND category = %s AND label = %s",
            (entity_id, category, label),
        )
        if category == "art":
            cur.execute("DELETE FROM art_detail WHERE entity_id = %s AND label = %s",
                        (entity_id, label))
        cur.execute(
            "DELETE FROM manual_input WHERE entity_id = %s AND category = %s AND label = %s",
            (entity_id, category, label),
        )
        deleted = cur.rowcount
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        urow = cur.fetchone()
        write_audit_log(conn, urow["id"] if urow else None, "MANUAL_INPUT_DELETE",
                        "manual_input", None, f"{category}/{label} ({deleted} version(s))")
        conn.commit()
        cur.close()
        return {"deleted": deleted}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in DELETE /api/v1/manual-inputs: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Manual-asset attachments (artwork images, property deeds / plans / documents)
# and Art details (painter). Files live on the filesystem under UPLOADS_ROOT;
# the DB holds path + metadata only. Keyed by the STABLE (entity_id, category,
# label) — manual_input rows are versioned so their id is not stable. Admin (IWS)
# writes; the owning entity (and admin) can read/serve.
# ---------------------------------------------------------------------------

UPLOADS_ROOT          = os.getenv("UPLOADS_DIR", "/var/www/uploads")
MANUAL_UPLOAD_SUBDIR  = "manual"
MAX_UPLOAD_BYTES      = 25 * 1024 * 1024   # 25 MB per file
ATTACHMENT_KINDS      = {"art_image", "deed", "plan", "document"}


def _uploads_abspath(rel: str) -> str:
    """Resolve a stored-relative path to an absolute one, refusing traversal."""
    root = os.path.normpath(UPLOADS_ROOT)
    full = os.path.normpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid attachment path")
    return full


def _make_thumbnail(src_abs: str, dst_abs: str) -> bool:
    """Best-effort 480px JPEG thumbnail. Returns False if Pillow is unavailable
    or the source isn't a decodable image (callers then fall back to the original)."""
    try:
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(src_abs) as im:
            im.thumbnail((480, 480))
            im.convert("RGB").save(dst_abs, "JPEG", quality=80)
        return True
    except Exception:
        return False


def _member_entity(cur, payload) -> "Optional[int]":
    """Entity id a member is scoped to (None for admin)."""
    if _live_role(cur, payload["email"]) == "admin":
        return None
    return _resolve_entity(cur, payload, None)


@app.post("/api/v1/manual-attachments")
@limiter.limit("60/minute")
async def upload_manual_attachment(
    request: Request,
    entity_id: int = Form(...),
    category: str = Form(...),
    label: str = Form(...),
    kind: str = Form("document"),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Upload one file for a manual asset (admin/IWS only)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        if category not in VALID_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Invalid category: {category}")
        if not label.strip():
            raise HTTPException(status_code=422, detail="label is required")
        if kind not in ATTACHMENT_KINDS:
            kind = "document"

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 25 MB)")
        if not data:
            raise HTTPException(status_code=422, detail="Empty file")

        mime = (file.content_type
                or mimetypes.guess_type(file.filename or "")[0]
                or "application/octet-stream")
        ext  = os.path.splitext(file.filename or "")[1].lower()[:12]
        uid  = uuid.uuid4().hex
        rel_dir = os.path.join(MANUAL_UPLOAD_SUBDIR, str(entity_id))
        os.makedirs(os.path.join(UPLOADS_ROOT, rel_dir), exist_ok=True)
        rel_path = os.path.join(rel_dir, uid + ext)
        with open(_uploads_abspath(rel_path), "wb") as fh:
            fh.write(data)

        thumb_rel = None
        if mime.startswith("image/"):
            cand = os.path.join(rel_dir, uid + "_thumb.jpg")
            if _make_thumbnail(_uploads_abspath(rel_path), _uploads_abspath(cand)):
                thumb_rel = cand

        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        urow = cur.fetchone()
        user_id = urow["id"] if urow else None

        cur.execute(
            """
            INSERT INTO manual_attachment
                (entity_id, category, label, kind, original_name, stored_path,
                 thumb_path, mime, size_bytes, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, uploaded_at
            """,
            (entity_id, category, label.strip(), kind, file.filename, rel_path,
             thumb_rel, mime, len(data), user_id),
        )
        row = cur.fetchone()
        write_audit_log(conn, user_id, "MANUAL_ATTACHMENT_UPLOAD", "manual_attachment",
                        row["id"], f"{category}/{label} ({file.filename})")
        conn.commit()
        cur.close()
        return {
            "id":            row["id"],
            "kind":          kind,
            "original_name": file.filename,
            "mime":          mime,
            "size_bytes":    len(data),
            "has_thumb":     thumb_rel is not None,
            "uploaded_at":   row["uploaded_at"].isoformat(),
        }
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/manual-attachments: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


def _attachment_row(r: dict) -> dict:
    return {
        "id":            r["id"],
        "entity_id":     r["entity_id"],
        "category":      r["category"],
        "label":         r["label"],
        "kind":          r["kind"],
        "original_name": r["original_name"],
        "mime":          r["mime"],
        "size_bytes":    int(r["size_bytes"]) if r["size_bytes"] is not None else None,
        "has_thumb":     bool(r["thumb_path"]),
        "uploaded_at":   r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
    }


@app.get("/api/v1/manual-attachments")
def list_manual_attachments(
    request: Request,
    entity_id: Optional[int] = None,
    category: Optional[str] = None,
    label: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """List attachments, entity-scoped (a member sees only their entity's)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        conds, params = [], []
        if eid is not None:
            conds.append("entity_id = %s"); params.append(eid)
        if category:
            conds.append("category = %s"); params.append(category)
        if label:
            conds.append("label = %s"); params.append(label)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        cur.execute(
            f"""
            SELECT id, entity_id, category, label, kind, original_name,
                   thumb_path, mime, size_bytes, uploaded_at
            FROM   manual_attachment
            {where}
            ORDER BY uploaded_at DESC
            """,
            params,
        )
        rows = cur.fetchall()
        cur.close()
        return [_attachment_row(r) for r in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/manual-attachments: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


def _serve_attachment(att_id: int, request: Request, authorization, want_thumb: bool):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            "SELECT entity_id, stored_path, thumb_path, mime, original_name "
            "FROM manual_attachment WHERE id = %s",
            (att_id,),
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Attachment not found")
        member_eid = _member_entity(cur, payload)
        cur.close()
        if member_eid is not None and member_eid != r["entity_id"]:
            raise HTTPException(status_code=403, detail="Not permitted")

        use_thumb = want_thumb and bool(r["thumb_path"])
        rel       = r["thumb_path"] if use_thumb else r["stored_path"]
        abs_path  = _uploads_abspath(rel)
        if not os.path.exists(abs_path):
            raise HTTPException(status_code=404, detail="File missing on disk")
        media = "image/jpeg" if use_thumb else (r["mime"] or "application/octet-stream")
        # Inline so images/PDFs render in the browser; private + cacheable.
        return FileResponse(
            abs_path,
            media_type=media,
            headers={"Cache-Control": "private, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving attachment {att_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/manual-attachments/{att_id}/file")
def serve_manual_attachment_file(att_id: int, request: Request,
                                 authorization: Optional[str] = Header(None)):
    return _serve_attachment(att_id, request, authorization, want_thumb=False)


@app.get("/api/v1/manual-attachments/{att_id}/thumb")
def serve_manual_attachment_thumb(att_id: int, request: Request,
                                  authorization: Optional[str] = Header(None)):
    return _serve_attachment(att_id, request, authorization, want_thumb=True)


@app.delete("/api/v1/manual-attachments/{att_id}")
def delete_manual_attachment(att_id: int, request: Request,
                             authorization: Optional[str] = Header(None)):
    """Delete an attachment + its files (admin/IWS only)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute(
            "SELECT stored_path, thumb_path, category, label FROM manual_attachment WHERE id = %s",
            (att_id,),
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Attachment not found")
        for rel in (r["stored_path"], r["thumb_path"]):
            if not rel:
                continue
            try:
                p = _uploads_abspath(rel)
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logger.warning(f"could not remove attachment file {rel}: {e}")
        cur.execute("DELETE FROM manual_attachment WHERE id = %s", (att_id,))
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        urow = cur.fetchone()
        write_audit_log(conn, urow["id"] if urow else None, "MANUAL_ATTACHMENT_DELETE",
                        "manual_attachment", att_id, f"{r['category']}/{r['label']}")
        conn.commit()
        cur.close()
        return {"deleted": att_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error deleting attachment {att_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


class ArtDetailRequest(BaseModel):
    entity_id:     int
    label:         str
    painter_name:  Optional[str] = None
    painter_about: Optional[str] = None


@app.post("/api/v1/art-detail")
@limiter.limit("30/minute")
def save_art_detail(request: Request, body: ArtDetailRequest,
                    authorization: Optional[str] = Header(None)):
    """Upsert painter name / about for an Art entry (admin/IWS only)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        if not body.label.strip():
            raise HTTPException(status_code=422, detail="label is required")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        urow = cur.fetchone()
        user_id = urow["id"] if urow else None
        cur.execute(
            """
            INSERT INTO art_detail (entity_id, label, painter_name, painter_about, updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (entity_id, label) DO UPDATE SET
                painter_name  = EXCLUDED.painter_name,
                painter_about = EXCLUDED.painter_about,
                updated_by    = EXCLUDED.updated_by,
                updated_at    = NOW()
            """,
            (body.entity_id, body.label.strip(), body.painter_name, body.painter_about, user_id),
        )
        conn.commit()
        cur.close()
        return {"saved": True}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/art-detail: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/manual-assets")
def get_manual_assets(
    request: Request,
    category: str,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """Latest manual_input per (entity, label) for one category, entity-scoped,
    enriched with art_detail (painter) and grouped attachments. Drives the
    read-only Art and Properties pages for entity logins."""
    conn = None
    try:
        if category not in VALID_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Invalid category: {category}")
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)

        conds, params = ["m.category = %s"], [category]
        if eid is not None:
            conds.append("m.entity_id = %s"); params.append(eid)
        where = "WHERE " + " AND ".join(conds)
        cur.execute(
            f"""
            SELECT DISTINCT ON (m.entity_id, m.label)
                m.entity_id, e.entity_name, m.label, m.cost, m.current_value,
                m.currency, m.inception_date, m.notes, m.updated_at
            FROM   manual_input m
            JOIN   entity e ON e.id = m.entity_id
            {where}
            ORDER BY m.entity_id, m.label, m.updated_at DESC
            """,
            params,
        )
        assets = cur.fetchall()

        # Attachments for the same scope, grouped by (entity_id, label).
        acond, aparams = ["category = %s"], [category]
        if eid is not None:
            acond.append("entity_id = %s"); aparams.append(eid)
        cur.execute(
            f"""
            SELECT id, entity_id, category, label, kind, original_name,
                   thumb_path, mime, size_bytes, uploaded_at
            FROM   manual_attachment
            WHERE  {" AND ".join(acond)}
            ORDER BY uploaded_at DESC
            """,
            aparams,
        )
        att_by_key: dict = {}
        for a in cur.fetchall():
            att_by_key.setdefault((a["entity_id"], a["label"]), []).append(_attachment_row(a))

        # Art painter details.
        art_by_key: dict = {}
        if category == "art":
            dcond, dparams = [], []
            if eid is not None:
                dcond.append("entity_id = %s"); dparams.append(eid)
            dwhere = ("WHERE " + " AND ".join(dcond)) if dcond else ""
            cur.execute(
                f"SELECT entity_id, label, painter_name, painter_about FROM art_detail {dwhere}",
                dparams,
            )
            for d in cur.fetchall():
                art_by_key[(d["entity_id"], d["label"])] = {
                    "painter_name":  d["painter_name"],
                    "painter_about": d["painter_about"],
                }
        cur.close()

        out = []
        for m in assets:
            key = (m["entity_id"], m["label"])
            item = {
                "entity_id":     m["entity_id"],
                "entity_name":   m["entity_name"],
                "label":         m["label"],
                "cost":          float(m["cost"])          if m["cost"]          is not None else None,
                "current_value": float(m["current_value"]) if m["current_value"] is not None else None,
                "currency":      m["currency"],
                "inception_date": str(m["inception_date"]) if m["inception_date"] else None,
                "notes":         m["notes"],
                "updated_at":    m["updated_at"].isoformat() if m["updated_at"] else None,
                "attachments":   att_by_key.get(key, []),
            }
            if category == "art":
                item.update(art_by_key.get(key, {"painter_name": None, "painter_about": None}))
            out.append(item)

        total_value = round(sum(a["current_value"] or 0 for a in out), 2)
        return {
            "category":      category,
            "entity_id":     eid or 0,
            "total_value":   total_value,
            "count":         len(out),
            "assets":        out,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/manual-assets: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# FX rates (for manual input form reference)
# ---------------------------------------------------------------------------

@app.get("/api/v1/fx-rates")
def get_fx_rates(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (from_currency)
                from_currency, to_currency, rate, rate_date
            FROM fx_rate
            WHERE to_currency = 'INR'
            ORDER BY from_currency, rate_date DESC
        """)
        rows = cur.fetchall()
        cur.close()
        return {r["from_currency"]: {"rate": float(r["rate"]), "date": str(r["rate_date"])} for r in rows}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/fx-rates: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Bank accounts (cash-only; balances fed by uploaded statements or manual entry)
# ---------------------------------------------------------------------------

BANK_STATEMENT_DIR = "/var/www/mis-portal/bank_statements"
MAX_STATEMENT_BYTES = 15 * 1024 * 1024   # 15 MB


def _latest_fx_to_inr(cur) -> dict:
    """{currency: rate} for the most recent INR rate of each currency (INR→1.0)."""
    cur.execute("""
        SELECT DISTINCT ON (from_currency) from_currency, rate
        FROM fx_rate WHERE to_currency = 'INR'
        ORDER BY from_currency, rate_date DESC
    """)
    rates = {r["from_currency"]: float(r["rate"]) for r in cur.fetchall()}
    rates["INR"] = 1.0
    return rates


@app.get("/api/v1/bank-accounts")
def list_bank_accounts(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """List bank accounts with native balance + INR equivalent. Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        where  = "WHERE b.entity_id = %s" if entity_id else ""
        params = [entity_id] if entity_id else []
        cur.execute(f"""
            SELECT b.id, b.entity_id, e.entity_name, b.bank_name, b.account_type,
                   b.currency, b.balance, b.balance_as_of, b.notes, b.updated_at,
                   u.full_name AS updated_by_name,
                   (SELECT COUNT(*) FROM bank_statement s WHERE s.bank_account_id = b.id) AS statement_count
            FROM bank_account b
            JOIN entity e ON e.id = b.entity_id
            LEFT JOIN users u ON u.id = b.updated_by
            {where}
            ORDER BY e.entity_name, b.bank_name, b.account_type
        """, params)
        rows = cur.fetchall()
        fx = _latest_fx_to_inr(cur)
        cur.close()

        accounts, total_inr = [], 0.0
        for r in rows:
            bal = float(r["balance"]) if r["balance"] is not None else 0.0
            rate = fx.get(r["currency"])
            inr = bal * rate if rate is not None else None
            if inr is not None:
                total_inr += inr
            accounts.append({
                "id":             r["id"],
                "entity_id":      r["entity_id"],
                "entity_name":    r["entity_name"],
                "bank_name":      r["bank_name"],
                "account_type":   r["account_type"],
                "currency":       r["currency"],
                "balance":        bal,
                "balance_inr":    inr,
                "fx_rate":        rate,
                "balance_as_of":  str(r["balance_as_of"]) if r["balance_as_of"] else None,
                "notes":          r["notes"],
                "statement_count": r["statement_count"],
                "updated_at":     r["updated_at"].isoformat() if r["updated_at"] else None,
                "updated_by":     r["updated_by_name"],
            })
        return {"accounts": accounts, "total_inr": total_inr, "fx_rates": fx}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/bank-accounts: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/bank-accounts")
@limiter.limit("20/minute")
def create_bank_account(
    request: Request,
    body: BankAccountCreate,
    authorization: Optional[str] = Header(None),
):
    """Create a bank account (one entity per account). Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        if body.account_type not in VALID_BANK_ACCOUNT_TYPES:
            raise HTTPException(status_code=422, detail=f"Invalid account_type: {body.account_type}")
        if body.currency not in VALID_CURRENCIES:
            raise HTTPException(status_code=422, detail=f"Invalid currency: {body.currency}")

        cur.execute("SELECT id FROM entity WHERE id = %s", (body.entity_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=422, detail="Unknown entity_id")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        user_id = cur.fetchone()["id"]

        try:
            cur.execute("""
                INSERT INTO bank_account (entity_id, bank_name, account_type, currency, notes, updated_by)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """, (body.entity_id, body.bank_name.strip(), body.account_type,
                  body.currency, body.notes, user_id))
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            raise HTTPException(status_code=409,
                                detail="An account with this entity, bank, and type already exists.")
        new_id = cur.fetchone()["id"]
        write_audit_log(conn, user_id, "BANK_ACCOUNT_CREATE", "bank_account", new_id,
                        f"{body.bank_name} ({body.account_type}/{body.currency}) by {payload['email']}")
        conn.commit()
        cur.close()
        return {"id": new_id}

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/bank-accounts: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/bank-accounts/{account_id}/statements")
def list_bank_statements(
    account_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Upload history for one account. Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute("""
            SELECT s.id, s.filename, s.file_kind, s.parsed_balance, s.parsed_as_of,
                   s.parse_status, s.parse_note, s.committed, s.uploaded_at,
                   u.full_name AS uploaded_by_name
            FROM bank_statement s
            LEFT JOIN users u ON u.id = s.uploaded_by
            WHERE s.bank_account_id = %s
            ORDER BY s.uploaded_at DESC
        """, (account_id,))
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id":             r["id"],
                "filename":       r["filename"],
                "file_kind":      r["file_kind"],
                "parsed_balance": float(r["parsed_balance"]) if r["parsed_balance"] is not None else None,
                "parsed_as_of":   str(r["parsed_as_of"]) if r["parsed_as_of"] else None,
                "parse_status":   r["parse_status"],
                "parse_note":     r["parse_note"],
                "committed":      r["committed"],
                "uploaded_at":    r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
                "uploaded_by":    r["uploaded_by_name"],
            }
            for r in rows
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/bank-accounts/{account_id}/statements: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/bank-accounts/{account_id}/statements")
@limiter.limit("20/minute")
async def upload_bank_statement(
    account_id: int,
    request: Request,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Upload a statement (PDF/CSV/Excel), parse a best-guess balance, return it
    for the admin to confirm. Does NOT change the account balance — that happens
    on the separate /balance commit. Admin only."""
    from equity import bank_statements

    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        cur.execute("SELECT id FROM bank_account WHERE id = %s", (account_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Bank account not found")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        user_id = cur.fetchone()["id"]

        kind = bank_statements.detect_kind(file.filename or "")
        if kind is None:
            raise HTTPException(status_code=422,
                                detail="Unsupported file type — upload a PDF, CSV, or Excel statement.")

        data = await file.read()
        if len(data) == 0:
            raise HTTPException(status_code=422, detail="Uploaded file is empty.")
        if len(data) > MAX_STATEMENT_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 15 MB).")

        folder = os.path.join(BANK_STATEMENT_DIR, str(account_id))
        os.makedirs(folder, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(file.filename or f"statement.{kind}"))
        stored = os.path.join(folder, f"{datetime.utcnow():%Y%m%d%H%M%S}_{safe}")
        with open(stored, "wb") as fh:
            fh.write(data)
        os.chmod(stored, 0o600)

        parsed = bank_statements.parse(stored, file.filename)
        parsed_balance = float(parsed["balance"]) if parsed["balance"] is not None else None
        parsed_as_of   = parsed["as_of"]

        cur.execute("""
            INSERT INTO bank_statement
                (bank_account_id, filename, filepath, file_kind,
                 parsed_balance, parsed_as_of, parse_status, parse_note, uploaded_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (account_id, file.filename, stored, kind,
              parsed_balance, parsed_as_of, parsed["status"], parsed["note"], user_id))
        statement_id = cur.fetchone()["id"]
        write_audit_log(conn, user_id, "BANK_STATEMENT_UPLOAD", "bank_statement", statement_id,
                        f"{file.filename} → {parsed['status']} ({parsed_balance}) by {payload['email']}")
        conn.commit()
        cur.close()

        return {
            "statement_id":   statement_id,
            "parsed_balance": parsed_balance,
            "parsed_as_of":   str(parsed_as_of) if parsed_as_of else None,
            "parse_status":   parsed["status"],
            "parse_note":     parsed["note"],
            "file_kind":      kind,
        }

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/bank-accounts/{account_id}/statements: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/bank-accounts/{account_id}/balance")
@limiter.limit("30/minute")
def commit_bank_balance(
    account_id: int,
    request: Request,
    body: BankBalanceUpdate,
    authorization: Optional[str] = Header(None),
):
    """Commit a confirmed balance onto the account (admin-confirmed value from a
    parsed statement, or a plain manual entry). Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        cur.execute("SELECT id FROM bank_account WHERE id = %s", (account_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Bank account not found")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        user_id = cur.fetchone()["id"]

        as_of = None
        if body.balance_as_of:
            try:
                as_of = date.fromisoformat(body.balance_as_of)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid balance_as_of: {body.balance_as_of}")

        cur.execute("""
            UPDATE bank_account
               SET balance = %s, balance_as_of = %s,
                   notes = COALESCE(%s, notes), updated_at = NOW(), updated_by = %s
             WHERE id = %s
        """, (body.balance, as_of, body.notes, user_id, account_id))

        if body.statement_id is not None:
            cur.execute("""
                UPDATE bank_statement SET committed = TRUE
                 WHERE id = %s AND bank_account_id = %s
            """, (body.statement_id, account_id))

        write_audit_log(conn, user_id, "BANK_BALANCE_COMMIT", "bank_account", account_id,
                        f"balance={body.balance} as_of={body.balance_as_of} by {payload['email']}")
        conn.commit()
        cur.close()
        return {"id": account_id, "balance": body.balance}

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/bank-accounts/{account_id}/balance: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/bank-accounts/{account_id}/delete")
@limiter.limit("20/minute")
def delete_bank_account(
    account_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Delete a bank account and its statement history (files left on disk). Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        user_id = cur.fetchone()["id"]
        cur.execute("DELETE FROM bank_account WHERE id = %s RETURNING bank_name", (account_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bank account not found")
        write_audit_log(conn, user_id, "BANK_ACCOUNT_DELETE", "bank_account", account_id,
                        f"{row['bank_name']} by {payload['email']}")
        conn.commit()
        cur.close()
        return {"deleted": account_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/bank-accounts/{account_id}/delete: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Market benchmarks (Nifty/Sensex auto; GS bonds manual)
# ---------------------------------------------------------------------------

class BenchmarkEntry(BaseModel):
    code:       str
    label:      Optional[str] = None
    as_of_date: str                      # ISO yyyy-mm-dd
    value:      Optional[float] = None
    unit:       Optional[str] = "index"


class BenchmarkUpsertRequest(BaseModel):
    password: str
    entries:  list[BenchmarkEntry]


@app.get("/api/v1/benchmarks")
def get_benchmarks(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Current / prev-week / 31-Mar values + week%/YTD% per benchmark."""
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        from workers.report_generator import _fetch_benchmarks
        return _fetch_benchmarks(conn, date.today())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/benchmarks: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/benchmarks")
@limiter.limit("20/minute")
def save_benchmarks(
    request: Request,
    body: BenchmarkUpsertRequest,
    authorization: Optional[str] = Header(None),
):
    """Admin manual entry/override (used for GS-bond YTM/price which have no live feed)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        cur.execute("SELECT id, password_hash FROM users WHERE email = %s AND is_active = TRUE",
                    (payload["email"],))
        user_row = cur.fetchone()
        if not user_row or not verify_password(body.password, user_row["password_hash"]):
            raise HTTPException(status_code=401, detail="Incorrect password")
        user_id = user_row["id"]

        saved = 0
        for e in body.entries:
            try:
                as_of = date.fromisoformat(e.as_of_date)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid as_of_date: {e.as_of_date}")
            cur.execute("""
                INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_by, updated_at)
                VALUES (%s, COALESCE(%s, (SELECT label FROM market_benchmark WHERE code=%s ORDER BY as_of_date LIMIT 1), %s),
                        %s, %s, %s, 'manual', %s, NOW())
                ON CONFLICT (code, as_of_date)
                DO UPDATE SET value = EXCLUDED.value, label = EXCLUDED.label,
                              source = 'manual', updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """, (e.code, e.label, e.code, e.code, as_of, e.value, e.unit or "index", user_id))
            saved += 1

        write_audit_log(conn, user_id, "BENCHMARK_SAVE", "market_benchmark",
                        None, f"Saved {saved} benchmark value(s) by {payload['email']}")
        conn.commit()
        cur.close()
        return {"saved": saved}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/benchmarks: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Realised gains (FY-to-date; MF auto from CAS, equity from imported trades)
# ---------------------------------------------------------------------------

@app.get("/api/v1/realised-gains")
def get_realised_gains(
    request: Request,
    period: str = "fy",
    switches: str = "include",
    authorization: Optional[str] = Header(None),
):
    """Realised gains across all entities (admin).

    period   — "fy" (default, FY-to-date) or "inception" (whole history).
    switches — "include" (default) or "exclude" (drop SWITCH_IN/SWITCH_OUT).
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        from workers.report_generator import _fetch_realised_gains
        cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
        entities = cur.fetchall()
        cur.close()

        since_inception  = (period == "inception")
        include_switches = (switches != "exclude")
        out = []
        for e in entities:
            for r in _fetch_realised_gains(
                conn, [e["id"]], date.today(),
                since_inception=since_inception,
                include_switches=include_switches,
            ):
                out.append({
                    "entity":          e["entity_name"],
                    "group":           r["group"],
                    "security_name":   r["security_name"],
                    "purchase_amount": r["purchase_amount"],
                    "sale_date":       str(r["sale_date"]),
                    "sale_amount":     r["sale_amount"],
                    "pnl":             r["pnl"],
                    "return_pct":      r["return_pct"],
                })
        return out
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/realised-gains: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.get("/api/v1/reports")
def list_reports(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        cur.execute("""
            SELECT r.id, r.report_type, r.entity_name, r.filename,
                   r.as_of_date, r.generated_at, u.full_name AS generated_by_name
            FROM generated_report r
            LEFT JOIN users u ON u.id = r.generated_by
            ORDER BY r.generated_at DESC
            LIMIT 100
        """)
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "id":           r["id"],
                "type":         r["report_type"],
                "entity_name":  r["entity_name"],
                "filename":     r["filename"],
                "as_of_date":   str(r["as_of_date"]),
                "generated_at": r["generated_at"].isoformat(),
                "generated_by": r["generated_by_name"],
            }
            for r in rows
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/reports: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/reports/generate")
@limiter.limit("5/minute")
def generate_reports_endpoint(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        user_id = cur.fetchone()["id"]
        cur.close()

        from workers.report_generator import generate_reports
        results = generate_reports(conn, generated_by_user_id=user_id)

        write_audit_log(conn, user_id, "REPORT_GENERATE", "generated_report",
                        None, f"Generated {len(results)} reports by {payload['email']}")
        conn.commit()

        return {"generated": len(results), "reports": results}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/reports/generate: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/reports/{report_id}/download")
def download_report(
    report_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

        cur.execute("SELECT filepath, filename FROM generated_report WHERE id = %s", (report_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            raise HTTPException(status_code=404, detail="Report not found")
        if not os.path.exists(row["filepath"]):
            raise HTTPException(status_code=404, detail="Report file not found on disk")

        return FileResponse(
            path=row["filepath"],
            filename=row["filename"],
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/reports/{report_id}/download: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


def _verify_dhan_postback(request: Request) -> bool:
    """
    Constant-time check of the optional webhook shared secret. Accepts the secret in
    the `X-Postback-Token` header (preferred) or a `token` query param (use only if the
    caller can't set headers — query strings can leak into access logs). Returns True
    when no secret is configured (open, backward-compatible).
    """
    if not DHAN_POSTBACK_SECRET:
        return True
    presented = request.headers.get("x-postback-token") or request.query_params.get("token") or ""
    return hmac.compare_digest(presented, DHAN_POSTBACK_SECRET)


@app.post("/api/v1/dhan/postback")
@limiter.limit("120/minute")
async def dhan_postback(request: Request):
    """
    Dhan order-update postback (webhook).
    Dhan POSTs JSON on every order/trade event.
    We log it and return 200 — downstream processing can be added here.

    Unauthenticated by necessity (Dhan sends no JWT and no signature). Set
    DHAN_POSTBACK_SECRET to require a shared secret before this is trusted for any
    state-changing work; until then it only logs.
    """
    if not _verify_dhan_postback(request):
        logger.warning("Dhan postback rejected — bad/missing shared secret")
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not DHAN_POSTBACK_SECRET:
        logger.warning("Dhan postback is UNAUTHENTICATED — set DHAN_POSTBACK_SECRET before "
                       "adding any state-changing processing here")
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Redacted log: only non-sensitive routing fields, never the full payload (which can
    # carry account/order identifiers). Adjust the allow-list when real processing lands.
    summary = {k: body.get(k) for k in ("orderId", "orderStatus", "transactionType",
                                        "tradingSymbol", "exchangeSegment") if k in body} \
        if isinstance(body, dict) else {}
    logger.info(f"Dhan postback received (fields={sorted(body) if isinstance(body, dict) else 'n/a'}): {summary}")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Jarvis — read-only portfolio advisory assistant
# ---------------------------------------------------------------------------

class AssistantConversationCreate(BaseModel):
    title:     Optional[str] = Field(default=None, max_length=300)
    entity_id: Optional[int] = None   # admins only; ignored for members


class AssistantChatRequest(BaseModel):
    conversation_id: int
    message:         str = Field(min_length=1, max_length=4000)


def _assistant_user_id(cursor, email: str) -> int:
    """Numeric users.id for the authenticated email, or 401 if not active."""
    cursor.execute(
        "SELECT id FROM users WHERE email = %s AND is_active = TRUE",
        (email,),
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return row["id"]


def _resolve_assistant_scope(cursor, payload: dict, requested_entity_id: Optional[int]) -> Optional[int]:
    """
    Scope rules for the assistant (stricter than _resolve_entity for the admin all-entities
    case): admins may scope to a specific entity OR to all entities (None); members are always
    pinned to their own entity regardless of what was requested. Uses the live DB role.
    """
    role = _live_role(cursor, payload["email"])
    if role == "admin":
        return requested_entity_id  # None = whole family, N = single entity
    cursor.execute(
        "SELECT entity_id FROM users WHERE email = %s AND is_active = TRUE",
        (payload["email"],),
    )
    row = cursor.fetchone()
    if not row or not row["entity_id"]:
        raise HTTPException(status_code=404, detail="No entity linked to this user")
    return row["entity_id"]


@app.post("/api/v1/assistant/conversations")
@limiter.limit("30/minute")
def assistant_create_conversation(
    request: Request,
    body: AssistantConversationCreate,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        user_id = _assistant_user_id(cursor, payload["email"])
        scope_eid = _resolve_assistant_scope(cursor, payload, body.entity_id)
        conv = assistant_persistence.create_conversation(conn, user_id, body.title, scope_eid)
        conn.commit()
        return conv
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in POST /api/v1/assistant/conversations: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/assistant/conversations")
def assistant_list_conversations(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        user_id = _assistant_user_id(cursor, payload["email"])
        return {"conversations": assistant_persistence.list_conversations(conn, user_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/assistant/conversations: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/assistant/conversations/{conversation_id}")
def assistant_get_conversation(
    request: Request,
    conversation_id: int,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        user_id = _assistant_user_id(cursor, payload["email"])
        conv = assistant_persistence.get_conversation(conn, user_id, conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conv["messages"] = assistant_persistence.get_messages(conn, conversation_id)
        return conv
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/assistant/conversations/{conversation_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/assistant/conversations/{conversation_id}/archive")
@limiter.limit("30/minute")
def assistant_archive_conversation(
    request: Request,
    conversation_id: int,
    authorization: Optional[str] = Header(None),
):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        user_id = _assistant_user_id(cursor, payload["email"])
        ok = assistant_persistence.archive_conversation(conn, user_id, conversation_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conn.commit()
        return {"status": "archived", "conversation_id": conversation_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in POST /api/v1/assistant/conversations/{conversation_id}/archive: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/assistant/chat")
@limiter.limit("15/minute")
def assistant_chat(
    request: Request,
    body: AssistantChatRequest,
    authorization: Optional[str] = Header(None),
):
    """
    Send a message in a conversation; stream the answer as Server-Sent Events.
    The DB connection is held for the lifetime of the stream (tools query it under the
    conversation's entity scope) and released when the generator completes.
    """
    payload = _require_auth(request, authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        user_id = _assistant_user_id(cursor, payload["email"])
        conv = assistant_persistence.get_conversation(conn, user_id, body.conversation_id)
        if not conv:
            release_db_connection(conn)
            raise HTTPException(status_code=404, detail="Conversation not found")

        scope_eid = _resolve_assistant_scope(cursor, payload, conv["scope_entity_id"])
        history = assistant_persistence.get_messages(conn, conv["id"])

        assistant_persistence.add_message(conn, conv["id"], "user", body.message)
        # Auto-title on the first message of an untitled conversation (like Claude/ChatGPT).
        # Committed here, before streaming, so it's persisted by the time the client
        # refetches the conversation list on the stream's "done" event. Best-effort.
        if not history and not (conv.get("title") or "").strip():
            title = assistant_engine.generate_title(body.message)
            if title:
                assistant_persistence.update_title(conn, conv["id"], title)
        write_audit_log(conn, user_id, "assistant_chat", "assistant_conversation",
                        conv["id"], body.message[:500])
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        release_db_connection(conn)
        logger.error(f"Error preparing assistant chat: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

    def event_stream():
        final_content, citations, tool_names, charts = "", [], [], []
        try:
            for ev in assistant_engine.run_stream(history, body.message, scope_eid, conn):
                if ev.get("type") == "done":
                    final_content = ev.get("content", "")
                    citations = ev.get("citations", [])
                    tool_names = ev.get("tool_names", [])
                    charts = ev.get("charts", [])
                yield f"data: {json.dumps(ev)}\n\n"
            assistant_persistence.add_message(
                conn, conv["id"], "assistant", final_content,
                tool_calls=(tool_names or None), citations=(citations or None),
                charts=(charts or None),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Error during assistant stream: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            yield f"data: {json.dumps({'type': 'error', 'message': 'stream failed'})}\n\n"
        finally:
            release_db_connection(conn)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
