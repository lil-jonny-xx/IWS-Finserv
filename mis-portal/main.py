from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from typing import Optional
import jwt
import bcrypt
from datetime import datetime, timedelta
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
load_dotenv('/var/www/mis-portal/.env', override=True)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="IWS MIS Portal API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None
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

def blacklist_token(token: str, expiry_seconds: int = 86400) -> bool:
    if redis_client is None:
        logger.error("Redis unavailable - cannot revoke token")
        return False
    try:
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

@app.get("/api/health")
def health_check_legacy(request: Request, authorization: Optional[str] = Header(None)):
    return health_check(request, authorization)

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
            "exp": datetime.utcnow() + timedelta(hours=24)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        logger.info(f"Successful login: {email}")

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=86400
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

@app.post("/api/auth/login")
@limiter.limit("5/minute")
def login_legacy(request: Request, login_request: LoginRequest, response: Response):
    return _login_impl(request, login_request, response)

@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    """Logout - revokes JWT token and clears cookie."""
    conn = None
    try:
        token = get_token_from_request(request, authorization)
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if not blacklist_token(token):
            raise HTTPException(status_code=503, detail="Logout failed - please try again")
        response.delete_cookie(key="access_token", httponly=True, secure=True, samesite="strict")

        conn = get_db_connection()
        write_audit_log(conn, payload.get("user_id"), "LOGOUT", "users", payload.get("user_id"), "User logged out")
        conn.commit()

        return {"message": "Logged out successfully"}

    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except (KeyError, IndexError):
        raise HTTPException(status_code=401, detail="Invalid token format")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)

@app.post("/api/auth/logout")
def logout_legacy(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    return logout(request, response, authorization)

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

@app.get("/api/me")
def get_current_user_legacy(request: Request, authorization: Optional[str] = Header(None)):
    return get_current_user(request, authorization)

@app.get("/api/v1/entities")
def get_entities(request: Request, authorization: Optional[str] = Header(None)):
    """Get all entities - requires auth."""
    conn = None
    try:
        token = get_token_from_request(request, authorization)

        if is_token_blacklisted(token):
            raise HTTPException(status_code=401, detail="Token has been revoked")

        jwt.decode(token, SECRET_KEY, algorithms=["HS256"])

        conn = get_db_connection()
        cursor = conn.cursor()
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

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/entities: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)

@app.get("/api/entities")
def get_entities_legacy(request: Request, authorization: Optional[str] = Header(None)):
    return get_entities(request, authorization)


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


