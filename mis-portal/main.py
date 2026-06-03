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
            "exp": datetime.utcnow() + timedelta(hours=2)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        logger.info(f"Successful login: {email}")

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=7200
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

@app.post("/api/v1/auth/refresh")
def refresh_token(request: Request, response: Response, authorization: Optional[str] = Header(None)):
    """Issue a new 2-hour token if the current token is valid and not blacklisted."""
    try:
        payload = _require_auth(request, authorization)

        new_payload = {
            "user_id": payload["user_id"],
            "email": payload["email"],
            "role": payload["role"],
            "exp": datetime.utcnow() + timedelta(hours=2),
        }
        new_token = jwt.encode(new_payload, SECRET_KEY, algorithm="HS256")

        response.set_cookie(
            key="access_token",
            value=new_token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=7200,
        )
        return {"message": "Token refreshed"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")

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
    """Get all entities - admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

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
                    h.remarks,
                    sm.isin,
                    sm.security_name,
                    sm.security_type,
                    sm.asset_class,
                    sm.amfi_code,
                    e.entity_name
                FROM holding h
                JOIN security_master sm ON sm.id = h.security_id
                JOIN entity e ON e.id = h.entity_id
                ORDER BY sm.asset_class, sm.security_name, h.folio_number
            """)
            rows = cursor.fetchall()
            cursor.close()

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
                h.remarks,
                sm.isin,
                sm.security_name,
                sm.security_type,
                sm.asset_class,
                sm.amfi_code
            FROM holding h
            JOIN security_master sm ON sm.id = h.security_id
            WHERE h.entity_id = %s
            ORDER BY sm.asset_class, sm.security_name, h.folio_number
        """, (eid,))
        rows = cursor.fetchall()
        cursor.close()

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


@app.get("/api/holdings")
def get_holdings_legacy(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    return get_holdings(request, entity_id, authorization)


def _resolve_entity(cursor, payload: dict, entity_id_param: Optional[int]) -> Optional[int]:
    """
    Returns the entity_id to query.
    - Admin + entity_id_param  → use the param
    - Admin + no param         → None (all entities)
    - Member                   → their own entity_id from users table
    """
    role = payload.get("role", "member")
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
    eh.remarks,
    eh.updated_at
"""


def _row_to_holding(r: dict) -> dict:
    return {
        "id":                    r["id"],
        "entity_id":             r["entity_id"],
        "entity_name":           r.get("entity_name"),
        "broker":                r["broker"],
        "symbol":                r["symbol"],
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
        "remarks":               r["remarks"],
        "updated_at":            r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _equity_totals(rows: list[dict]) -> dict:
    def s(key):
        return round(sum(r[key] or 0 for r in rows), 2)
    return {
        "total_cost":             s("cost"),
        "total_current_value":    s("current_market_value"),
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
            "entity_id":   eid or 0,
            "entity_name": entity_name,
            "broker":      broker,
            "count":       len(holdings),
            **totals,
            "holdings":    holdings,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/equity/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/equity/holdings")
def get_equity_holdings_legacy(
    request: Request,
    entity_id: Optional[int] = None,
    broker: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    return get_equity_holdings(request, entity_id, broker, authorization)


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


@app.get("/api/equity/summary")
def get_equity_summary_legacy(
    request: Request,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    return get_equity_summary(request, entity_id, authorization)


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
