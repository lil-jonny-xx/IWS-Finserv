from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
import jwt
import bcrypt
from datetime import datetime, timedelta, date
import os
from dotenv import load_dotenv
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import redis
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
import logging
import hashlib
import atexit

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
                    expiry_seconds = 900
            except Exception:
                expiry_seconds = 900
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

@app.get("/api/ping")
def ping():
    """Public health check for uptime monitoring."""
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
            "exp": datetime.utcnow() + timedelta(minutes=15)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        logger.info(f"Successful login: {email}")

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=900
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
        if entity_id is not None and user_role == "admin":
            eid = entity_id
        else:
            cursor.execute(
                "SELECT entity_id FROM users WHERE email = %s AND is_active = TRUE",
                (payload["email"],)
            )
            row = cursor.fetchone()
            if not row or not row["entity_id"]:
                if user_role != "admin":
                    raise HTTPException(status_code=404, detail="No entity linked to this user")
                eid = None  # admin all-entities view
            else:
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
    if entity_id_param is not None and role == "admin":
        return entity_id_param
    cursor.execute(
        "SELECT entity_id FROM users WHERE email = %s AND is_active = TRUE",
        (payload["email"],),
    )
    row = cursor.fetchone()
    if row and row["entity_id"]:
        return row["entity_id"]
    if role == "admin":
        return None  # all-entities view
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
    eh.first_invested_date,
    eh.sector,
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
        "first_invested_date":   str(r["first_invested_date"]) if r["first_invested_date"] else None,
        "sector":                r["sector"],
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
        cur.close()

        holdings = [_row_to_holding(r) for r in rows]
        totals   = _equity_totals(rows)

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
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/equity/holdings: {e}")
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
        cur.close()

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
                'DIRECT_EQUITY'                       AS asset_class,
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
        """)
        eq_rows = cursor.fetchall()
        cursor.close()

        all_rows = list(mf_rows) + list(eq_rows)

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

        asset_class_breakdown = [
            {
                "asset_class": cls,
                "invested":    round(v["invested"], 2),
                "value":       round(v["value"],    2),
                "pnl":         round(v["pnl"],      2),
                "pct":         round(v["value"] / total_value * 100, 2) if total_value else 0,
            }
            for cls, v in sorted(class_totals.items(), key=lambda x: -x[1]["value"])
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

            broad = "DIRECT_EQUITY" if cls == "DIRECT_EQUITY" else "MF"
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
    "unlisted", "startup",
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
    authorization: Optional[str] = Header(None),
):
    """FY-to-date realised gains across all entities (admin)."""
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

        out = []
        for e in entities:
            for r in _fetch_realised_gains(conn, [e["id"]], date.today()):
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


@app.post("/api/v1/dhan/postback")
async def dhan_postback(request: Request):
    """
    Dhan order-update postback (webhook).
    Dhan POSTs JSON on every order/trade event.
    We log it and return 200 — downstream processing can be added here.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.info(f"Dhan postback received: {body}")
    return {"status": "ok"}