def _compute_realized_gains(conn, entity_id=None):
    """
    Compute realized capital gains per (entity_id, security_id, folio_number)
    using the average cost method.
    Inflows: PURCHASE, PURCHASE_SIP, SWITCH_IN (also adds STAMP_DUTY_TAX to cost basis).
    Outflows: REDEMPTION, SWITCH_OUT (gain = proceeds - redeemed_units × avg_cost).
    Returns dict keyed by (entity_id, security_id, folio_number) → float.
    """
    cursor = conn.cursor()
    query = """
        SELECT entity_id, security_id, folio_number,
               transaction_date, transaction_type, amount, units, id
        FROM mf_transaction
    """
    params: list = []
    if entity_id is not None:
        query += " WHERE entity_id = %s"
        params.append(entity_id)
    query += " ORDER BY entity_id, security_id, folio_number, transaction_date, id"
    cursor.execute(query, params)
    txns = cursor.fetchall()
    cursor.close()

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for t in txns:
        key = (t["entity_id"], t["security_id"], t["folio_number"])
        groups[key].append(t)

    realized: dict = {}
    for key, txn_list in groups.items():
        running_units = 0.0
        running_cost  = 0.0
        total_gain    = 0.0
        for t in txn_list:
            amt   = float(t["amount"]) if t["amount"]  else 0.0
            units = float(t["units"])  if t["units"]   else 0.0
            ttype = t["transaction_type"]
            if ttype in ("REDEMPTION", "SWITCH_OUT"):
                redeemed = abs(units)
                proceeds = abs(amt)
                if running_units > 0.001:
                    avg_cost     = running_cost / running_units
                    total_gain  += proceeds - redeemed * avg_cost
                    running_units -= redeemed
                    running_cost  -= redeemed * avg_cost
                    if running_units < 0.001:
                        running_units = 0.0
                        running_cost  = 0.0
            elif ttype == "STAMP_DUTY_TAX":
                # Stamp duty is part of acquisition cost
                running_cost += amt
            elif units > 0 and amt > 0:
                running_units += units
                running_cost  += amt
        realized[key] = round(total_gain, 2)
    return realized


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
        user_role = payload.get("role", "member")

        conn = get_db_connection()
        cursor = conn.cursor()

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
                    h.last_updated_nav        AS nav,
                    h.current_value,
                    h.last_updated,
                    h.market_value_as_on,
                    h.as_of_date,
                    h.prev_week_value,
                    h.weekly_change,
                    h.exposure_pct,
                    h.pnl_inception,
                    h.pnl_ytd,
                    h.pnl_weekly_change,
                    h.returns_inception_pct,
                    h.returns_ytd_pct,
                    h.cagr_inception_pct,
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

            realized_gains = _compute_realized_gains(conn)

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
                    "first_invested_date":    str(r["first_invested_date"]) if r["first_invested_date"] else None,
                    "last_updated":           r["last_updated"].isoformat() if r["last_updated"] else None,
                    "entity_name":            r["entity_name"],
                    "pan_group_name":         r["pan_group_name"],
                    "realized_gain":          realized_gains.get(rg_key, 0.0),
                    "market_value_as_on":     float(r["market_value_as_on"])    if r["market_value_as_on"]    else None,
                    "as_of_date":             str(r["as_of_date"])              if r["as_of_date"]            else None,
                    "prev_week_value":        float(r["prev_week_value"])       if r["prev_week_value"]       else None,
                    "weekly_change":          float(r["weekly_change"])         if r["weekly_change"]         else None,
                    "exposure_pct":           float(r["exposure_pct"])          if r["exposure_pct"]          else None,
                    "pnl_inception":          float(r["pnl_inception"])         if r["pnl_inception"]         else None,
                    "pnl_ytd":                float(r["pnl_ytd"])               if r["pnl_ytd"]               else None,
                    "pnl_weekly_change":      float(r["pnl_weekly_change"])     if r["pnl_weekly_change"]     else None,
                    "returns_inception_pct":  float(r["returns_inception_pct"]) if r["returns_inception_pct"] else None,
                    "returns_ytd_pct":        float(r["returns_ytd_pct"])       if r["returns_ytd_pct"]       else None,
                    "cagr_inception_pct":     float(r["cagr_inception_pct"])    if r["cagr_inception_pct"]    else None,
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
                h.last_updated_nav        AS nav,
                h.current_value,
                h.last_updated,
                h.market_value_as_on,
                h.as_of_date,
                h.prev_week_value,
                h.weekly_change,
                h.exposure_pct,
                h.pnl_inception,
                h.pnl_ytd,
                h.pnl_weekly_change,
                h.returns_inception_pct,
                h.returns_ytd_pct,
                h.cagr_inception_pct,
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

        realized_gains = _compute_realized_gains(conn, entity_id=eid)

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
                "first_invested_date":    str(r["first_invested_date"]) if r["first_invested_date"] else None,
                "last_updated":           r["last_updated"].isoformat() if r["last_updated"] else None,
                "pan_group_name":         r["pan_group_name"],
                "realized_gain":          realized_gains.get(rg_key, 0.0),
                "market_value_as_on":     float(r["market_value_as_on"])    if r["market_value_as_on"]    else None,
                "as_of_date":             str(r["as_of_date"])              if r["as_of_date"]            else None,
                "prev_week_value":        float(r["prev_week_value"])       if r["prev_week_value"]       else None,
                "weekly_change":          float(r["weekly_change"])         if r["weekly_change"]         else None,
                "exposure_pct":           float(r["exposure_pct"])          if r["exposure_pct"]          else None,
                "pnl_inception":          float(r["pnl_inception"])         if r["pnl_inception"]         else None,
                "pnl_ytd":                float(r["pnl_ytd"])               if r["pnl_ytd"]               else None,
                "pnl_weekly_change":      float(r["pnl_weekly_change"])     if r["pnl_weekly_change"]     else None,
                "returns_inception_pct":  float(r["returns_inception_pct"]) if r["returns_inception_pct"] else None,
                "returns_ytd_pct":        float(r["returns_ytd_pct"])       if r["returns_ytd_pct"]       else None,
                "cagr_inception_pct":     float(r["cagr_inception_pct"])    if r["cagr_inception_pct"]    else None,
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


@app.get("/api/holdings")
def get_holdings_legacy(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    return get_holdings(request, entity_id, authorization)


@app.get("/api/v1/equity/holdings")
def get_equity_holdings(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """Return equity holdings from equity_holding table for the requesting user's entity."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        user_role = payload.get("role", "member")

        conn = get_db_connection()
        cursor = conn.cursor()

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
                eid = None
            else:
                eid = row["entity_id"]

        if eid is not None:
            cursor.execute(
                "SELECT entity_name FROM entity WHERE id = %s",
                (eid,)
            )
            entity_row = cursor.fetchone()
            entity_name = entity_row["entity_name"] if entity_row else None
            cursor.execute("""
                SELECT eh.*, e.entity_name
                FROM equity_holding eh
                JOIN entity e ON e.id = eh.entity_id
                WHERE eh.entity_id = %s
                ORDER BY eh.current_market_value DESC NULLS LAST
            """, (eid,))
        else:
            entity_name = None
            cursor.execute("""
                SELECT eh.*, e.entity_name
                FROM equity_holding eh
                JOIN entity e ON e.id = eh.entity_id
                ORDER BY eh.entity_id, eh.current_market_value DESC NULLS LAST
            """)

        rows = cursor.fetchall()
        holdings = []
        for r in rows:
            h = dict(r)
            for k, v in h.items():
                if hasattr(v, '__float__'):
                    h[k] = float(v)
                elif hasattr(v, 'isoformat'):
                    h[k] = v.isoformat()
            holdings.append(h)

        totals = {
            "total_cost":                  sum(h.get("cost") or 0 for h in holdings),
            "total_current_market_value":  sum(h.get("current_market_value") or 0 for h in holdings),
            "total_pnl_inception":         sum(h.get("pnl_inception") or 0 for h in holdings),
            "total_pnl_ytd":               sum(h.get("pnl_ytd") or 0 for h in holdings),
            "total_weekly_change":         sum(h.get("weekly_change") or 0 for h in holdings),
            "grand_total":                 sum(h.get("current_market_value") or 0 for h in holdings),
        }

        return {
            "entity_id":   eid,
            "entity_name": entity_name,
            "holdings":    holdings,
            "totals":      totals,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/equity/holdings: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            release_db_connection(conn)


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

        # ── MF holdings (all entities) ────────────────────────────────────────
        cursor.execute("""
            SELECT
                h.entity_id,
                e.entity_name,
                h.asset_class,
                h.security_type,
                COALESCE(h.invested_amount, 0)        AS invested,
                COALESCE(h.market_value_as_on, h.current_value, 0) AS mkt_value,
                COALESCE(h.pnl_inception, 0)          AS pnl_inception,
                COALESCE(h.pnl_ytd, 0)                AS pnl_ytd,
                COALESCE(h.weekly_change, 0)           AS weekly_change,
                h.cagr_inception_pct,
                COALESCE(h.market_value_as_on, h.current_value, 0) AS weight
            FROM holding h
            JOIN entity e ON e.id = h.entity_id
        """)
        mf_rows = cursor.fetchall()

        # ── Equity holdings (all entities) ────────────────────────────────────
        cursor.execute("""
            SELECT
                eh.entity_id,
                e.entity_name,
                'DIRECT_EQUITY'                        AS asset_class,
                'DIRECT_EQUITY'                        AS security_type,
                COALESCE(eh.cost, 0)                   AS invested,
                COALESCE(eh.current_market_value, 0)   AS mkt_value,
                COALESCE(eh.pnl_inception, 0)          AS pnl_inception,
                COALESCE(eh.pnl_ytd, 0)                AS pnl_ytd,
                COALESCE(eh.weekly_change, 0)          AS weekly_change,
                eh.cagr_inception_pct,
                COALESCE(eh.current_market_value, 0)   AS weight
            FROM equity_holding eh
            JOIN entity e ON e.id = eh.entity_id
        """)
        eq_rows = cursor.fetchall()

        all_rows = list(mf_rows) + list(eq_rows)

        def row_val(r, key):
            v = r[key]
            return float(v) if v is not None else 0.0

        # ── Overall summary ───────────────────────────────────────────────────
        total_invested = sum(row_val(r, "invested")     for r in all_rows)
        total_value    = sum(row_val(r, "mkt_value")    for r in all_rows)
        total_pnl      = sum(row_val(r, "pnl_inception") for r in all_rows)
        total_pnl_ytd  = sum(row_val(r, "pnl_ytd")     for r in all_rows)
        total_weekly   = sum(row_val(r, "weekly_change") for r in all_rows)

        # Weighted CAGR across all holdings
        w_sum, w_cagr = 0.0, 0.0
        for r in all_rows:
            if r["cagr_inception_pct"] is not None:
                w = row_val(r, "weight")
                w_cagr += float(r["cagr_inception_pct"]) * w
                w_sum  += w
        weighted_cagr = round(w_cagr / w_sum, 4) if w_sum > 0 else None

        # ── Asset class breakdown (combined) ──────────────────────────────────
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

        # ── Per-entity breakdown ──────────────────────────────────────────────
        entity_map: dict = {}
        for r in all_rows:
            eid  = r["entity_id"]
            ename = r["entity_name"]
            cls  = r["asset_class"]
            if eid not in entity_map:
                entity_map[eid] = {
                    "entity_id":   eid,
                    "entity_name": ename,
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

            # bucket by broad class: MF vs DIRECT_EQUITY vs future
            broad = "DIRECT_EQUITY" if cls == "DIRECT_EQUITY" else "MF"
            em["asset_classes"].setdefault(broad, {"invested": 0.0, "value": 0.0, "pnl": 0.0})
            em["asset_classes"][broad]["invested"] += row_val(r, "invested")
            em["asset_classes"][broad]["value"]    += row_val(r, "mkt_value")
            em["asset_classes"][broad]["pnl"]      += row_val(r, "pnl_inception")

        entities_out = []
        for em in sorted(entity_map.values(), key=lambda x: -x["total_value"]):
            ev    = em["total_value"]
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
                "entity_id":    em["entity_id"],
                "entity_name":  em["entity_name"],
                "total_invested": round(em["total_invested"], 2),
                "total_value":    round(em["total_value"],    2),
                "total_pnl":      round(em["total_pnl"],      2),
                "total_pnl_ytd":  round(em["total_pnl_ytd"],  2),
                "total_weekly":   round(em["total_weekly"],   2),
                "asset_classes":  classes,
            })

        return {
            "summary": {
                "total_invested":  round(total_invested, 2),
                "total_value":     round(total_value,    2),
                "total_pnl":       round(total_pnl,      2),
                "total_pnl_ytd":   round(total_pnl_ytd,  2),
                "total_weekly":    round(total_weekly,   2),
                "weighted_cagr":   weighted_cagr,
            },
            "asset_class_breakdown": asset_class_breakdown,
            "entities": entities_out,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/overview: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            release_db_connection(conn)


@app.get("/api/v1/transactions")
def get_transactions(
    request: Request,
    entity_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    authorization: Optional[str] = Header(None),
):
    """Return MF transactions for the requesting user's entity."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        user_role = payload.get("role", "member")

        conn = get_db_connection()
        cursor = conn.cursor()

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

        if eid is None:
            # Admin all-entities view
            cursor.execute("""
                SELECT
                    t.id, t.transaction_date, t.description, t.transaction_type,
                    t.amount, t.units, t.nav, t.balance_units, t.folio_number,
                    sm.security_name, sm.isin, e.entity_name
                FROM mf_transaction t
                JOIN security_master sm ON sm.id = t.security_id
                JOIN entity e ON e.id = t.entity_id
                ORDER BY t.transaction_date DESC, t.id DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) AS total FROM mf_transaction")
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

        cursor.execute("""
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
            WHERE t.entity_id = %s
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT %s OFFSET %s
        """, (eid, limit, offset))
        rows = cursor.fetchall()

        cursor.execute(
            "SELECT COUNT(*) AS total FROM mf_transaction WHERE entity_id = %s",
            (eid,)
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
