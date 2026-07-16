from fastapi import FastAPI, HTTPException, Header, Request, Response, UploadFile, File, Form, Query
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
import urllib.parse
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
from equity.finmath import xirr as _xirr
import property_docs

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
            "SELECT id, email, password_hash, role, failed_attempts, locked_until, token_version FROM users WHERE email = %s AND is_active = TRUE",
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
            # Session-revocation counter — must match users.token_version on every
            # request (see _require_auth). A password change / admin reset bumps the
            # column, instantly invalidating tokens minted before the bump.
            "token_version": user_row.get("token_version", 0),
            "iat": datetime.utcnow(),
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

# ---------------------------------------------------------------------------
# Password management — self-service change + admin-mediated reset (no email)
# ---------------------------------------------------------------------------

class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class AdminResetPasswordRequest(BaseModel):
    email: EmailStr
    new_password: str = Field(min_length=8, max_length=72)


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _validate_password_strength(pw: str) -> None:
    if not (8 <= len(pw) <= 72):
        raise HTTPException(status_code=400, detail="Password must be 8–72 characters.")
    if not (any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)):
        raise HTTPException(status_code=400,
                            detail="Password must include an uppercase letter, a lowercase letter, and a digit.")


_GENERIC_FORGOT_MSG = ("If an account exists for that email, your administrator has been "
                       "notified and will reset the password.")


@app.post("/api/v1/auth/change-password")
def change_password(request: Request, body: ChangePasswordRequest, response: Response,
                    authorization: Optional[str] = Header(None)):
    """Authenticated self-service password change: verify the current password, set a new one."""
    payload = _require_auth(request, authorization)
    _validate_password_strength(body.new_password)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, password_hash, role FROM users WHERE email = %s AND is_active = TRUE",
                    (payload["email"],))
        row = cur.fetchone()
        if not row or not verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
        if verify_password(body.new_password, row["password_hash"]):
            raise HTTPException(status_code=400,
                                detail="New password must be different from the current one.")
        # Bump token_version so every OTHER live session for this user is revoked on
        # its next request (a password change should log out sessions the user no
        # longer controls). RETURNING gives us the new value to re-mint this caller's
        # own token below, so the session that made the change stays logged in.
        cur.execute("UPDATE users SET password_hash = %s, token_version = token_version + 1 "
                    "WHERE id = %s RETURNING token_version",
                    (_hash_password(body.new_password), row["id"]))
        new_version = cur.fetchone()["token_version"]
        write_audit_log(conn, row["id"], "CHANGE_PASSWORD", "users", row["id"],
                        f"Password changed by {payload['email']}")
        conn.commit()
        # Re-issue this session's cookie with the new token_version so it isn't caught
        # by the revocation above. Header/Bearer clients must re-authenticate.
        new_token = jwt.encode(
            {"user_id": row["id"], "email": payload["email"], "role": row["role"],
             "token_version": new_version, "iat": datetime.utcnow(),
             "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
            SECRET_KEY, algorithm="HS256")
        response.set_cookie(key="access_token", value=new_token,
                            httponly=True, secure=True, samesite="strict")
        return {"message": "Password changed successfully."}
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); logger.error(f"change-password error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/auth/forgot-password")
@limiter.limit("5/minute")
def forgot_password(request: Request, body: ForgotPasswordRequest):
    """Public: record a reset request for an admin to action. Always returns a generic
    message so it never reveals whether an email is registered."""
    email = body.email.lower()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s AND is_active = TRUE", (email,))
        row = cur.fetchone()
        if row:
            # Don't pile up duplicates — one pending request per user is enough.
            cur.execute("SELECT id FROM password_reset_request WHERE user_id = %s AND status = 'pending'",
                        (row["id"],))
            if not cur.fetchone():
                cur.execute("INSERT INTO password_reset_request (email, user_id, status) "
                            "VALUES (%s, %s, 'pending')", (email, row["id"]))
                write_audit_log(conn, row["id"], "FORGOT_PASSWORD_REQUEST", "users", row["id"],
                                f"Reset requested for {email}")
                conn.commit()
        return {"message": _GENERIC_FORGOT_MSG}
    except Exception as e:
        conn.rollback(); logger.error(f"forgot-password error: {e}")
        return {"message": _GENERIC_FORGOT_MSG}   # never leak errors to the client
    finally:
        release_db_connection(conn)


@app.get("/api/v1/auth/users")
def list_users(request: Request, authorization: Optional[str] = Header(None)):
    """Admin: active login accounts (for the reset dropdown). No password data."""
    payload = _require_auth(request, authorization)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute("SELECT email, full_name, role FROM users WHERE is_active = TRUE "
                    "ORDER BY role DESC, email")
        return {"users": [dict(r) for r in cur.fetchall()]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list-users error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/auth/reset-requests")
def list_reset_requests(request: Request, authorization: Optional[str] = Header(None)):
    """Admin: pending 'forgot password' requests."""
    payload = _require_auth(request, authorization)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute("""SELECT r.id, r.email, r.requested_at, u.full_name
                       FROM password_reset_request r LEFT JOIN users u ON u.id = r.user_id
                       WHERE r.status = 'pending' ORDER BY r.requested_at DESC""")
        return {"requests": [{"id": r["id"], "email": r["email"], "full_name": r["full_name"],
                              "requested_at": r["requested_at"].isoformat() if r["requested_at"] else None}
                             for r in cur.fetchall()]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"reset-requests error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/auth/admin-reset-password")
def admin_reset_password(request: Request, body: AdminResetPasswordRequest,
                         authorization: Optional[str] = Header(None)):
    """Admin: set a new password for any active user and resolve their pending requests."""
    payload = _require_auth(request, authorization)
    _validate_password_strength(body.new_password)
    target = body.email.lower()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        cur.execute("SELECT id FROM users WHERE email = %s AND is_active = TRUE", (target,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No active user with that email.")
        # Bump token_version too: a reset is often incident response, so every
        # outstanding session for the target (e.g. an attacker's) must die at once.
        cur.execute("UPDATE users SET password_hash = %s, failed_attempts = 0, locked_until = NULL, "
                    "token_version = token_version + 1 "
                    "WHERE id = %s", (_hash_password(body.new_password), row["id"]))
        cur.execute("UPDATE password_reset_request SET status = 'resolved', resolved_at = NOW(), "
                    "resolved_by = %s WHERE user_id = %s AND status = 'pending'",
                    (payload.get("user_id"), row["id"]))
        write_audit_log(conn, payload.get("user_id"), "ADMIN_RESET_PASSWORD", "users", row["id"],
                        f"Admin {payload['email']} reset password for {target}")
        conn.commit()
        return {"message": f"Password reset for {target}."}
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); logger.error(f"admin-reset-password error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


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
    """Get all entities — available to any authenticated user (drives the entity switcher)."""
    conn = None
    try:
        _require_auth(request, authorization)
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

def _assert_session_current(payload: dict) -> None:
    """
    Live server-side session check, run on every authenticated request. Rejects a
    token whose owner has since been deactivated (is_active=FALSE) or whose sessions
    were revoked (users.token_version bumped by a password change / admin reset).

    Without this a valid signed JWT stays usable until its 8-hour expiry even after
    the credential is rotated or the account disabled. Fails CLOSED: any lookup error
    denies the request rather than letting it through.

    Uses its own short-lived pooled connection and releases it before returning, so it
    does not raise peak concurrency (the endpoint acquires its connection afterwards).
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT is_active, token_version FROM users WHERE email = %s",
            (payload.get("email"),),
        )
        row = cur.fetchone()
        cur.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"session validation failed: {e}")
        raise HTTPException(status_code=401, detail="Could not validate session.")
    finally:
        release_db_connection(conn)

    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="Account is inactive. Please log in again.")
    if int(row["token_version"]) != int(payload.get("token_version", 0)):
        raise HTTPException(status_code=401, detail="Session has been revoked. Please log in again.")


def _require_auth(request: Request, authorization: Optional[str]) -> dict:
    """Validate token and return JWT payload. Raises 401 on failure."""
    token = get_token_from_request(request, authorization)
    if is_token_blacklisted(token):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    _assert_session_current(payload)
    return payload


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
    entity_id: Optional[List[int]] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """
    Return MF holdings for the requesting user's entity.
    Admin users may pass ?entity_id=N (repeatable) to view one entity or a subset.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cursor = conn.cursor()
        # Any authenticated user may view every entity (see _resolve_entities):
        # ?entity_id=N (repeatable) → that entity or subset; no param → all entities.
        eids = _resolve_entities(cursor, payload, entity_id)

        where  = ""
        params: list = []
        if eids:
            where = "WHERE h.entity_id = ANY(%s)"
            params.append(eids)

        cursor.execute(f"""
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
            {where}
            ORDER BY sm.asset_class, sm.security_name, h.folio_number
        """, params)
        rows = cursor.fetchall()
        cursor.close()

        # Realized gains are keyed by (entity_id, security_id, folio), so the
        # full-book cache serves every scope — filter happens at lookup.
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

        resp_entity_id, entity_name = _entity_label(eids, rows)
        return {
            "entity_id":       resp_entity_id,
            "entity_name":     entity_name,
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
    entity_id: Optional[List[int]] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """
    MF holdings merged by security across entities. Units summed, cost
    weighted-averaged, XIRR from pooled transactions. No ?entity_id → pool the
    whole book; ?entity_id=N (repeatable) → pool only that entity or subset.
    """
    from collections import OrderedDict, defaultdict
    from datetime import date as _date
    conn = None
    try:
        payload   = _require_auth(request, authorization)
        conn      = get_db_connection()
        cursor    = conn.cursor()
        eids      = _resolve_entities(cursor, payload, entity_id)

        hold_where = "WHERE h.entity_id = ANY(%s)" if eids else ""
        hold_params: list = [eids] if eids else []
        cursor.execute(f"""
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
            {hold_where}
            ORDER BY sm.asset_class, sm.security_name, e.entity_name
        """, hold_params)
        rows = cursor.fetchall()

        txn_where = "AND entity_id = ANY(%s)" if eids else ""
        cursor.execute(f"""
            SELECT security_id, transaction_date, amount, units
            FROM mf_transaction
            WHERE amount IS NOT NULL {txn_where}
            ORDER BY security_id, transaction_date
        """, ([eids] if eids else []))
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
    Returns the entity_id to query. Every authenticated user (admin AND member)
    may view all entities — the only member restrictions are the Manual Data page
    and user management, which are gated separately. So entity scoping is uniform:
      - entity_id_param given → that entity
      - no param              → None (all entities)
    (cursor/payload retained for signature compatibility with existing callers.)
    """
    return entity_id_param


def _resolve_entities(cursor, payload: dict, entity_ids: Optional[List[int]]) -> Optional[List[int]]:
    """
    Multi-entity variant of _resolve_entity. FastAPI parses a repeated
    ?entity_id=1&entity_id=5 query param into a list; a single ?entity_id=5 into
    [5]; an absent param into None. Returns a de-duped list, or None for
    "all entities". Every authenticated user may view any entity (see
    _resolve_entity), so no per-user gating here.
    """
    if not entity_ids:
        return None
    return list(dict.fromkeys(entity_ids))


def _entity_label(eids: Optional[List[int]], rows, name_key: str = "entity_name") -> tuple:
    """
    Derive the (entity_id, entity_name) pair a response advertises for a given
    entity scope. All entities → (0, "All Entities"); exactly one → that entity's
    id/name (from the first row when available); a subset → (0, "N entities").
    """
    if not eids:
        return 0, "All Entities"
    if len(eids) == 1:
        return eids[0], (rows[0][name_key] if rows else "")
    return 0, f"{len(eids)} entities"


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
        "pnl_daily":             _fmt(r.get("pnl_daily")),   # foreign only (None for India)
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
        return round(sum((r.get(key) or 0) for r in rows), 2)
    return {
        "total_cost":             s("cost"),
        "total_current_market_value": s("current_market_value"),
        "total_prev_week_value":  s("prev_week_value"),
        "total_weekly_change":    s("weekly_change"),
        "total_pnl_inception":    s("pnl_inception"),
        "total_pnl_daily":        s("pnl_daily"),   # foreign only; 0 where absent
        "total_pnl_ytd":          s("pnl_ytd"),
        "total_pnl_weekly_change":s("pnl_weekly_change"),
    }


@app.get("/api/v1/equity/holdings")
def get_equity_holdings(
    request: Request,
    entity_id: Optional[List[int]] = Query(None),
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
        eids = _resolve_entities(cur, payload, entity_id)

        # Build WHERE clause
        conditions = []
        params     = []
        # Gold/silver/commodity holdings moved to the dedicated Gold/Silver page
        # (the 2026-06-26 split) — keep the Equity page to actual equity.
        conditions.append(
            "COALESCE(eh.asset_class, 'equity') NOT IN ('gold','silver','commodity')")
        if eids:
            conditions.append("eh.entity_id = ANY(%s)")
            params.append(eids)
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
        # Indian brokers only — foreign broker cash (IBKR/Vested/DBS) lives on the
        # Foreign Equity page, so it doesn't double-count here.
        cash_conditions = ["bc.broker <> ALL(%s)"]
        cash_params: list = [list(FOREIGN_BROKERS)]
        if eids:
            cash_conditions.append("bc.entity_id = ANY(%s)")
            cash_params.append(eids)
        if broker:
            cash_conditions.append("bc.broker = %s")
            cash_params.append(broker)
        cash_where = "WHERE " + " AND ".join(cash_conditions)
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
        if not broker and eids and len(eids) == 1:
            cur.execute(
                """SELECT xirr_pct, income_inr, coverage FROM portfolio_returns
                   WHERE entity_id = %s ORDER BY as_of_date DESC LIMIT 1""",
                (eids[0],),
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

        resp_entity_id, entity_name = _entity_label(eids, rows)

        return {
            "entity_id":      resp_entity_id,
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


@app.get("/api/v1/equity/activity")
def get_equity_activity(
    request: Request,
    entity_id: Optional[List[int]] = Query(None),
    day: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """Equity trades detected today by the intraday snapshot worker (source='snapshot').

    Powers the Equity page's "Traded today" panel. Buys/sells are diffed from hourly
    position snapshots, so the price is the snapshot LTP at detection, not the exact
    fill. Realised P&L on a sell is qty × (sale price − latest snapshot avg cost).

    Admin: optional ?entity_id=N (default all). Member: own entity only.
    ?day=YYYY-MM-DD overrides today (defaults to the current date).
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        eids = _resolve_entities(cur, payload, entity_id)

        try:
            as_of = date.fromisoformat(day) if day else date.today()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid day (expected YYYY-MM-DD).")

        conditions = ["st.source = 'snapshot'", "st.transaction_date = %s"]
        params     = [as_of]
        if eids:
            conditions.append("st.entity_id = ANY(%s)")
            params.append(eids)
        where = " AND ".join(conditions)

        # avg_cost comes from the most recent position snapshot for the security
        # (matched by ISIN when present, else by trading symbol == security_name).
        cur.execute(
            f"""
            SELECT st.entity_id, e.entity_name, sm.security_name, sm.isin,
                   st.transaction_type, st.quantity, st.price, st.amount,
                   st.exchange, st.created_at, ac.avg_cost
            FROM   stock_transaction st
            JOIN   entity e ON e.id = st.entity_id
            JOIN   security_master sm ON sm.id = st.security_id
            LEFT JOIN LATERAL (
                SELECT ps.avg_cost
                FROM   equity_position_snapshot ps
                WHERE  ps.entity_id = st.entity_id
                  AND  (ps.isin = sm.isin OR ps.symbol = sm.security_name)
                ORDER  BY ps.captured_at DESC
                LIMIT  1
            ) ac ON TRUE
            WHERE  {where}
            ORDER  BY st.created_at DESC, e.entity_name, sm.security_name
            """,
            params,
        )
        rows = cur.fetchall()
        cur.close()

        trades = []
        buy_count = sell_count = 0
        realized_total = 0.0
        for r in rows:
            side  = (r["transaction_type"] or "").upper()
            qty   = float(r["quantity"] or 0)
            price = float(r["price"] or 0)
            avg   = float(r["avg_cost"]) if r["avg_cost"] is not None else None
            pnl   = None
            if side == "SELL" and avg is not None:
                pnl = round(qty * (price - avg), 2)
                realized_total += pnl
            if side == "BUY":
                buy_count += 1
            elif side == "SELL":
                sell_count += 1
            trades.append({
                "entity_id":     r["entity_id"],
                "entity_name":   r["entity_name"],
                "security_name": r["security_name"],
                "isin":          r["isin"],
                "side":          side,
                "quantity":      qty,
                "price":         price,
                "amount":        float(r["amount"] or 0),
                "avg_cost":      round(avg, 4) if avg is not None else None,
                "realized_pnl":  pnl,
                "exchange":      r["exchange"],
                "detected_at":   r["created_at"].isoformat() if r["created_at"] else None,
            })

        return {
            "date":               str(as_of),
            "entity_id":          (eids[0] if eids and len(eids) == 1 else 0),
            "buy_count":          buy_count,
            "sell_count":         sell_count,
            "realized_pnl_total": round(realized_total, 2),
            "trades":             trades,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/equity/activity: {e}")
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
    entity_id: Optional[List[int]] = Query(None),
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
        eids = _resolve_entities(cur, payload, entity_id)

        conditions, params = [], []
        # Gold/silver/commodity (e.g. IBKR uranium) moved to the dedicated
        # Gold/Silver page (2026-06-26 split) — exclude from Foreign Equity too.
        conditions.append(
            "COALESCE(eh.asset_class, 'equity') NOT IN ('gold','silver','commodity')")
        if eids:
            conditions.append("eh.entity_id = ANY(%s)")
            params.append(eids)
        if broker:
            conditions.append("eh.broker = %s")
            params.append(broker)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(
            f"""
            SELECT {_EQUITY_HOLDING_COLS}, eh.pnl_daily
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
        if eids:
            cash_conditions.append("bc.entity_id = ANY(%s)")
            cash_params.append(eids)
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

        # Per-currency breakdown behind each broker's consolidated cash (e.g. SDR's IBKR
        # cash split into AED / GBP margin / USD). Additive detail — the totals above still
        # come from the single broker_cash row, so nothing double-counts.
        cur.execute(
            f"""
            SELECT d.entity_id, e.entity_name, d.broker, d.currency,
                   d.balance_native, d.balance_inr, d.fx_rate, d.updated_at
            FROM   broker_cash_currency d
            JOIN   entity e ON e.id = d.entity_id
            WHERE  {" AND ".join(c.replace("bc.", "d.") for c in cash_conditions)}
            ORDER BY e.entity_name, d.broker, ABS(d.balance_inr) DESC
            """,
            cash_params,
        )
        ccy_rows  = cur.fetchall()
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

        resp_entity_id, entity_name = _entity_label(eids, rows)

        return {
            "entity_id":      resp_entity_id,
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
            "cash_currency_breakdown": [
                {
                    "entity_id":      c["entity_id"],
                    "entity_name":    c["entity_name"],
                    "broker":         c["broker"],
                    "currency":       c["currency"],
                    "balance_native": float(c["balance_native"]) if c["balance_native"] is not None else None,
                    "balance":        float(c["balance_inr"] or 0),
                    "fx_rate":        float(c["fx_rate"]) if c["fx_rate"] is not None else None,
                    "updated_at":     c["updated_at"].isoformat() if c["updated_at"] else None,
                }
                for c in ccy_rows
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/foreign-equity/holdings: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/foreign-equity/activity")
def get_foreign_equity_activity(
    request: Request,
    entity_id: Optional[List[int]] = Query(None),
    day: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """Foreign-equity trades booked today, from equity_trade_ledger.

    Two sources feed this: IBKR's exact Flex fills (source='ibkr_flex') and Vested
    positions diffed by the foreign snapshot worker (source='snapshot'). Native
    amounts are converted to INR at the trade-date FX; realised P&L on sells reuses
    the Foreign-Equity realised-gains engine (avg cost on native flows).

    Admin: optional ?entity_id=N (default all). Member: own entity only.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        eids = _resolve_entities(cur, payload, entity_id)

        try:
            as_of = date.fromisoformat(day) if day else date.today()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid day (expected YYYY-MM-DD).")

        # snapshot_open rows are opening cost-basis seeds, not trades — never list them.
        conditions = ["etl.trade_date = %s", "etl.source <> 'snapshot_open'"]
        params     = [as_of]
        if eids:
            conditions.append("etl.entity_id = ANY(%s)")
            params.append(eids)
        where = " AND ".join(conditions)
        cur.execute(
            f"""
            SELECT etl.entity_id, e.entity_name, etl.broker, etl.symbol, etl.side,
                   etl.quantity, etl.price_native, etl.currency, etl.cash_flow_native,
                   etl.source
            FROM   equity_trade_ledger etl
            JOIN   entity e ON e.id = etl.entity_id
            WHERE  {where}
            ORDER  BY etl.broker, etl.symbol
            """,
            params,
        )
        rows = cur.fetchall()

        # Which entities are in scope for the realised-gains recompute.
        if eids:
            entity_ids = list(eids)
        else:
            cur.execute("SELECT id FROM entity ORDER BY id")
            entity_ids = [r["id"] for r in cur.fetchall()]

        from workers.report_generator import _fetch_realised_gains, _fx_rate_on
        pnl_by_symbol: dict = {}
        realized_total = 0.0
        for r in _fetch_realised_gains(conn, entity_ids, as_of, since_inception=True):
            if r.get("category") == "Foreign Equity" and str(r.get("sale_date")) == str(as_of):
                p = float(r.get("pnl") or 0)
                pnl_by_symbol[r["security_name"]] = pnl_by_symbol.get(r["security_name"], 0.0) + p
                realized_total += p

        fx_cache: dict = {}
        def _fx(ccy: str) -> float:
            if ccy not in fx_cache:
                fx_cache[ccy] = _fx_rate_on(conn, ccy, as_of) or 0.0
            return fx_cache[ccy]

        trades = []
        buy_count = sell_count = 0
        for r in rows:
            side  = (r["side"] or "").upper()
            qty   = float(r["quantity"] or 0)
            pnat  = float(r["price_native"] or 0)
            ccy   = (r["currency"] or "USD").upper()
            fx    = _fx(ccy)
            val_native = abs(float(r["cash_flow_native"] or 0))
            if side == "BUY":
                buy_count += 1
            elif side == "SELL":
                sell_count += 1
            trades.append({
                "entity_id":     r["entity_id"],
                "entity_name":   r["entity_name"],
                "broker":        r["broker"],
                "security_name": r["symbol"],
                "side":          side,
                "quantity":      qty,
                "price_native":  pnat,
                "currency":      ccy,
                "value_native":  round(val_native, 2),
                "value_inr":     round(val_native * fx, 2) if fx else None,
                "realized_pnl":  (round(pnl_by_symbol.get(r["symbol"]), 2)
                                  if side == "SELL" and r["symbol"] in pnl_by_symbol else None),
                "source":        r["source"],
            })
        cur.close()

        return {
            "date":               str(as_of),
            "entity_id":          (eids[0] if eids and len(eids) == 1 else 0),
            "buy_count":          buy_count,
            "sell_count":         sell_count,
            "realized_pnl_total": round(realized_total, 2),
            "trades":             trades,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/foreign-equity/activity: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# DBS Wealth — no API/scrape; the entity's holdings statement is uploaded as a
# CSV (weekly). Parse-then-confirm: /preview parses + returns the extracted rows
# for admin review (no DB write); /commit snapshot-replaces foreign_equity_holding
# (broker='dbs') for that entity. Live US names then price-refresh via
# foreign_price_worker; SGX/other unresolvable names keep the statement value.
# ---------------------------------------------------------------------------

MAX_DBS_BYTES = 5 * 1024 * 1024   # 5 MB — a holdings CSV is a few KB


def _dbs_save_and_parse(entity_id: int, file: UploadFile, data: bytes):
    """Persist the upload under UPLOADS_ROOT and parse it. Returns (stored_path, parsed).
    UPLOADS_ROOT is defined later in this module, so the dir is resolved lazily here."""
    from equity import dbs_statement
    if not dbs_statement.detect(file.filename or "", data[:400].decode("utf-8", "ignore")):
        raise HTTPException(status_code=422,
                            detail="Doesn't look like a DBS holdings CSV (no 'Asset Type' header).")
    folder = os.path.join(UPLOADS_ROOT, "foreign", "dbs", str(entity_id))
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(file.filename or "dbs.csv"))
    stored = os.path.join(folder, f"{datetime.utcnow():%Y%m%d%H%M%S}_{safe}")
    with open(stored, "wb") as fh:
        fh.write(data)
    os.chmod(stored, 0o600)
    try:
        parsed = dbs_statement.parse(stored)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return stored, parsed


def _dbs_preview_payload(parsed: dict) -> dict:
    """Shape a parsed statement for the confirm UI (native figures + resolvable flag)."""
    def f(v):
        return float(v) if v is not None else None
    return {
        "account": parsed.get("account"),
        "as_of":   str(parsed["as_of"]) if parsed.get("as_of") else None,
        "note":    parsed.get("note"),
        "holdings": [{
            "name": h["name"], "symbol": h["symbol"], "isin": h.get("isin"),
            "exchange": h.get("exchange"), "currency": h["currency"],
            "quantity": f(h["quantity"]), "avg_cost_native": f(h.get("avg_cost_native")),
            "price_native": f(h.get("price_native")),
            "market_value_native": f(h.get("market_value_native")),
            "resolvable": h.get("resolvable", False),
        } for h in parsed.get("holdings", [])],
        "cash": [{"currency": c["currency"],
                  "market_value_native": f(c["market_value_native"])} for c in parsed.get("cash", [])],
    }


@app.post("/api/v1/foreign-equity/dbs/preview")
@limiter.limit("20/minute")
async def dbs_preview(
    request: Request,
    entity_id: int = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Parse an uploaded DBS holdings CSV and return the extracted rows for review.
    Does NOT touch foreign_equity_holding — that happens on /commit. Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("SELECT entity_name FROM entity WHERE id = %s", (entity_id,))
        ent = cur.fetchone()
        if not ent:
            raise HTTPException(status_code=404, detail="Entity not found")

        data = await file.read(MAX_DBS_BYTES + 1)
        if len(data) == 0:
            raise HTTPException(status_code=422, detail="Uploaded file is empty.")
        if len(data) > MAX_DBS_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 5 MB).")

        _stored, parsed = _dbs_save_and_parse(entity_id, file, data)
        cur.close()
        return {"entity_id": entity_id, "entity_name": ent["entity_name"],
                "committed": False, **_dbs_preview_payload(parsed)}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/foreign-equity/dbs/preview: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/foreign-equity/dbs/commit")
@limiter.limit("20/minute")
async def dbs_commit(
    request: Request,
    entity_id: int = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Snapshot-replace the entity's DBS holdings from an uploaded CSV. Anything
    absent from this file is treated as exited. Admin only."""
    from equity import dbs_ingest
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("SELECT entity_name FROM entity WHERE id = %s", (entity_id,))
        ent = cur.fetchone()
        if not ent:
            raise HTTPException(status_code=404, detail="Entity not found")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        user_id = cur.fetchone()["id"]

        data = await file.read(MAX_DBS_BYTES + 1)
        if len(data) == 0:
            raise HTTPException(status_code=422, detail="Uploaded file is empty.")
        if len(data) > MAX_DBS_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 5 MB).")

        _stored, parsed = _dbs_save_and_parse(entity_id, file, data)
        summary = dbs_ingest.ingest(conn, entity_id, parsed, commit=True)
        write_audit_log(conn, user_id, "DBS_HOLDINGS_UPLOAD", "foreign_equity_holding", entity_id,
                        f"{ent['entity_name']} DBS: replaced {summary['replaced']} → "
                        f"{summary['inserted']} rows (as of {summary['as_of']}) by {payload['email']}")
        conn.commit()
        cur.close()
        return {"entity_id": entity_id, "entity_name": ent["entity_name"],
                "committed": True, **summary, **_dbs_preview_payload(parsed)}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/foreign-equity/dbs/commit: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# FnO — open derivative positions scraped from the FnO broker portals
# (Share India uTrade → HHR now; Orbis → DHR later). Positions live in
# fno_position, per-account margin/P&L summaries in fno_account, both fed by
# workers/shareindia_fno_worker.py.
# ---------------------------------------------------------------------------

FNO_SOURCE_LABEL = {"shareindia": "Share India", "orbis": "Orbis"}


@app.get("/api/v1/fno/positions")
def get_fno_positions(
    request: Request,
    entity_id: Optional[List[int]] = Query(None),
    source: Optional[str] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Open FnO positions (net quantity; negative = short) plus per-account
    margin / MTM summaries. All figures are INR. Admin sees all entities
    (or ?entity_id=N); a member sees only their entity.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eids = _resolve_entities(cur, payload, entity_id)

        conds, params = [], []
        if eids:
            conds.append("p.entity_id = ANY(%s)")
            params.append(eids)
        if source:
            conds.append("p.source = %s")
            params.append(source)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""

        cur.execute(
            f"""
            SELECT p.entity_id, e.entity_name, p.source, p.symbol, p.underlying,
                   p.instrument, p.expiry, p.strike, p.product, p.quantity,
                   p.lot_size, p.avg_price, p.ltp, p.mtm_pnl, p.realized_pnl,
                   p.as_of_date, p.updated_at
            FROM   fno_position p
            JOIN   entity e ON e.id = p.entity_id
            {where}
            ORDER BY e.entity_name, p.source, p.underlying NULLS LAST, p.expiry NULLS LAST, p.symbol
            """,
            params,
        )
        rows = cur.fetchall()

        acct_where = where.replace("p.entity_id", "a.entity_id").replace("p.source", "a.source")
        cur.execute(
            f"""
            SELECT a.entity_id, e.entity_name, a.source, a.margin_available,
                   a.margin_used, a.ledger_balance, a.day_realized_pnl,
                   a.total_mtm_pnl, a.as_of_date, a.updated_at
            FROM   fno_account a
            JOIN   entity e ON e.id = a.entity_id
            {acct_where}
            ORDER BY e.entity_name, a.source
            """,
            params,
        )
        acct_rows = cur.fetchall()
        cur.close()

        def _f(v):
            return float(v) if v is not None else None

        positions = [
            {
                "entity_id":    r["entity_id"],
                "entity_name":  r["entity_name"],
                "source":       r["source"],
                "source_label": FNO_SOURCE_LABEL.get(r["source"], r["source"]),
                "symbol":       r["symbol"],
                "underlying":   r["underlying"],
                "instrument":   r["instrument"],
                "expiry":       str(r["expiry"]) if r["expiry"] else None,
                "strike":       _f(r["strike"]),
                "product":      r["product"] or None,
                "quantity":     _f(r["quantity"]) or 0.0,
                "lot_size":     r["lot_size"],
                "avg_price":    _f(r["avg_price"]),
                "ltp":          _f(r["ltp"]),
                "mtm_pnl":      _f(r["mtm_pnl"]),
                "realized_pnl": _f(r["realized_pnl"]),
            }
            for r in rows
        ]
        accounts = [
            {
                "entity_id":        a["entity_id"],
                "entity_name":      a["entity_name"],
                "source":           a["source"],
                "source_label":     FNO_SOURCE_LABEL.get(a["source"], a["source"]),
                "margin_available": _f(a["margin_available"]),
                "margin_used":      _f(a["margin_used"]),
                "ledger_balance":   _f(a["ledger_balance"]),
                "day_realized_pnl": _f(a["day_realized_pnl"]),
                "total_mtm_pnl":    _f(a["total_mtm_pnl"]),
                "as_of_date":       str(a["as_of_date"]) if a["as_of_date"] else None,
            }
            for a in acct_rows
        ]

        totals = {
            "position_count": len(positions),
            "mtm_pnl":        round(sum(p["mtm_pnl"] or 0 for p in positions), 2),
            "realized_pnl":   round(sum(p["realized_pnl"] or 0 for p in positions), 2),
            "margin_used":      round(sum(a["margin_used"] or 0 for a in accounts), 2),
            "margin_available": round(sum(a["margin_available"] or 0 for a in accounts), 2),
        }

        as_of_dates = [r["as_of_date"] for r in rows if r["as_of_date"]]
        updated_ats = [r["updated_at"] for r in rows if r["updated_at"]] + \
                      [a["updated_at"] for a in acct_rows if a["updated_at"]]

        if not eids:
            entity_name = "All Entities"
        elif len(eids) == 1:
            entity_name = rows[0]["entity_name"] if rows else (acct_rows[0]["entity_name"] if acct_rows else "")
        else:
            entity_name = f"{len(eids)} entities"

        return {
            "entity_id":    (eids[0] if eids and len(eids) == 1 else 0),
            "entity_name":  entity_name,
            "source":       source,
            "positions":    positions,
            "accounts":     accounts,
            "totals":       totals,
            "as_of_date":   str(max(as_of_dates)) if as_of_dates else None,
            "last_updated": max(updated_ats).isoformat() if updated_ats else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/v1/fno/positions: {e}")
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
    entity_id: Optional[List[int]] = Query(None),
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
        eids = _resolve_entities(cur, payload, entity_id)

        conds  = ["COALESCE(eh.asset_class, 'equity') IN ('gold','silver','commodity')"]
        params: list = []
        if eids:
            conds.append("eh.entity_id = ANY(%s)")
            params.append(eids)
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

        resp_entity_id, entity_name = _entity_label(eids, rows)

        return {
            "entity_id":         resp_entity_id,
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
# PMS holdings — segmented by source (Nuvama / Zerodha / ICICI Pru), with
# equity / cash / combined totals and P&L metrics at every level
# ---------------------------------------------------------------------------

# Display names for pms_holding.source values.
PMS_SOURCE_LABELS = {
    "nuvama_pms":  "Nuvama",
    "zerodha_pms": "Zerodha",
    "icici_pms":   "ICICI Prudential",
}

# PMS sources whose funding deposits/withdrawals flow through a broker ledger
# already imported into external_cashflow, keyed to that ledger's broker code.
# zerodha_pms runs INSIDE the client's own Zerodha account, so its deposits ARE
# the Zerodha ledger flows — a true money-weighted XIRR is computable. Nuvama
# and ICICI are separately managed accounts whose deposit history isn't
# ingested, so they only get absolute return (same rule as equity: annualised
# metrics only where the dated flows exist).
PMS_SOURCE_FLOW_BROKER = {"zerodha_pms": "zerodha"}

# User-supplied total capital deposited into a PMS whose provider doesn't
# report per-holding cost (ICICI Pru gives market value only). Keyed
# "<entity_name>/<source>". The account's cash is the uninvested part of the
# deposit, so equity cost = deposit − cash and net return is measured on that.
try:
    PMS_TOTAL_DEPOSITS = {k: float(v) for k, v in
                          json.loads(os.getenv("PMS_TOTAL_DEPOSITS", "{}")).items()}
except (ValueError, TypeError) as _e:
    logger.error(f"PMS_TOTAL_DEPOSITS is not valid JSON — ignoring: {_e}")
    PMS_TOTAL_DEPOSITS = {}


def _pms_aggregate(holdings: list[dict]) -> dict:
    """Totals + P&L metrics over a group of pms_holding rows.

    P&L / return are computed ONLY over equity holdings that carry a cost
    (ICICI Pru reports market value but no cost basis); `cost_complete` tells
    the UI whether the metrics cover the whole group or a costed subset.
    Counting a cost-less holding's full market value as profit — what the old
    single-total endpoint did — is exactly the distortion this avoids.
    """
    equity = [h for h in holdings if h["holding_type"] == "equity"]
    cash   = [h for h in holdings if h["holding_type"] == "cash"]

    equity_total = sum(h["market_value"] for h in equity)
    cash_total   = sum(h["market_value"] for h in cash)

    costed       = [h for h in equity if h["cost"] is not None]
    equity_cost  = sum(h["cost"] for h in costed)
    costed_value = sum(h["market_value"] for h in costed)
    pnl          = round(costed_value - equity_cost, 2) if costed else None
    returns_pct  = round((costed_value - equity_cost) / equity_cost * 100, 2) if equity_cost > 0 else None

    return {
        "equity_total":  round(equity_total, 2),
        "cash_total":    round(cash_total, 2),
        "total":         round(equity_total + cash_total, 2),
        "equity_cost":   round(equity_cost, 2),
        # Capital put in = cost of equity holdings + cash parked in the account
        # (cash is uninvested principal, so it counts toward invested).
        "invested_cost": round(equity_cost + cash_total, 2),
        "equity_pnl":    pnl,
        "returns_pct":   returns_pct,
        "cost_complete": len(costed) == len(equity),
        "equity_count":  len(equity),
        "cash_count":    len(cash),
    }


def _apply_deposit_override(agg: dict, entity_name: str, source: str) -> dict:
    """Replace a source aggregate's cost basis with the user-supplied total
    deposit when the provider reports no per-holding cost. Cash is the
    uninvested part of the deposit, so equity cost = deposit − cash."""
    deposit = PMS_TOTAL_DEPOSITS.get(f"{entity_name}/{source}")
    if deposit is None or agg["cost_complete"]:
        return {**agg, "deposit_total": None}
    equity_cost = max(deposit - agg["cash_total"], 0.0)
    pnl         = round(agg["equity_total"] - equity_cost, 2)
    return {
        **agg,
        "equity_cost":   round(equity_cost, 2),
        "invested_cost": round(deposit, 2),
        "equity_pnl":    pnl,
        "returns_pct":   round(pnl / equity_cost * 100, 2) if equity_cost > 0 else None,
        "cost_complete": True,
        "deposit_total": round(deposit, 2),
    }


def _pms_combine(aggs: list[dict]) -> dict:
    """Roll source-level aggregates up to an entity or the grand total, so
    deposit-derived cost bases flow into the higher levels consistently.
    P&L / return cover only the sources that have a cost basis."""
    pnls = [a["equity_pnl"] for a in aggs if a["equity_pnl"] is not None]
    pnl  = round(sum(pnls), 2) if pnls else None
    cost = sum(a["equity_cost"] for a in aggs)
    return {
        "equity_total":  round(sum(a["equity_total"] for a in aggs), 2),
        "cash_total":    round(sum(a["cash_total"] for a in aggs), 2),
        "total":         round(sum(a["total"] for a in aggs), 2),
        "equity_cost":   round(cost, 2),
        "invested_cost": round(sum(a["invested_cost"] for a in aggs), 2),
        "equity_pnl":    pnl,
        "returns_pct":   round(pnl / cost * 100, 2) if pnl is not None and cost > 0 else None,
        "cost_complete": all(a["cost_complete"] for a in aggs),
        "equity_count":  sum(a["equity_count"] for a in aggs),
        "cash_count":    sum(a["cash_count"] for a in aggs),
    }


def _pms_source_xirr(cur, entity_id: int, source: str, current_value: float) -> Optional[float]:
    """Money-weighted return (%) for one (entity, PMS source), from the broker
    ledger flows in external_cashflow plus the current value as final inflow.
    None when the source has no ledger mapping, no flows, or <1yr of history
    (annualising shorter periods exaggerates the rate)."""
    broker = PMS_SOURCE_FLOW_BROKER.get(source)
    if broker is None or current_value <= 0:
        return None
    cur.execute(
        """SELECT flow_date, amount_native, currency FROM external_cashflow
           WHERE entity_id = %s AND broker = %s ORDER BY flow_date""",
        (entity_id, broker),
    )
    rows = cur.fetchall()
    if not rows or (date.today() - rows[0]["flow_date"]).days < 365:
        return None
    # Flows are stored investor-signed (deposits negative, withdrawals/income
    # positive); INR is the only ledger currency for Indian brokers.
    flows = [(r["flow_date"], float(r["amount_native"])) for r in rows]
    flows.append((date.today(), current_value))
    rate = _xirr(flows)
    return round(rate * 100, 2) if rate is not None else None


@app.get("/api/v1/pms/holdings")
def get_pms_holdings(
    request: Request,
    entity_id: Optional[List[int]] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """
    All PMS holdings segmented by source (Nuvama WealthSpectrum, Zerodha PMS,
    ICICI Prudential PMS), with totals + P&L metrics overall, per entity, and
    per (entity, source) — plus XIRR where the source's ledger flows exist.
    Admin: optional ?entity_id=N to filter. Member: own entity only.
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cur  = conn.cursor()
        eids = _resolve_entities(cur, payload, entity_id)

        conditions = []
        params     = []
        if eids:
            conditions.append("p.entity_id = ANY(%s)")
            params.append(eids)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(
            f"""
            SELECT p.entity_id, e.entity_name, p.source, p.holding_type,
                   p.security_name, p.isin, p.quantity, p.avg_cost, p.cost,
                   p.current_price, p.market_value, p.weight_pct, p.as_on_date
            FROM   pms_holding p
            JOIN   entity e ON e.id = p.entity_id
            {where}
            ORDER BY e.entity_name, p.source, p.holding_type, p.market_value DESC
            """,
            params,
        )
        rows = cur.fetchall()

        def _f(v):
            return float(v) if v is not None else None

        holdings = []
        for r in rows:
            cost = _f(r["cost"])
            mv   = _f(r["market_value"]) or 0.0
            has_pnl = cost is not None and r["holding_type"] == "equity"
            holdings.append({
                "entity_id":     r["entity_id"],
                "entity_name":   r["entity_name"],
                "source":        r["source"],
                "source_label":  PMS_SOURCE_LABELS.get(r["source"], r["source"]),
                "holding_type":  r["holding_type"],
                "security_name": r["security_name"],
                "isin":          r["isin"],
                "quantity":      _f(r["quantity"]),
                "avg_cost":      _f(r["avg_cost"]),
                "cost":          cost,
                "current_price": _f(r["current_price"]),
                "market_value":  mv,
                "weight_pct":    _f(r["weight_pct"]),
                "pnl":           round(mv - cost, 2) if has_pnl else None,
                "returns_pct":   round((mv - cost) / cost * 100, 2) if has_pnl and cost > 0 else None,
            })

        # Per-(entity, source) sections — one PMS account each.
        source_keys: list[tuple[int, str]] = []
        source_groups: dict[tuple[int, str], list[dict]] = {}
        for h in holdings:
            key = (h["entity_id"], h["source"])
            if key not in source_groups:
                source_groups[key] = []
                source_keys.append(key)
            source_groups[key].append(h)

        as_on_by_key = {}
        for r in rows:
            key = (r["entity_id"], r["source"])
            if r["as_on_date"] and (key not in as_on_by_key or r["as_on_date"] > as_on_by_key[key]):
                as_on_by_key[key] = r["as_on_date"]

        by_source = []
        source_aggs: dict[tuple[int, str], dict] = {}
        for key in source_keys:
            group = source_groups[key]
            agg   = _apply_deposit_override(_pms_aggregate(group), group[0]["entity_name"], key[1])
            source_aggs[key] = agg
            by_source.append({
                "entity_id":    key[0],
                "entity_name":  group[0]["entity_name"],
                "source":       key[1],
                "source_label": PMS_SOURCE_LABELS.get(key[1], key[1]),
                "as_on_date":   as_on_by_key.get(key).isoformat() if as_on_by_key.get(key) else None,
                "xirr_pct":     _pms_source_xirr(cur, key[0], key[1], agg["total"]),
                **agg,
            })

        # Per-entity rollup (an entity can hold several PMS accounts) — combined
        # from the source aggregates so deposit-derived cost bases carry through.
        entity_keys: list[int] = []
        for key in source_keys:
            if key[0] not in entity_keys:
                entity_keys.append(key[0])

        by_entity = []
        for ek in entity_keys:
            ek_aggs = [source_aggs[k] for k in source_keys if k[0] == ek]
            by_entity.append({
                "entity_id":   ek,
                "entity_name": next(g[0]["entity_name"] for k, g in source_groups.items() if k[0] == ek),
                "pms_count":   len(ek_aggs),
                **_pms_combine(ek_aggs),
            })

        cur.close()

        resp_entity_id, entity_name = _entity_label(eids, rows)
        as_on = max(as_on_by_key.values()).isoformat() if as_on_by_key else None

        return {
            "entity_id":   resp_entity_id,
            "entity_name": entity_name,
            "as_on_date":  as_on,
            "totals":      _pms_combine(list(source_aggs.values())),
            "by_entity":   by_entity,
            "by_source":   by_source,
            "holdings":    holdings,
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
    "collectibles":    "ART",   # same overview bucket as art — only the page split
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
    # Collectibles are deliberately kept OUT of the portfolio overview (owner
    # decision 2026-07-16) — tracked on their own page but not folded into
    # portfolio totals, same treatment as the property register.
    conds  = ["m.category <> 'collectibles'"]
    params: list = []
    if entity_id:
        conds.append("m.entity_id = %s")
        params.append(entity_id)
    where = "WHERE " + " AND ".join(conds)
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


def _fetch_property_overview_rows(conn, entity_id: Optional[int] = None):
    """
    Property register fair values (area x RRR x 1.75, computed like
    /api/v1/properties) folded into the REAL_ESTATE bucket. Holders are
    property_entity rows, not system entities: where a holder mirrors a system
    entity (same name) its rows carry that entity id so member scoping and the
    per-entity bars line up; other holders (LLPs, trusts, parent companies)
    appear as their own synthetic entities with a negative id — the dashboard
    only uses entity_id as a list key. Value per property: sale_price when
    sold (the asset became realised proceeds), else the hand-entered
    market_value when present, else area x RRR x 1.75. Joint ownership splits
    a property's value/cost across its owners by their pct. purchase_price,
    when recorded, feeds invested + an unrealised pnl.
    """
    mult  = property_docs.FAIR_VALUE_MULTIPLIER
    share = OLD_LEASE_OWNER_SHARE
    # Effective value per property: sale_price when sold; else (market OR land
    # RRR fair value) + summed floor costings, halved for old statutory leases.
    val_expr = (
        f"CASE WHEN p.sold THEN p.sale_price ELSE "
        f"(COALESCE(p.market_value, p.area * p.rrr * {mult}) + COALESCE(fv.bval, 0)) "
        f"* CASE WHEN p.is_old_lease THEN {share} ELSE 1 END END"
    )
    cur = conn.cursor()
    cur.execute(f"""
        WITH fv AS (
            SELECT property_id,
                   SUM(COALESCE(built_up_area, area) * rate_per_unit) AS bval
            FROM property_floor WHERE rate_per_unit IS NOT NULL
            GROUP BY property_id
        )
        SELECT pe.id AS holder_id, pe.name AS holder_name, e.id AS sys_entity_id,
               SUM(COALESCE({val_expr}, 0) * o.pct / 100)                AS value,
               SUM(COALESCE(p.purchase_price, 0) * o.pct / 100)          AS invested,
               SUM(CASE WHEN p.purchase_price IS NOT NULL THEN
                        (COALESCE({val_expr}, 0) - p.purchase_price) * o.pct / 100
                        ELSE 0 END)                                      AS pnl
        FROM property p
        JOIN property_owner o  ON o.property_id = p.id
        JOIN property_entity pe ON pe.id = o.holder_id
        LEFT JOIN entity e ON e.entity_name = pe.name
        LEFT JOIN fv ON fv.property_id = p.id
        GROUP BY pe.id, pe.name, e.id
    """)
    rows = cur.fetchall()
    cur.close()

    out = []
    for r in rows:
        sys_id = r["sys_entity_id"]
        if entity_id is not None and sys_id != entity_id:
            continue   # member scope: only properties held by their own entity
        val = float(r["value"]) if r["value"] is not None else 0.0
        if val <= 0:
            continue
        out.append({
            "entity_id":          sys_id if sys_id is not None else -r["holder_id"],
            "entity_name":        r["holder_name"],
            "asset_class":        "REAL_ESTATE",
            "security_type":      "PROPERTY",
            "invested":           float(r["invested"] or 0),
            "mkt_value":          val,
            "pnl_inception":      float(r["pnl"] or 0),
            "pnl_ytd":            0.0,
            "weekly_change":      0.0,
            "cagr_inception_pct": None,
            "weight":             val,
        })
    return out


@app.get("/api/v1/overview")
def get_overview(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """
    Aggregate portfolio overview across all asset classes.
    - Admin  → all entities (per-entity breakdown).
    - Member → scoped to their own entity only (a member never sees other entities).
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
        # Admin → None (all entities); member → their own entity_id. This makes the
        # Overview available to individual-entity logins, scoped to just themselves.
        eid = _resolve_entity(cursor, payload, None)

        mf_where  = "WHERE h.entity_id = %s"  if eid is not None else ""
        mf_params = [eid] if eid is not None else []
        cursor.execute(f"""
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
            {mf_where}
        """, mf_params)
        mf_rows = cursor.fetchall()

        eq_where  = "WHERE eh.entity_id = %s" if eid is not None else ""
        eq_params = ([eid, eid] if eid is not None else [])
        cursor.execute(f"""
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
            {eq_where}
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
            {eq_where}
        """, eq_params)
        eq_rows = cursor.fetchall()
        cursor.close()

        # Manual inputs (PPF, PMS/AIF, unlisted equity, startups, overseas,
        # cash balances, …) folded into the same asset-class buckets so the
        # dashboard portfolio matches the generated reports.
        manual_rows = _fetch_manual_overview_rows(conn, eid)

        # Nuvama PMS holdings (equity → EQUITY bucket, cash → CASH) so the
        # dashboard totals and allocation include the PMS portfolio.
        pms_rows = _fetch_pms_overview_rows(conn, eid)

        # Broker-account cash (Zerodha / Angel One / Dhan) → CASH bucket.
        broker_cash_rows = _fetch_broker_cash_overview_rows(conn, eid)

        # Bank-account cash (HSBC / DBS / FAB / …), native ccy → INR → CASH bucket.
        bank_cash_rows = _fetch_bank_cash_overview_rows(conn, eid)

        # Property register values are deliberately NOT folded in here — the
        # register is a standalone sheet and its fair values stay off the
        # overview totals (owner decision 2026-07-13). To re-include, add
        # _fetch_property_overview_rows(conn, eid) back into all_rows.
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
    entity_id: Optional[List[int]] = Query(None),
    txn_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    authorization: Optional[str] = Header(None),
):
    """Return MF transactions for the requesting user's entity or a subset."""
    conn = None
    try:
        payload = _require_auth(request, authorization)

        conn = get_db_connection()
        cursor = conn.cursor()
        # Any authenticated user may view every entity: ?entity_id=N (repeatable)
        # → that entity or subset, no param → all entities.
        eids = _resolve_entities(cursor, payload, entity_id)

        limit  = max(1, min(limit, 500))
        offset = max(0, offset)

        type_filter = txn_type.strip() if txn_type else None

        conds  = ["1=1"]
        params: list = []
        if eids:
            conds.append("t.entity_id = ANY(%s)")
            params.append(eids)
        if type_filter:
            conds.append("t.transaction_type ILIKE %s")
            params.append(type_filter)
        where = " AND ".join(conds)

        cursor.execute(f"""
            SELECT
                t.id, t.transaction_date, t.description, t.transaction_type,
                t.amount, t.units, t.nav, t.balance_units, t.folio_number,
                sm.security_name, sm.isin, e.entity_name
            FROM mf_transaction t
            JOIN security_master sm ON sm.id = t.security_id
            JOIN entity e ON e.id = t.entity_id
            WHERE {where}
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        rows = cursor.fetchall()

        cursor.execute(
            f"SELECT COUNT(*) AS total FROM mf_transaction t WHERE {where}",
            params
        )
        total = cursor.fetchone()["total"]
        cursor.close()

        return {
            "entity_id": (eids[0] if eids and len(eids) == 1 else 0),
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

# "properties" was removed 2026-07-13: real estate now lives in the dedicated
# property register (see /api/v1/properties), not in manual inputs.
VALID_CATEGORIES = {
    "liquid_fund", "debt_fund", "arbitrage_fund", "ppf",
    "pms", "direct_equity", "aif",
    "overseas_fund", "overseas_equity", "forex", "gold_etf",
    "unlisted", "startup", "art", "collectibles",
    "funds_transit", "broker_balance", "bank",
}

VALID_CURRENCIES = {"INR", "USD", "GBP", "EUR", "AED", "SGD", "HKD"}

# Categories whose value is built from funding rounds + corporate events
# (Phase 3) rather than a single hand-typed cost / current value.
UNLISTED_CATEGORIES = {"unlisted", "startup"}

# Real-estate value band derived from the admin-entered Ready Reckoner rate.
# The RRR (circle / guidance value) typically runs ~20-40% below real market
# value, so market ~= RRR / 0.7 ~= 1.4x. We express the estimate as a range —
# RRR x 1.5 (low) .. RRR x 2.0 (high) — and feed the midpoint (x 1.75) into the
# portfolio total. Kept here so the multipliers live in exactly one place.
RRR_LOW_MULT  = 1.5
RRR_HIGH_MULT = 2.0
RRR_MID_MULT  = 1.75


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
        if category in ("art", "collectibles"):
            cur.execute("DELETE FROM art_detail WHERE entity_id = %s AND label = %s",
                        (entity_id, label))
        if category == "properties":
            cur.execute("DELETE FROM property_detail WHERE entity_id = %s AND label = %s",
                        (entity_id, label))
        if category in UNLISTED_CATEGORIES:
            cur.execute("DELETE FROM unlisted_round WHERE entity_id = %s AND category = %s AND label = %s",
                        (entity_id, category, label))
            cur.execute("DELETE FROM unlisted_event WHERE entity_id = %s AND category = %s AND label = %s",
                        (entity_id, category, label))
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
MAX_UPLOAD_BYTES      = 200 * 1024 * 1024  # 200 MB per file (nginx client_max_body_size must match)
ATTACHMENT_KINDS      = {"art_image", "deed", "plan", "document",
                         "bill", "authentication_certificate"}


def _uploads_abspath(rel: str) -> str:
    """Resolve a stored-relative path to an absolute one, refusing traversal."""
    root = os.path.normpath(UPLOADS_ROOT)
    full = os.path.normpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid attachment path")
    return full


def _uploads_file_response(rel: str, media: str):
    """Serve one file from UPLOADS_ROOT after the caller has authorized it.

    With UPLOADS_XACCEL=1 in .env, the app answers with an X-Accel-Redirect
    header and nginx streams the file itself (sendfile — much faster for big
    scans, and the Python worker is freed immediately). Requires this nginx
    block alongside the /api/ location:

        location /_protected_uploads/ { internal; alias /var/www/uploads/; }

    Without the flag, FastAPI streams the file directly (works everywhere).
    """
    abs_path = _uploads_abspath(rel)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="File missing on disk")
    headers = {"Cache-Control": "private, max-age=86400"}
    if os.getenv("UPLOADS_XACCEL") == "1":
        headers["X-Accel-Redirect"] = "/_protected_uploads/" + rel.replace(os.sep, "/")
        headers["Content-Type"] = media
        return Response(status_code=200, headers=headers)
    return FileResponse(abs_path, media_type=media, headers=headers)


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
            raise HTTPException(status_code=413, detail="File too large (max 200 MB)")
        if not data:
            raise HTTPException(status_code=422, detail="Empty file")

        mime = (file.content_type
                or mimetypes.guess_type(file.filename or "")[0]
                or "application/octet-stream")
        ext  = os.path.splitext(file.filename or "")[1].lower()[:12]
        uid  = uuid.uuid4().hex
        # Organized by schema: manual/<entity_id>/<category>/
        rel_dir = os.path.join(MANUAL_UPLOAD_SUBDIR, str(entity_id), category)
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
        media = "image/jpeg" if use_thumb else (r["mime"] or "application/octet-stream")
        # Inline so images/PDFs render in the browser; private + cacheable.
        return _uploads_file_response(rel, media)
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
    entity_id:      int
    label:          str
    painter_name:   Optional[str] = None
    painter_about:  Optional[str] = None
    location:       Optional[str] = None   # where the piece is kept
    seller_name:    Optional[str] = None
    seller_address: Optional[str] = None


@app.post("/api/v1/art-detail")
@limiter.limit("30/minute")
def save_art_detail(request: Request, body: ArtDetailRequest,
                    authorization: Optional[str] = Header(None)):
    """Upsert painter / location / seller details for an Art or Collectibles
    entry (admin/IWS only). Shared table; collectibles simply leave painter null."""
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
            INSERT INTO art_detail (entity_id, label, painter_name, painter_about,
                                    location, seller_name, seller_address, updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (entity_id, label) DO UPDATE SET
                painter_name   = EXCLUDED.painter_name,
                painter_about  = EXCLUDED.painter_about,
                location       = EXCLUDED.location,
                seller_name    = EXCLUDED.seller_name,
                seller_address = EXCLUDED.seller_address,
                updated_by     = EXCLUDED.updated_by,
                updated_at     = NOW()
            """,
            (body.entity_id, body.label.strip(), body.painter_name, body.painter_about,
             body.location, body.seller_name, body.seller_address, user_id),
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


class PropertyDetailRequest(BaseModel):
    entity_id:           int
    label:               str
    location:            Optional[str]   = None
    area_sqft:           Optional[float] = None
    ready_reckoner_rate: Optional[float] = None


@app.post("/api/v1/property-detail")
@limiter.limit("30/minute")
def save_property_detail(request: Request, body: PropertyDetailRequest,
                         authorization: Optional[str] = Header(None)):
    """Upsert a property's Ready-Reckoner inputs (location, area, RRR) and, when
    both area and rate are present, write the derived value (RRR x area x 1.75
    midpoint) into a fresh manual_input version so the portfolio total reflects
    it. The manual_input row is created if it does not exist yet, so a property
    can be entered entirely from this panel. Admin only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        label = body.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail="label is required")
        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        urow = cur.fetchone()
        user_id = urow["id"] if urow else None

        cur.execute(
            """
            INSERT INTO property_detail
                (entity_id, label, location, area_sqft, ready_reckoner_rate, updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (entity_id, label) DO UPDATE SET
                location            = EXCLUDED.location,
                area_sqft           = EXCLUDED.area_sqft,
                ready_reckoner_rate = EXCLUDED.ready_reckoner_rate,
                updated_by          = EXCLUDED.updated_by,
                updated_at          = NOW()
            """,
            (body.entity_id, label, body.location, body.area_sqft,
             body.ready_reckoner_rate, user_id),
        )

        # Derive the value band; the midpoint becomes the property's current_value.
        mid = low = high = None
        if body.area_sqft and body.ready_reckoner_rate:
            base = float(body.area_sqft) * float(body.ready_reckoner_rate)
            low  = round(base * RRR_LOW_MULT, 2)
            high = round(base * RRR_HIGH_MULT, 2)
            mid  = round(base * RRR_MID_MULT, 2)

            # Carry forward cost / currency / inception / notes from the latest
            # version so we only overwrite the derived current_value.
            cur.execute(
                """
                SELECT cost, currency, inception_date, notes
                FROM   manual_input
                WHERE  entity_id = %s AND category = 'properties' AND label = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (body.entity_id, label),
            )
            prev = cur.fetchone()
            cur.execute(
                """
                INSERT INTO manual_input
                    (entity_id, category, label, cost, current_value, prev_week_value,
                     currency, raw_amount, fx_rate, inception_date, notes, updated_by, updated_at)
                VALUES (%s,'properties',%s,%s,%s,NULL,%s,NULL,NULL,%s,%s,%s,NOW())
                """,
                (
                    body.entity_id, label,
                    prev["cost"] if prev else None,
                    mid,
                    prev["currency"] if prev else "INR",
                    prev["inception_date"] if prev else None,
                    prev["notes"] if prev else None,
                    user_id,
                ),
            )

        conn.commit()
        cur.close()
        return {"saved": True, "value_low": low, "value_high": high, "value_mid": mid}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/property-detail: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Property register — dedicated land/building sheet with its own holder
# universe (companies, LLPs, trusts — NOT the system entity table) and a
# per-type document checklist. Uploads are converted to PDF where possible
# (see property_docs.convert_to_pdf); the original is kept when conversion
# changes or skips the file. All logins may view; only admin writes.
# ---------------------------------------------------------------------------

PROPERTY_UPLOAD_SUBDIR = "properties"   # properties/<property_id>/<doc_type>/


def _property_user_id(cur, payload) -> Optional[int]:
    cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
    row = cur.fetchone()
    return row["id"] if row else None


def _require_admin(cur, payload):
    if _live_role(cur, payload["email"]) != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


@app.get("/api/v1/property-doc-types")
def get_property_doc_types(request: Request,
                           authorization: Optional[str] = Header(None)):
    """The land/building document checklist that drives the upload dropdown."""
    _require_auth(request, authorization)
    return {"doc_types": property_docs.DOC_TYPES,
            "fair_value_multiplier": property_docs.FAIR_VALUE_MULTIPLIER}


@app.get("/api/v1/property-entities")
def list_property_entities(request: Request,
                           authorization: Optional[str] = Header(None)):
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""SELECT id, name, short_code, grp, is_custom
                       FROM property_entity ORDER BY grp, sort_order, name""")
        rows = cur.fetchall()
        cur.close()
        return [{"id": r["id"], "name": r["name"], "short_code": r["short_code"],
                 "grp": r["grp"], "is_custom": r["is_custom"]} for r in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/property-entities: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


class PropertyEntityRequest(BaseModel):
    name:       str
    short_code: Optional[str] = None
    grp:        str = "parent"     # "Others" additions default under Parent Companies


@app.post("/api/v1/property-entities")
@limiter.limit("30/minute")
def create_property_entity(request: Request, body: PropertyEntityRequest,
                           authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        if body.grp not in ("main", "parent"):
            raise HTTPException(status_code=422, detail="grp must be main or parent")
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """INSERT INTO property_entity (name, short_code, grp, is_custom, sort_order, created_by)
               VALUES (%s,%s,%s,TRUE,900,%s)
               ON CONFLICT (name) DO NOTHING RETURNING id""",
            (name, (body.short_code or "").strip() or None, body.grp, user_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="Entity already exists")
        write_audit_log(conn, user_id, "PROPERTY_ENTITY_CREATE", "property_entity",
                        row["id"], name)
        conn.commit()
        cur.close()
        return {"id": row["id"], "name": name}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/property-entities: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/property-nature-types")
def list_property_nature_types(request: Request,
                               authorization: Optional[str] = Header(None)):
    """Nature options (industrial, orchard… + admin customs) for the property form."""
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""SELECT id, name, is_custom FROM property_nature_type
                       ORDER BY sort_order, name""")
        rows = cur.fetchall()
        cur.close()
        return [{"id": r["id"], "name": r["name"], "is_custom": r["is_custom"]} for r in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/property-nature-types: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


class PropertyNatureTypeRequest(BaseModel):
    name: str


@app.post("/api/v1/property-nature-types")
@limiter.limit("30/minute")
def create_property_nature_type(request: Request, body: PropertyNatureTypeRequest,
                                authorization: Optional[str] = Header(None)):
    """Add a custom nature at runtime (admin only) — same pattern as custom holders."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """INSERT INTO property_nature_type (name, is_custom, sort_order, created_by)
               VALUES (%s, TRUE, 900, %s)
               ON CONFLICT (name) DO NOTHING RETURNING id""",
            (name, user_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="Nature already exists")
        write_audit_log(conn, user_id, "PROPERTY_NATURE_CREATE", "property_nature_type",
                        row["id"], name)
        conn.commit()
        cur.close()
        return {"id": row["id"], "name": name}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/property-nature-types: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# Bhunaksha Goa portal has no documented deep-link to a single survey plot (it is
# a stateful JS app: State -> District -> Taluka -> Village -> Survey). We expose
# the portal URL and the row's village/survey so the UI can render a "Bhunaksha"
# link; the admin makes the final in-portal selection.
BHUNAKSHA_GOA_URL = "https://bhunaksha.goa.gov.in/bhunaksha/"
OLD_LEASE_OWNER_SHARE = 0.5   # statutory sitting tenant holds the other half


def _maps_link(gps_link, address, location):
    """Manual GPS override wins; else a keyless Google Maps search link built from
    the address (falling back to the locality). None when there's nothing to map."""
    if gps_link:
        return gps_link
    q = (address or location or "").strip()
    if not q:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(q)


def _num(v):
    return float(v) if v is not None else None


def _property_row(r: dict, docs: list, owners: list, floors: list,
                  natures: list, images: list) -> dict:
    area = _num(r["area"])
    rrr  = _num(r["rrr"])
    # Land value from the circle rate (unchanged); building value from floors.
    fair = round(area * rrr * property_docs.FAIR_VALUE_MULTIPLIER, 2) \
        if area is not None and rrr is not None else None
    building_value = round(sum(f["floor_value"] for f in floors
                               if f["floor_value"] is not None), 2) if floors else 0.0
    market = _num(r["market_value"])
    # Displayed total: hand-entered market value (or land fair value) plus the
    # summed floor costings for building-like types.
    base = market if market is not None else (fair or 0.0)
    total = round((base or 0.0) + (building_value or 0.0), 2)
    is_old_lease = bool(r["is_old_lease"])
    # Old statutory lease: the sitting tenant holds ~50%, so only the owner's
    # half flows into portfolio totals (full value shown alongside).
    effective = round(total * OLD_LEASE_OWNER_SHARE, 2) if is_old_lease else total

    uploaded = {d["doc_type"] for d in docs}
    required = [d["slug"] for d in property_docs.doc_types_for(r["property_type"])
                if not d["optional"]]
    return {
        "purchase_price":   _num(r["purchase_price"]),
        "market_value":     market,
        "owners":           owners,
        "natures":          natures,
        "floors":           floors,
        "images":           images,
        "id":               r["id"],
        "name":             r["name"],
        "property_type":    r["property_type"],
        "holder_id":        r["holder_id"],
        "holder_name":      r["holder_name"],
        "location":         r["location"],
        "address":          r["address"],
        "taluka":           r["taluka"],
        "village":          r["village"],
        "survey_no":        r["survey_no"],
        "gps_link":         r["gps_link"],
        "maps_link":        _maps_link(r["gps_link"], r["address"], r["location"]),
        "bhunaksha_url":    BHUNAKSHA_GOA_URL if r["survey_no"] else None,
        "area":             area,
        "built_up_area":    _num(r["built_up_area"]),
        "area_unit":        r["area_unit"],
        "property_no":      r["property_no"],
        "acquisition_date": r["acquisition_date"].isoformat() if r["acquisition_date"] else None,
        "ownership":        r["ownership"],
        "tenure":           r["tenure"],
        "is_old_lease":     is_old_lease,
        "has_parking":      bool(r["has_parking"]),
        "parking_count":    r["parking_count"],
        "seller_name":      r["seller_name"],
        "seller_address":   r["seller_address"],
        "stamp_value":      _num(r["stamp_value"]),
        "lawyer_fees":      _num(r["lawyer_fees"]),
        "rrr":              rrr,
        "fair_value":       fair,
        "building_value":   building_value or None,
        "total_value":      total,
        "value_effective":  effective,   # feeds portfolio totals (halved if old lease)
        "sold":             r["sold"],
        "sale_price":       _num(r["sale_price"]),
        "sale_date":        r["sale_date"].isoformat() if r["sale_date"] else None,
        "notes":            r["notes"],
        "documents":        docs,
        "missing_required": [s for s in required if s not in uploaded],
    }


@app.get("/api/v1/properties")
def list_properties(request: Request, holder_id: Optional[int] = None,
                    authorization: Optional[str] = Header(None)):
    """The property sheet. Every authenticated login may view (holders are
    companies/trusts that don't map onto member entity scoping)."""
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cond, params = "", []
        if holder_id is not None:
            cond, params = "WHERE p.holder_id = %s", [holder_id]
        cur.execute(f"""
            SELECT p.*, e.name AS holder_name
            FROM   property p JOIN property_entity e ON e.id = p.holder_id
            {cond} ORDER BY e.sort_order, e.name, p.name""", params)
        props = cur.fetchall()
        docs_by_pid, owners_by_pid, floors_by_pid = {}, {}, {}
        natures_by_pid, images_by_pid = {}, {}
        if props:
            pids = [p["id"] for p in props]
            cur.execute(
                """SELECT id, property_id, doc_type, floor_id, original_name, custom_label,
                          mime, size_bytes, converted, original_path IS NOT NULL AS has_original,
                          uploaded_at
                   FROM property_document WHERE property_id = ANY(%s)
                   ORDER BY uploaded_at""",
                (pids,),
            )
            for d in cur.fetchall():
                docs_by_pid.setdefault(d["property_id"], []).append({
                    "id":            d["id"],
                    "doc_type":      d["doc_type"],
                    "floor_id":      d["floor_id"],
                    "original_name": d["original_name"],
                    "custom_label":  d["custom_label"],
                    "mime":          d["mime"],
                    "size_bytes":    int(d["size_bytes"]) if d["size_bytes"] is not None else None,
                    "converted":     d["converted"],
                    "has_original":  d["has_original"],
                    "uploaded_at":   d["uploaded_at"].isoformat() if d["uploaded_at"] else None,
                })
            cur.execute(
                """SELECT o.property_id, o.holder_id, o.pct, e.name
                   FROM property_owner o JOIN property_entity e ON e.id = o.holder_id
                   WHERE o.property_id = ANY(%s) ORDER BY o.pct DESC, e.name""",
                (pids,),
            )
            for o in cur.fetchall():
                owners_by_pid.setdefault(o["property_id"], []).append({
                    "holder_id": o["holder_id"], "name": o["name"], "pct": float(o["pct"]),
                })
            cur.execute(
                """SELECT id, property_id, floor_label, area, rate_per_unit, built_up_area,
                          carpet_area, is_rented, rent_amount, tenant, sort_order
                   FROM property_floor WHERE property_id = ANY(%s)
                   ORDER BY sort_order, id""",
                (pids,),
            )
            for f in cur.fetchall():
                area  = float(f["area"]) if f["area"] is not None else None
                bua   = float(f["built_up_area"]) if f["built_up_area"] is not None else None
                rate  = float(f["rate_per_unit"]) if f["rate_per_unit"] is not None else None
                basis = bua if bua is not None else area
                fval  = round(basis * rate, 2) if basis is not None and rate is not None else None
                floors_by_pid.setdefault(f["property_id"], []).append({
                    "id":            f["id"],
                    "floor_label":   f["floor_label"],
                    "area":          area,
                    "rate_per_unit": rate,
                    "built_up_area": bua,
                    "carpet_area":   float(f["carpet_area"]) if f["carpet_area"] is not None else None,
                    "is_rented":     bool(f["is_rented"]),
                    "rent_amount":   float(f["rent_amount"]) if f["rent_amount"] is not None else None,
                    "tenant":        f["tenant"],
                    "floor_value":   fval,
                })
            cur.execute(
                """SELECT n.property_id, n.nature_id, n.area, t.name
                   FROM property_nature n JOIN property_nature_type t ON t.id = n.nature_id
                   WHERE n.property_id = ANY(%s) ORDER BY t.sort_order, t.name""",
                (pids,),
            )
            for n in cur.fetchall():
                natures_by_pid.setdefault(n["property_id"], []).append({
                    "nature_id": n["nature_id"], "name": n["name"],
                    "area": float(n["area"]) if n["area"] is not None else None,
                })
            cur.execute(
                """SELECT id, property_id, caption, is_hero, thumb_path IS NOT NULL AS has_thumb
                   FROM property_image WHERE property_id = ANY(%s)
                   ORDER BY is_hero DESC, sort_order, id""",
                (pids,),
            )
            for im in cur.fetchall():
                images_by_pid.setdefault(im["property_id"], []).append({
                    "id": im["id"], "caption": im["caption"],
                    "is_hero": bool(im["is_hero"]), "has_thumb": im["has_thumb"],
                })
        cur.close()
        rows = [_property_row(p, docs_by_pid.get(p["id"], []),
                              owners_by_pid.get(p["id"], []),
                              floors_by_pid.get(p["id"], []),
                              natures_by_pid.get(p["id"], []),
                              images_by_pid.get(p["id"], [])) for p in props]
        total = sum(r["value_effective"] for r in rows
                    if r["value_effective"] is not None and not r["sold"])
        sold  = sum(r["sale_price"] for r in rows if r["sold"] and r["sale_price"] is not None)
        return {"count": len(rows), "total_fair_value": round(total, 2),
                "total_sold_value": round(sold, 2), "properties": rows}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/properties: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


class PropertyOwnerIn(BaseModel):
    holder_id: int
    pct:       float = 100.0


class PropertyNatureIn(BaseModel):
    nature_id: int
    area:      Optional[float] = None   # in the property's area_unit


class PropertyFloorIn(BaseModel):
    id:            Optional[int] = None   # present = update existing floor
    floor_label:   str
    area:          Optional[float] = None
    rate_per_unit: Optional[float] = None
    built_up_area: Optional[float] = None
    carpet_area:   Optional[float] = None
    is_rented:     bool            = False
    rent_amount:   Optional[float] = None
    tenant:        Optional[str]   = None


class PropertyRequest(BaseModel):
    name:             str
    property_type:    str
    holder_id:        int
    location:         Optional[str]   = None
    address:          Optional[str]   = None
    taluka:           Optional[str]   = None
    village:          Optional[str]   = None
    survey_no:        Optional[str]   = None
    gps_link:         Optional[str]   = None   # manual override; blank = derive from address
    area:             Optional[float] = None
    built_up_area:    Optional[float] = None
    area_unit:        Optional[str]   = "sq m"
    property_no:      Optional[str]   = None   # government-assigned property number
    acquisition_date: Optional[str]   = None   # YYYY-MM-DD
    ownership:        Optional[str]   = None
    tenure:           Optional[str]   = None   # freehold | leasehold
    is_old_lease:     bool            = False  # statutory pre-1990 rent-controlled lease
    has_parking:      bool            = False
    parking_count:    Optional[int]   = None
    seller_name:      Optional[str]   = None
    seller_address:   Optional[str]   = None
    stamp_value:      Optional[float] = None
    lawyer_fees:      Optional[float] = None
    purchase_price:   Optional[float] = None
    market_value:     Optional[float] = None
    rrr:              Optional[float] = None
    notes:            Optional[str]   = None
    owners:           Optional[List[PropertyOwnerIn]]  = None   # default: holder_id @ 100%
    natures:          Optional[List[PropertyNatureIn]] = None   # multi-nature area split
    floors:           Optional[List[PropertyFloorIn]]  = None   # buildings


def _validate_property_body(cur, body: PropertyRequest):
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    if body.property_type not in property_docs.LAND_TYPES | property_docs.BUILDING_TYPES:
        raise HTTPException(status_code=422, detail="Unknown property_type")
    if body.tenure not in (None, "freehold", "leasehold"):
        raise HTTPException(status_code=422, detail="tenure must be freehold or leasehold")
    cur.execute("SELECT id FROM property_entity WHERE id = %s", (body.holder_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=422, detail="Unknown holder entity")
    if body.natures:
        nids = [n.nature_id for n in body.natures]
        if len(set(nids)) != len(nids):
            raise HTTPException(status_code=422, detail="Duplicate nature")
        cur.execute("SELECT COUNT(*) AS n FROM property_nature_type WHERE id = ANY(%s)", (nids,))
        if cur.fetchone()["n"] != len(nids):
            raise HTTPException(status_code=422, detail="Unknown nature")
        if any(n.area is not None and n.area < 0 for n in body.natures):
            raise HTTPException(status_code=422, detail="Nature area must be non-negative")
    if body.acquisition_date:
        try:
            datetime.strptime(body.acquisition_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="acquisition_date must be YYYY-MM-DD")
    if body.owners:
        ids = [o.holder_id for o in body.owners]
        if len(set(ids)) != len(ids):
            raise HTTPException(status_code=422, detail="Duplicate owner entity")
        cur.execute("SELECT COUNT(*) AS n FROM property_entity WHERE id = ANY(%s)", (ids,))
        if cur.fetchone()["n"] != len(ids):
            raise HTTPException(status_code=422, detail="Unknown owner entity")
        if any(o.pct <= 0 for o in body.owners):
            raise HTTPException(status_code=422, detail="Ownership % must be positive")
        total = sum(o.pct for o in body.owners)
        if abs(total - 100.0) > 0.1:
            raise HTTPException(status_code=422, detail=f"Ownership must total 100% (got {total:g}%)")
    if body.floors:
        if any(not f.floor_label.strip() for f in body.floors):
            raise HTTPException(status_code=422, detail="Every floor needs a label")


def _save_property_children(cur, pid: int, body: PropertyRequest):
    """Replace owners + natures; upsert floors by id (so floor-plan / tenancy
    documents keep their floor link across edits) and drop floors omitted from
    the payload."""
    owners = body.owners or [PropertyOwnerIn(holder_id=body.holder_id, pct=100.0)]
    cur.execute("DELETE FROM property_owner WHERE property_id = %s", (pid,))
    for o in owners:
        cur.execute(
            "INSERT INTO property_owner (property_id, holder_id, pct) VALUES (%s,%s,%s)",
            (pid, o.holder_id, o.pct),
        )

    cur.execute("DELETE FROM property_nature WHERE property_id = %s", (pid,))
    for n in (body.natures or []):
        cur.execute(
            "INSERT INTO property_nature (property_id, nature_id, area) VALUES (%s,%s,%s)",
            (pid, n.nature_id, n.area),
        )

    floors = body.floors if property_docs.is_building_like(body.property_type) else []
    keep = []
    for i, f in enumerate(floors or []):
        cols = (f.floor_label.strip(), f.area, f.rate_per_unit, f.built_up_area,
                f.carpet_area, f.is_rented, f.rent_amount,
                (f.tenant or "").strip() or None, i)
        if f.id is not None:
            cur.execute(
                """UPDATE property_floor SET floor_label=%s, area=%s, rate_per_unit=%s,
                       built_up_area=%s, carpet_area=%s, is_rented=%s, rent_amount=%s,
                       tenant=%s, sort_order=%s
                   WHERE id=%s AND property_id=%s RETURNING id""",
                (*cols, f.id, pid),
            )
            row = cur.fetchone()
            if row:
                keep.append(row["id"])
                continue
        cur.execute(
            """INSERT INTO property_floor
                   (property_id, floor_label, area, rate_per_unit, built_up_area,
                    carpet_area, is_rented, rent_amount, tenant, sort_order)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (pid, *cols),
        )
        keep.append(cur.fetchone()["id"])
    if keep:
        cur.execute("DELETE FROM property_floor WHERE property_id = %s AND NOT (id = ANY(%s))",
                    (pid, keep))
    else:
        cur.execute("DELETE FROM property_floor WHERE property_id = %s", (pid,))


@app.post("/api/v1/properties")
@limiter.limit("30/minute")
def create_property(request: Request, body: PropertyRequest,
                    authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        _validate_property_body(cur, body)
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """INSERT INTO property
                   (name, property_type, holder_id, location, address, taluka, village,
                    survey_no, gps_link, area, built_up_area, area_unit, property_no,
                    acquisition_date, ownership, tenure, is_old_lease, has_parking,
                    parking_count, seller_name, seller_address, stamp_value, lawyer_fees,
                    purchase_price, market_value, rrr, notes, created_by, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (body.name.strip(), body.property_type, body.holder_id, body.location,
             body.address, body.taluka, body.village, body.survey_no,
             (body.gps_link or "").strip() or None, body.area, body.built_up_area,
             body.area_unit, body.property_no, body.acquisition_date or None,
             body.ownership, body.tenure, body.is_old_lease, body.has_parking,
             body.parking_count, body.seller_name, body.seller_address, body.stamp_value,
             body.lawyer_fees, body.purchase_price, body.market_value, body.rrr,
             body.notes, user_id, user_id),
        )
        pid = cur.fetchone()["id"]
        _save_property_children(cur, pid, body)
        write_audit_log(conn, user_id, "PROPERTY_CREATE", "property", pid, body.name.strip())
        conn.commit()
        cur.close()
        return {"id": pid}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/properties: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.put("/api/v1/properties/{prop_id}")
@limiter.limit("30/minute")
def update_property(prop_id: int, request: Request, body: PropertyRequest,
                    authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        _validate_property_body(cur, body)
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """UPDATE property SET
                   name=%s, property_type=%s, holder_id=%s, location=%s, address=%s,
                   taluka=%s, village=%s, survey_no=%s, gps_link=%s, area=%s,
                   built_up_area=%s, area_unit=%s, property_no=%s, acquisition_date=%s,
                   ownership=%s, tenure=%s, is_old_lease=%s, has_parking=%s,
                   parking_count=%s, seller_name=%s, seller_address=%s, stamp_value=%s,
                   lawyer_fees=%s, purchase_price=%s, market_value=%s, rrr=%s, notes=%s,
                   updated_by=%s, updated_at=NOW()
               WHERE id=%s RETURNING id""",
            (body.name.strip(), body.property_type, body.holder_id, body.location,
             body.address, body.taluka, body.village, body.survey_no,
             (body.gps_link or "").strip() or None, body.area, body.built_up_area,
             body.area_unit, body.property_no, body.acquisition_date or None,
             body.ownership, body.tenure, body.is_old_lease, body.has_parking,
             body.parking_count, body.seller_name, body.seller_address, body.stamp_value,
             body.lawyer_fees, body.purchase_price, body.market_value, body.rrr,
             body.notes, user_id, prop_id),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Property not found")
        _save_property_children(cur, prop_id, body)
        write_audit_log(conn, user_id, "PROPERTY_UPDATE", "property", prop_id, body.name.strip())
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
        logger.error(f"Error in PUT /api/v1/properties/{prop_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.delete("/api/v1/properties/{prop_id}")
def delete_property(prop_id: int, request: Request,
                    authorization: Optional[str] = Header(None)):
    """Delete a property + its document files (admin only)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("SELECT name FROM property WHERE id = %s", (prop_id,))
        prow = cur.fetchone()
        if not prow:
            raise HTTPException(status_code=404, detail="Property not found")
        cur.execute("SELECT stored_path, original_path FROM property_document WHERE property_id = %s",
                    (prop_id,))
        for r in cur.fetchall():
            for rel in (r["stored_path"], r["original_path"]):
                if not rel:
                    continue
                try:
                    p = _uploads_abspath(rel)
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    logger.warning(f"could not remove property file {rel}: {e}")
        cur.execute("DELETE FROM property WHERE id = %s", (prop_id,))
        user_id = _property_user_id(cur, payload)
        write_audit_log(conn, user_id, "PROPERTY_DELETE", "property", prop_id, prow["name"])
        conn.commit()
        cur.close()
        return {"deleted": prop_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in DELETE /api/v1/properties/{prop_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


class PropertySellRequest(BaseModel):
    sale_price: float
    sale_date:  Optional[str] = None   # YYYY-MM-DD, defaults to today


@app.post("/api/v1/properties/{prop_id}/sell")
@limiter.limit("30/minute")
def sell_property(prop_id: int, request: Request, body: PropertySellRequest,
                  authorization: Optional[str] = Header(None)):
    """Mark a property sold (admin only). It moves to the page's Sold section,
    stops contributing fair value, and the sale price feeds Realised Gains and
    the overview instead."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        if body.sale_price <= 0:
            raise HTTPException(status_code=422, detail="sale_price must be positive")
        sale_date = body.sale_date or date.today().isoformat()
        try:
            datetime.strptime(sale_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="sale_date must be YYYY-MM-DD")
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """UPDATE property SET sold = TRUE, sale_price = %s, sale_date = %s,
                   updated_by = %s, updated_at = NOW()
               WHERE id = %s RETURNING name""",
            (body.sale_price, sale_date, user_id, prop_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Property not found")
        write_audit_log(conn, user_id, "PROPERTY_SELL", "property", prop_id,
                        f"{row['name']} @ {body.sale_price}")
        conn.commit()
        cur.close()
        return {"sold": True}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/properties/{prop_id}/sell: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/properties/{prop_id}/unsell")
@limiter.limit("30/minute")
def unsell_property(prop_id: int, request: Request,
                    authorization: Optional[str] = Header(None)):
    """Undo an accidental sale (admin only) — restores the property to active."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """UPDATE property SET sold = FALSE, sale_price = NULL, sale_date = NULL,
                   updated_by = %s, updated_at = NOW()
               WHERE id = %s RETURNING name""",
            (user_id, prop_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Property not found")
        write_audit_log(conn, user_id, "PROPERTY_UNSELL", "property", prop_id, row["name"])
        conn.commit()
        cur.close()
        return {"sold": False}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/properties/{prop_id}/unsell: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.post("/api/v1/properties/{prop_id}/documents")
@limiter.limit("60/minute")
async def upload_property_document(
    prop_id: int,
    request: Request,
    doc_type: str = Form(...),
    floor_id: Optional[int] = Form(None),
    custom_label: Optional[str] = Form(None),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Upload one checklist document (admin only). Converted to PDF when we
    can (images always; office docs when LibreOffice is installed); the
    original upload is kept alongside whenever it isn't already the PDF."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        if doc_type not in property_docs.DOC_SLUGS:
            raise HTTPException(status_code=422, detail=f"Unknown doc_type: {doc_type}")
        cur.execute("SELECT id FROM property WHERE id = %s", (prop_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Property not found")
        if floor_id is not None:
            cur.execute("SELECT id FROM property_floor WHERE id = %s AND property_id = %s",
                        (floor_id, prop_id))
            if not cur.fetchone():
                raise HTTPException(status_code=422, detail="Unknown floor for this property")

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 200 MB)")
        if not data:
            raise HTTPException(status_code=422, detail="Empty file")

        mime = (file.content_type
                or mimetypes.guess_type(file.filename or "")[0]
                or "application/octet-stream")
        ext  = os.path.splitext(file.filename or "")[1].lower()[:12]
        uid  = uuid.uuid4().hex
        # Organized by schema: properties/<property_id>/<doc_type>/
        rel_dir = os.path.join(PROPERTY_UPLOAD_SUBDIR, str(prop_id), doc_type)
        os.makedirs(os.path.join(UPLOADS_ROOT, rel_dir), exist_ok=True)

        pdf = property_docs.convert_to_pdf(data, file.filename or "", mime)
        original_rel = None
        if pdf is not None:
            stored_rel = os.path.join(rel_dir, uid + ".pdf")
            with open(_uploads_abspath(stored_rel), "wb") as fh:
                fh.write(pdf)
            original_rel = os.path.join(rel_dir, uid + "_orig" + ext)
            with open(_uploads_abspath(original_rel), "wb") as fh:
                fh.write(data)
            stored_mime, converted = "application/pdf", True
        else:
            stored_rel = os.path.join(rel_dir, uid + ext)
            with open(_uploads_abspath(stored_rel), "wb") as fh:
                fh.write(data)
            stored_mime, converted = mime, False

        user_id = _property_user_id(cur, payload)
        clabel = (custom_label or "").strip() or None
        cur.execute(
            """INSERT INTO property_document
                   (property_id, doc_type, floor_id, original_name, custom_label,
                    stored_path, original_path, mime, size_bytes, converted, uploaded_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id, uploaded_at""",
            (prop_id, doc_type, floor_id, file.filename, clabel, stored_rel, original_rel,
             stored_mime, len(data), converted, user_id),
        )
        row = cur.fetchone()
        write_audit_log(conn, user_id, "PROPERTY_DOC_UPLOAD", "property_document",
                        row["id"], f"{prop_id}/{doc_type} ({file.filename})")
        conn.commit()
        cur.close()
        return {
            "id":            row["id"],
            "doc_type":      doc_type,
            "custom_label":  clabel,
            "original_name": file.filename,
            "mime":          stored_mime,
            "converted":     converted,
            "has_original":  original_rel is not None,
            "uploaded_at":   row["uploaded_at"].isoformat(),
        }
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/properties/{prop_id}/documents: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/property-documents/{doc_id}/file")
def serve_property_document(doc_id: int, request: Request, original: bool = False,
                            authorization: Optional[str] = Header(None)):
    """Serve a document inline (PDF/image renders in the browser). Pass
    ?original=true for the pre-conversion upload (e.g. the AutoCAD source)."""
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""SELECT stored_path, original_path, mime, original_name
                       FROM property_document WHERE id = %s""", (doc_id,))
        r = cur.fetchone()
        cur.close()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        rel = r["original_path"] if (original and r["original_path"]) else r["stored_path"]
        media = (mimetypes.guess_type(rel)[0] or "application/octet-stream") \
            if original else (r["mime"] or "application/octet-stream")
        return _uploads_file_response(rel, media)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving property document {doc_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.delete("/api/v1/property-documents/{doc_id}")
def delete_property_document(doc_id: int, request: Request,
                             authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("""SELECT property_id, doc_type, stored_path, original_path
                       FROM property_document WHERE id = %s""", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        for rel in (r["stored_path"], r["original_path"]):
            if not rel:
                continue
            try:
                p = _uploads_abspath(rel)
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logger.warning(f"could not remove property document file {rel}: {e}")
        cur.execute("DELETE FROM property_document WHERE id = %s", (doc_id,))
        user_id = _property_user_id(cur, payload)
        write_audit_log(conn, user_id, "PROPERTY_DOC_DELETE", "property_document",
                        doc_id, f"{r['property_id']}/{r['doc_type']}")
        conn.commit()
        cur.close()
        return {"deleted": doc_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in DELETE /api/v1/property-documents/{doc_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Property images (the Airbnb-style gallery). Separate from the document
# checklist: photos get thumbnails and one is the hero/cover. Admin writes; all
# logins may view/serve. Files live under properties/<id>/images/.
# ---------------------------------------------------------------------------
PROPERTY_IMAGE_SUBDIR = "images"


@app.post("/api/v1/properties/{prop_id}/images")
@limiter.limit("60/minute")
async def upload_property_image(
    prop_id: int,
    request: Request,
    caption: Optional[str] = Form(None),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    """Upload one gallery image (admin only). The first image for a property
    becomes its hero/cover automatically."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("SELECT id FROM property WHERE id = %s", (prop_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Property not found")

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 200 MB)")
        if not data:
            raise HTTPException(status_code=422, detail="Empty file")
        mime = (file.content_type
                or mimetypes.guess_type(file.filename or "")[0]
                or "application/octet-stream")
        if not mime.startswith("image/"):
            raise HTTPException(status_code=422, detail="Only image files are allowed")

        ext = os.path.splitext(file.filename or "")[1].lower()[:12] or ".jpg"
        uid = uuid.uuid4().hex
        rel_dir = os.path.join(PROPERTY_UPLOAD_SUBDIR, str(prop_id), PROPERTY_IMAGE_SUBDIR)
        os.makedirs(os.path.join(UPLOADS_ROOT, rel_dir), exist_ok=True)
        rel_path = os.path.join(rel_dir, uid + ext)
        with open(_uploads_abspath(rel_path), "wb") as fh:
            fh.write(data)
        thumb_rel = None
        cand = os.path.join(rel_dir, uid + "_thumb.jpg")
        if _make_thumbnail(_uploads_abspath(rel_path), _uploads_abspath(cand)):
            thumb_rel = cand

        # First image for the property is the hero; next sort_order after the last.
        cur.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(sort_order), -1) AS mx "
            "FROM property_image WHERE property_id = %s", (prop_id,))
        agg = cur.fetchone()
        is_hero = agg["n"] == 0
        user_id = _property_user_id(cur, payload)
        cur.execute(
            """INSERT INTO property_image
                   (property_id, stored_path, thumb_path, caption, sort_order, is_hero,
                    mime, size_bytes, uploaded_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (prop_id, rel_path, thumb_rel, (caption or "").strip() or None,
             agg["mx"] + 1, is_hero, mime, len(data), user_id),
        )
        img_id = cur.fetchone()["id"]
        write_audit_log(conn, user_id, "PROPERTY_IMAGE_UPLOAD", "property_image",
                        img_id, f"{prop_id} ({file.filename})")
        conn.commit()
        cur.close()
        return {"id": img_id, "is_hero": is_hero, "has_thumb": thumb_rel is not None}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/properties/{prop_id}/images: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


def _serve_property_image(img_id: int, request: Request, authorization, want_thumb: bool):
    conn = None
    try:
        _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("SELECT stored_path, thumb_path, mime FROM property_image WHERE id = %s",
                    (img_id,))
        r = cur.fetchone()
        cur.close()
        if not r:
            raise HTTPException(status_code=404, detail="Image not found")
        use_thumb = want_thumb and bool(r["thumb_path"])
        rel   = r["thumb_path"] if use_thumb else r["stored_path"]
        media = "image/jpeg" if use_thumb else (r["mime"] or "application/octet-stream")
        return _uploads_file_response(rel, media)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving property image {img_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/property-images/{img_id}/file")
def serve_property_image_file(img_id: int, request: Request,
                              authorization: Optional[str] = Header(None)):
    return _serve_property_image(img_id, request, authorization, want_thumb=False)


@app.get("/api/v1/property-images/{img_id}/thumb")
def serve_property_image_thumb(img_id: int, request: Request,
                               authorization: Optional[str] = Header(None)):
    return _serve_property_image(img_id, request, authorization, want_thumb=True)


@app.post("/api/v1/property-images/{img_id}/hero")
@limiter.limit("60/minute")
def set_property_image_hero(img_id: int, request: Request,
                            authorization: Optional[str] = Header(None)):
    """Make this image the property's hero/cover (admin only)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("SELECT property_id FROM property_image WHERE id = %s", (img_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Image not found")
        pid = r["property_id"]
        cur.execute("UPDATE property_image SET is_hero = (id = %s) WHERE property_id = %s",
                    (img_id, pid))
        conn.commit()
        cur.close()
        return {"hero": img_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/property-images/{img_id}/hero: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.delete("/api/v1/property-images/{img_id}")
def delete_property_image(img_id: int, request: Request,
                          authorization: Optional[str] = Header(None)):
    """Delete a gallery image + its files (admin only). If it was the hero, the
    next remaining image is promoted."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        _require_admin(cur, payload)
        cur.execute("SELECT property_id, stored_path, thumb_path, is_hero "
                    "FROM property_image WHERE id = %s", (img_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Image not found")
        for rel in (r["stored_path"], r["thumb_path"]):
            if not rel:
                continue
            try:
                p = _uploads_abspath(rel)
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logger.warning(f"could not remove property image file {rel}: {e}")
        cur.execute("DELETE FROM property_image WHERE id = %s", (img_id,))
        if r["is_hero"]:
            cur.execute(
                """UPDATE property_image SET is_hero = TRUE
                   WHERE id = (SELECT id FROM property_image WHERE property_id = %s
                               ORDER BY sort_order, id LIMIT 1)""",
                (r["property_id"],))
        user_id = _property_user_id(cur, payload)
        write_audit_log(conn, user_id, "PROPERTY_IMAGE_DELETE", "property_image",
                        img_id, str(r["property_id"]))
        conn.commit()
        cur.close()
        return {"deleted": img_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in DELETE /api/v1/property-images/{img_id}: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/manual-assets")
def get_manual_assets(
    request: Request,
    category: str,
    entity_id: Optional[List[int]] = Query(None),
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
        eids = _resolve_entities(cur, payload, entity_id)

        conds, params = ["m.category = %s"], [category]
        if eids:
            conds.append("m.entity_id = ANY(%s)"); params.append(eids)
        where = "WHERE " + " AND ".join(conds)
        cur.execute(
            f"""
            SELECT DISTINCT ON (m.entity_id, m.label)
                m.entity_id, e.entity_name, m.label, m.cost, m.current_value,
                m.currency, m.raw_amount, m.fx_rate,
                m.inception_date, m.notes, m.updated_at
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
        if eids:
            acond.append("entity_id = ANY(%s)"); aparams.append(eids)
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

        # Art / collectibles details (painter for art; location + seller for both).
        art_by_key: dict = {}
        if category in ("art", "collectibles"):
            dcond, dparams = [], []
            if eids:
                dcond.append("entity_id = ANY(%s)"); dparams.append(eids)
            dwhere = ("WHERE " + " AND ".join(dcond)) if dcond else ""
            cur.execute(
                f"""SELECT entity_id, label, painter_name, painter_about,
                           location, seller_name, seller_address
                    FROM art_detail {dwhere}""",
                dparams,
            )
            for d in cur.fetchall():
                art_by_key[(d["entity_id"], d["label"])] = {
                    "painter_name":   d["painter_name"],
                    "painter_about":  d["painter_about"],
                    "location":       d["location"],
                    "seller_name":    d["seller_name"],
                    "seller_address": d["seller_address"],
                }

        # Property Ready-Reckoner inputs + derived value band, grouped by key.
        prop_by_key: dict = {}
        if category == "properties":
            pcond, pparams = [], []
            if eids:
                pcond.append("entity_id = ANY(%s)"); pparams.append(eids)
            pwhere = ("WHERE " + " AND ".join(pcond)) if pcond else ""
            cur.execute(
                f"""SELECT entity_id, label, location, area_sqft, ready_reckoner_rate
                    FROM property_detail {pwhere}""",
                pparams,
            )
            for d in cur.fetchall():
                area = float(d["area_sqft"]) if d["area_sqft"] is not None else None
                rate = float(d["ready_reckoner_rate"]) if d["ready_reckoner_rate"] is not None else None
                base = area * rate if (area and rate) else None
                prop_by_key[(d["entity_id"], d["label"])] = {
                    "location":            d["location"],
                    "area_sqft":           area,
                    "ready_reckoner_rate": rate,
                    "value_low":  round(base * RRR_LOW_MULT, 2)  if base is not None else None,
                    "value_high": round(base * RRR_HIGH_MULT, 2) if base is not None else None,
                    "value_mid":  round(base * RRR_MID_MULT, 2)  if base is not None else None,
                }

        # Unlisted / startup funding rounds + corporate events, grouped by key.
        rounds_by_key: dict = {}
        if category in UNLISTED_CATEGORIES:
            rcond, rparams = ["category = %s"], [category]
            if eids:
                rcond.append("entity_id = ANY(%s)"); rparams.append(eids)
            rwhere = "WHERE " + " AND ".join(rcond)
            cur.execute(
                f"""SELECT id, entity_id, label, round_name, round_date, round_valuation,
                           price_per_share, shares, amount_invested, notes
                    FROM unlisted_round {rwhere} ORDER BY sort_order, id""",
                rparams,
            )
            rr_by_key: dict = {}
            for r in cur.fetchall():
                rr_by_key.setdefault((r["entity_id"], r["label"]), []).append(dict(r))
            cur.execute(
                f"""SELECT id, entity_id, label, event_type, event_date, factor,
                           bonus_shares, ratio_text, notes
                    FROM unlisted_event {rwhere} ORDER BY sort_order, id""",
                rparams,
            )
            ee_by_key: dict = {}
            for e in cur.fetchall():
                ee_by_key.setdefault((e["entity_id"], e["label"]), []).append(dict(e))
            for key in set(list(rr_by_key.keys()) + list(ee_by_key.keys())):
                rds = rr_by_key.get(key, [])
                evs = ee_by_key.get(key, [])
                rounds_by_key[key] = {
                    "rounds":    [_unlisted_round_row(x) for x in rds],
                    "events":    [_unlisted_event_row(x) for x in evs],
                    "aggregate": _compute_unlisted(rds, evs),
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
                "raw_amount":    float(m["raw_amount"])    if m["raw_amount"]    is not None else None,
                "fx_rate":       float(m["fx_rate"])       if m["fx_rate"]       is not None else None,
                "inception_date": str(m["inception_date"]) if m["inception_date"] else None,
                "notes":         m["notes"],
                "updated_at":    m["updated_at"].isoformat() if m["updated_at"] else None,
                "attachments":   att_by_key.get(key, []),
            }
            if category in ("art", "collectibles"):
                item.update(art_by_key.get(key, {
                    "painter_name": None, "painter_about": None,
                    "location": None, "seller_name": None, "seller_address": None,
                }))
            if category == "properties":
                item.update(prop_by_key.get(key, {
                    "location": None, "area_sqft": None, "ready_reckoner_rate": None,
                    "value_low": None, "value_high": None, "value_mid": None,
                }))
            if category in UNLISTED_CATEGORIES:
                item.update(rounds_by_key.get(key, {"rounds": [], "events": [], "aggregate": None}))
            out.append(item)

        total_value = round(sum(a["current_value"] or 0 for a in out), 2)
        return {
            "category":      category,
            "entity_id":     (eids[0] if eids and len(eids) == 1 else 0),
            "total_value":   total_value,
            "count":         len(out),
            "assets":        out,
            "fx_rates":      _latest_fx_rates(conn),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/manual-assets: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/nav-coverage")
def get_nav_coverage(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Which entities have data in each nav section, plus every Manual Data
    category with entries. Drives two things in the frontend:
      - NavTabs hides section tabs whose entity list is empty, and shows a
        dynamic tab for pageless manual categories (AIF, PPF, …);
      - each asset page hides entity filter pills for entities with no data
        in that section (e.g. ADR on Equity).
    Global across entities for every authenticated role — all logins may view
    all entities (see _resolve_entity)."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        is_admin = _live_role(cur, payload["email"]) == "admin"

        def ids(sql: str, params: tuple = ()) -> list:
            cur.execute(sql, params)
            return sorted({r["entity_id"] for r in cur.fetchall()})

        def manual(cats: list) -> list:
            return ids("SELECT DISTINCT entity_id FROM manual_input WHERE category = ANY(%s)", (cats,))

        # Same asset_class split the Equity and Commodities endpoints use.
        non_commodity = "COALESCE(asset_class, 'equity') NOT IN ('gold','silver','commodity')"
        commodity     = "COALESCE(asset_class, 'equity') IN ('gold','silver','commodity')"

        sections = {
            "/mutual-funds":   ids("SELECT DISTINCT entity_id FROM holding"),
            "/equity":         ids(f"SELECT DISTINCT entity_id FROM equity_holding WHERE {non_commodity}"),
            "/foreign-equity": sorted(set(ids(f"SELECT DISTINCT entity_id FROM foreign_equity_holding WHERE {non_commodity}"))
                                      | set(manual(["overseas_equity"]))),
            "/fno":            ids("SELECT DISTINCT entity_id FROM fno_position"),
            "/bank-accounts":  manual(["bank", "forex"]),
            "/pms":            ids("SELECT DISTINCT entity_id FROM pms_holding"),
            "/gold-silver":    ids(f"""SELECT entity_id FROM equity_holding WHERE {commodity}
                                       UNION SELECT entity_id FROM foreign_equity_holding WHERE {commodity}"""),
            "/unlisted":       manual(["unlisted", "startup"]),
            # The property register has its own holder universe (companies /
            # trusts), so the tab shows for everyone once any property exists —
            # and always for admins, who need a way in to add the first one.
            "/properties":     ids(f"""SELECT e.id AS entity_id FROM entity e
                                       WHERE {'TRUE' if is_admin else
                                              'EXISTS (SELECT 1 FROM property)'}"""),
            "/art":            manual(["art"]),
            "/collectibles":   manual(["collectibles"]),
        }

        cur.execute("SELECT category, array_agg(DISTINCT entity_id) AS eids FROM manual_input GROUP BY category")
        categories = {r["category"]: sorted(r["eids"]) for r in cur.fetchall()}
        cur.close()
        return {"sections": sections, "categories": categories}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/nav-coverage: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


# ---------------------------------------------------------------------------
# Unlisted / startup funding rounds + corporate events (Phase 3).
# Each round records price-per-share, shares acquired and amount invested.
# Splits / bonuses adjust the share count (factor) only — per-share price
# divides by the same factor, so total value is unchanged. Current value =
# total shares (after events) × the latest round's effective price-per-share;
# cost = Σ round investments. The derived aggregate is written to a fresh
# manual_input version so Overview and the manual list keep working unchanged.
# Keyed by the STABLE (entity_id, category, label). Admin (IWS) writes; the
# owning entity (and admin) can read.
# ---------------------------------------------------------------------------

class UnlistedRoundItem(BaseModel):
    round_name:      Optional[str]   = None
    round_date:      Optional[str]   = None   # YYYY-MM-DD
    round_valuation: Optional[float] = None   # company valuation at this round (informational)
    price_per_share: Optional[float] = None
    shares:          Optional[float] = None
    amount_invested: Optional[float] = None   # defaults to price × shares when omitted
    notes:           Optional[str]   = None


class UnlistedEventItem(BaseModel):
    event_type:   str                          # split | bonus
    event_date:   Optional[str]   = None       # YYYY-MM-DD
    factor:       Optional[float] = None       # split: share multiplier (2:1 -> 2.0)
    bonus_shares: Optional[float] = None       # bonus: absolute number of new shares
    ratio_text:   Optional[str]   = None       # display, e.g. '2:1' or '+1,500'
    notes:        Optional[str]   = None


class UnlistedRoundsRequest(BaseModel):
    entity_id: int
    category:  str
    label:     str = Field(min_length=1, max_length=200)
    rounds:    List[UnlistedRoundItem] = []
    events:    List[UnlistedEventItem] = []


def _parse_date_opt(s, field):
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field}: {s}")


def _unlisted_round_row(r: dict) -> dict:
    return {
        "id":              r.get("id"),
        "round_name":      r.get("round_name"),
        "round_date":      str(r["round_date"]) if r.get("round_date") else None,
        "round_valuation": float(r["round_valuation"]) if r.get("round_valuation") is not None else None,
        "price_per_share": float(r["price_per_share"]) if r.get("price_per_share") is not None else None,
        "shares":          float(r["shares"]) if r.get("shares") is not None else None,
        "amount_invested": float(r["amount_invested"]) if r.get("amount_invested") is not None else None,
        "notes":           r.get("notes"),
    }


def _unlisted_event_row(e: dict) -> dict:
    return {
        "id":           e.get("id"),
        "event_type":   e.get("event_type"),
        "event_date":   str(e["event_date"]) if e.get("event_date") else None,
        "factor":       float(e["factor"]) if e.get("factor") is not None else None,
        "bonus_shares": float(e["bonus_shares"]) if e.get("bonus_shares") is not None else None,
        "ratio_text":   e.get("ratio_text"),
        "notes":        e.get("notes"),
    }


def _compute_unlisted(rounds: list, events: list) -> dict:
    """Walk rounds + corporate events in date order over a running share pool.
    A SPLIT multiplies the pool by `factor`; a BONUS adds `bonus_shares` to the
    pool (its effective factor = (pool + bonus) / pool, so the per-share price
    drops proportionally and total value is unchanged). Current value = final
    pool × the latest round's price-per-share adjusted by the events that follow
    it. Cost = Σ round investments. Accepts DB rows or request dicts; numbers may
    be Decimal or float. Missing round_date => oldest, missing event_date =>
    newest."""
    DMIN, DMAX = date.min, date.max

    def gd(x, k):
        return x.get(k) if isinstance(x, dict) else getattr(x, k, None)

    # Build a single date-ordered timeline; rounds sort before events on a tie.
    items = []
    for i, r in enumerate(rounds):
        items.append(((gd(r, "round_date") or DMIN), 0, i, "round", r))
    for i, e in enumerate(events):
        items.append(((gd(e, "event_date") or DMAX), 1, i, "event", e))
    items.sort(key=lambda t: (t[0], t[1], t[2]))

    pool = 0.0
    cost = 0.0
    last_price = None          # latest round's recorded price-per-share
    post_factor = 1.0          # product of event factors applied AFTER the latest priced round
    round_eff = []             # per-round running effective share count
    event_out = []             # per-event resulting pool (in original event order)
    event_pool_by_i = {}

    for _, _, idx, kind, obj in items:
        if kind == "round":
            sh  = float(gd(obj, "shares") or 0)
            pps = gd(obj, "price_per_share")
            amt = gd(obj, "amount_invested")
            if amt is None and pps is not None:
                amt = float(pps) * sh
            cost += float(amt or 0)
            pool += sh
            entry = {"obj": obj, "eff": sh}
            round_eff.append(entry)
            if pps is not None:
                last_price = float(pps)
                post_factor = 1.0
        else:
            etype = gd(obj, "event_type")
            bonus = gd(obj, "bonus_shares")
            if etype == "bonus" and bonus is not None:
                c = float(bonus or 0)
                f = (pool + c) / pool if pool > 0 else 1.0
                pool += c
            else:
                f = float(gd(obj, "factor") or 1)
                pool *= f
            post_factor *= f
            for entry in round_eff:
                entry["eff"] *= f
            event_pool_by_i[idx] = pool

    total_shares = pool
    current_pps = (last_price / post_factor) if (last_price is not None and post_factor) else last_price
    current_value = round(total_shares * current_pps, 2) if current_pps is not None else None

    breakdown = []
    for entry in round_eff:
        r = entry["obj"]
        sh  = float(gd(r, "shares") or 0)
        pps = gd(r, "price_per_share")
        amt = gd(r, "amount_invested")
        if amt is None and pps is not None:
            amt = float(pps) * sh
        rd = gd(r, "round_date")
        breakdown.append({
            "round_name":       gd(r, "round_name"),
            "round_date":       str(rd) if rd else None,
            "round_valuation":  float(gd(r, "round_valuation")) if gd(r, "round_valuation") is not None else None,
            "price_per_share":  float(pps) if pps is not None else None,
            "shares":           sh,
            "amount_invested":  round(float(amt or 0), 2),
            "effective_shares": round(entry["eff"], 4),
            "current_value":    round(entry["eff"] * current_pps, 2) if current_pps is not None else None,
            "notes":            gd(r, "notes"),
        })

    return {
        "cost":                    round(cost, 2),
        "total_shares":            round(total_shares, 4),
        "current_price_per_share": round(current_pps, 6) if current_pps is not None else None,
        "current_value":           current_value,
        "pnl":                     round(current_value - cost, 2) if current_value is not None else None,
        "breakdown":               breakdown,
        "event_pool":              {str(k): round(v, 4) for k, v in event_pool_by_i.items()},
    }


@app.post("/api/v1/unlisted-rounds")
@limiter.limit("30/minute")
def save_unlisted_rounds(request: Request, body: UnlistedRoundsRequest,
                         authorization: Optional[str] = Header(None)):
    """Replace all funding rounds + corporate events for an unlisted/startup
    holding and write the derived aggregate (cost, current value) to a fresh
    manual_input version. Admin (IWS) only."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        if _live_role(cur, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        if body.category not in UNLISTED_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Category does not support rounds: {body.category}")
        label = body.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail="label is required")

        rounds_in = [{
            "round_name":      r.round_name,
            "round_date":      _parse_date_opt(r.round_date, "round_date"),
            "round_valuation": r.round_valuation,
            "price_per_share": r.price_per_share,
            "shares":          r.shares,
            "amount_invested": r.amount_invested,
            "notes":           r.notes,
        } for r in body.rounds]
        events_in = []
        for e in body.events:
            if e.event_type not in ("split", "bonus"):
                raise HTTPException(status_code=422, detail=f"Invalid event_type: {e.event_type}")
            if e.event_type == "split":
                if e.factor is None or e.factor <= 0:
                    raise HTTPException(status_code=422, detail="split ratio must be greater than 0")
            else:  # bonus
                if e.bonus_shares is None or e.bonus_shares <= 0:
                    raise HTTPException(status_code=422, detail="bonus shares must be greater than 0")
            events_in.append({
                "event_type":   e.event_type,
                "event_date":   _parse_date_opt(e.event_date, "event_date"),
                "factor":       e.factor if e.event_type == "split" else None,
                "bonus_shares": e.bonus_shares if e.event_type == "bonus" else None,
                "ratio_text":   e.ratio_text,
                "notes":        e.notes,
            })

        agg = _compute_unlisted(rounds_in, events_in)

        cur.execute("SELECT id FROM users WHERE email = %s", (payload["email"],))
        urow = cur.fetchone()
        user_id = urow["id"] if urow else None

        # Carry forward prev_week_value / inception / currency / notes from the
        # latest existing version so weekly change and metadata persist.
        cur.execute("""
            SELECT prev_week_value, inception_date, currency, notes
            FROM manual_input
            WHERE entity_id = %s AND category = %s AND label = %s
            ORDER BY updated_at DESC LIMIT 1
        """, (body.entity_id, body.category, label))
        prev = cur.fetchone()
        prev_week = prev["prev_week_value"] if prev else None
        inception = prev["inception_date"] if prev else None
        currency  = (prev["currency"] if prev else None) or "INR"
        mi_notes  = prev["notes"] if prev else None

        cur.execute("DELETE FROM unlisted_round WHERE entity_id=%s AND category=%s AND label=%s",
                    (body.entity_id, body.category, label))
        cur.execute("DELETE FROM unlisted_event WHERE entity_id=%s AND category=%s AND label=%s",
                    (body.entity_id, body.category, label))
        for i, r in enumerate(rounds_in):
            cur.execute("""
                INSERT INTO unlisted_round
                    (entity_id, category, label, round_name, round_date, round_valuation,
                     price_per_share, shares, amount_invested, notes, sort_order, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (body.entity_id, body.category, label, r["round_name"], r["round_date"],
                  r["round_valuation"], r["price_per_share"], r["shares"], r["amount_invested"],
                  r["notes"], i, user_id))
        for i, e in enumerate(events_in):
            cur.execute("""
                INSERT INTO unlisted_event
                    (entity_id, category, label, event_type, event_date, factor,
                     bonus_shares, ratio_text, notes, sort_order, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (body.entity_id, body.category, label, e["event_type"], e["event_date"],
                  e["factor"], e["bonus_shares"], e["ratio_text"], e["notes"], i, user_id))

        # Derived aggregate as a fresh manual_input version.
        cur.execute("""
            INSERT INTO manual_input
                (entity_id, category, label, cost, current_value, prev_week_value,
                 currency, raw_amount, fx_rate, inception_date, notes, updated_by, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s,%s,%s,NOW())
        """, (body.entity_id, body.category, label, agg["cost"], agg["current_value"],
              prev_week, currency, inception, mi_notes, user_id))

        write_audit_log(conn, user_id, "UNLISTED_ROUNDS_SAVE", "unlisted_round", None,
                        f"{body.category}/{label}: {len(rounds_in)} round(s), {len(events_in)} event(s)")
        conn.commit()
        cur.close()
        return {"saved": True, "aggregate": agg}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error in POST /api/v1/unlisted-rounds: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")
    finally:
        release_db_connection(conn)


@app.get("/api/v1/unlisted-rounds")
def get_unlisted_rounds(
    request: Request,
    category: str,
    label: str,
    entity_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    """Funding rounds + corporate events + derived aggregate/breakdown for one
    unlisted/startup holding. Entity-scoped for non-admins."""
    conn = None
    try:
        if category not in UNLISTED_CATEGORIES:
            raise HTTPException(status_code=422, detail=f"Invalid category: {category}")
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        eid  = _resolve_entity(cur, payload, entity_id)
        if eid is None:
            raise HTTPException(status_code=400, detail="entity_id is required")

        cur.execute("""
            SELECT id, round_name, round_date, round_valuation, price_per_share, shares, amount_invested, notes
            FROM unlisted_round WHERE entity_id=%s AND category=%s AND label=%s
            ORDER BY sort_order, id
        """, (eid, category, label))
        rounds = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT id, event_type, event_date, factor, bonus_shares, ratio_text, notes
            FROM unlisted_event WHERE entity_id=%s AND category=%s AND label=%s
            ORDER BY sort_order, id
        """, (eid, category, label))
        events = [dict(e) for e in cur.fetchall()]
        cur.close()

        return {
            "entity_id": eid,
            "category":  category,
            "label":     label,
            "rounds":    [_unlisted_round_row(r) for r in rounds],
            "events":    [_unlisted_event_row(e) for e in events],
            "aggregate": _compute_unlisted(rounds, events),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in GET /api/v1/unlisted-rounds: {e}")
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

# Bank statements live under the same canonical uploads root as everything else.
BANK_STATEMENT_DIR = os.path.join(UPLOADS_ROOT, "bank-statements")
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
    entity_id: Optional[List[int]] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """List bank accounts with native balance + INR equivalent.
    Admin sees all (optionally ?entity_id=N, repeatable); a member sees only their own entity."""
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        # Admin + param → that entity/subset; admin no param → all; member → own (param ignored).
        eids = _resolve_entities(cur, payload, entity_id)

        where  = "WHERE b.entity_id = ANY(%s)" if eids else ""
        params = [eids] if eids else []
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
    """Realised gains — admin sees all entities; a member sees only their own.

    period   — "fy" (default, FY-to-date) or "inception" (whole history).
    switches — "include" (default) or "exclude" (drop SWITCH_IN/SWITCH_OUT).
    """
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cur  = conn.cursor()
        # Admin → None (all entities); member → their own entity_id only.
        eid = _resolve_entity(cur, payload, None)

        from workers.report_generator import _fetch_realised_gains
        if eid is None:
            cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
        else:
            cur.execute("SELECT id, entity_name FROM entity WHERE id = %s", (eid,))
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
                    "category":        r.get("category", r["group"]),
                    "group":           r["group"],
                    "security_name":   r["security_name"],
                    "purchase_amount": r["purchase_amount"],
                    "sale_date":       str(r["sale_date"]),
                    "sale_amount":     r["sale_amount"],
                    "pnl":             r["pnl"],
                    "st_pnl":          r.get("st_pnl"),
                    "lt_pnl":          r.get("lt_pnl"),
                    "return_pct":      r["return_pct"],
                })

        # Sold properties from the register (own holder universe — companies /
        # trusts). purchase_price, when recorded, gives a real pnl; without it
        # only the sale proceeds are shown. Members see just the holders
        # mapping to their entity; admins see every holder.
        today = date.today()
        fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        cur = conn.cursor()
        cur.execute("""
            SELECT p.name, p.sale_date, p.sale_price, p.purchase_price,
                   pe.name AS holder_name, e.id AS sys_entity_id
            FROM property p
            JOIN property_entity pe ON pe.id = p.holder_id
            LEFT JOIN entity e ON e.entity_name = pe.name
            WHERE p.sold
            ORDER BY p.sale_date DESC NULLS LAST
        """)
        for r in cur.fetchall():
            if eid is not None and r["sys_entity_id"] != eid:
                continue
            if not since_inception and (r["sale_date"] is None or r["sale_date"] < fy_start):
                continue
            sale = float(r["sale_price"]) if r["sale_price"] is not None else None
            cost = float(r["purchase_price"]) if r["purchase_price"] is not None else None
            pnl  = (sale - cost) if sale is not None and cost is not None else None
            out.append({
                "entity":          r["holder_name"],
                "category":        "Real Estate",
                "group":           "Real Estate",
                "security_name":   r["name"],
                "purchase_amount": cost,
                "sale_date":       r["sale_date"].isoformat() if r["sale_date"] else "",
                "sale_amount":     sale,
                "pnl":             pnl,
                "st_pnl":          None,
                "lt_pnl":          None,
                "return_pct":      (pnl / cost) if pnl is not None and cost else None,
            })
        cur.close()
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
        # Admin → all reports; member → only their own entity's reports. Combined /
        # master workbooks span every entity and have entity_id NULL, so filtering
        # by entity_id also keeps those firm-wide files out of a member's list.
        eid = _resolve_entity(cur, payload, None)
        where  = "WHERE r.entity_id = %s" if eid is not None else ""
        params = [eid] if eid is not None else []
        cur.execute(f"""
            SELECT r.id, r.report_type, r.entity_name, r.filename,
                   r.as_of_date, r.generated_at, u.full_name AS generated_by_name
            FROM generated_report r
            LEFT JOIN users u ON u.id = r.generated_by
            {where}
            ORDER BY r.generated_at DESC
            LIMIT 100
        """, params)
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
        # A member may only download their own entity's report; admin downloads any.
        # Combined/master files have entity_id NULL, so a member's entity_id filter
        # returns no row (404) for them — no cross-entity leak.
        eid = _resolve_entity(cur, payload, None)
        if eid is None:
            cur.execute("SELECT filepath, filename FROM generated_report WHERE id = %s", (report_id,))
        else:
            cur.execute("SELECT filepath, filename FROM generated_report WHERE id = %s AND entity_id = %s",
                        (report_id, eid))
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
# Zerodha order-update postback (webhook) — a second, fill-capturing path that
# complements the KiteTicker WS in live_trade_daemon. Both write the SAME
# source_ref (`zerodha:live:{order_id}`), so whichever arrives first wins and the
# other dedupes to a no-op — belt-and-suspenders with no double counting.
# ---------------------------------------------------------------------------

# Entity names whose Zerodha creds live in .env (ZERODHA_<prefix>_*). Resolved via
# zerodha._env so the same _ENV_PREFIX mapping the daemon uses applies here too.
_ZERODHA_PB_ENTITIES = ["DHR", "HHR", "SDR", "Rajani Corp", "HDR"]
_zerodha_pb_routes_cache: Optional[dict] = None


def _zerodha_pb_routes() -> dict:
    """Map Zerodha user_id (client_id) -> {entity_name, api_secret}, built once from
    env. Kite postbacks carry user_id but not api_key, so we route by user_id and
    verify each with that account's own api_secret (per-account signing)."""
    global _zerodha_pb_routes_cache
    if _zerodha_pb_routes_cache is not None:
        return _zerodha_pb_routes_cache
    from equity.brokers import zerodha as z
    routes = {}
    for name in _ZERODHA_PB_ENTITIES:
        try:
            uid = z._env(name, "CLIENT_ID")
            sec = z._env(name, "API_SECRET")
        except KeyError:
            continue  # entity not configured for Zerodha — skip
        routes[uid] = {"entity_name": name, "api_secret": sec}
    _zerodha_pb_routes_cache = routes
    return routes


@app.post("/api/v1/zerodha/postback")
@limiter.limit("300/minute")
async def zerodha_postback(request: Request):
    """
    Kite Connect order-update postback. Kite POSTs a JSON order object on every
    status change, signed with checksum = SHA-256(order_id + order_timestamp +
    api_secret). We route by user_id, verify the checksum with that account's
    secret, and on a COMPLETE fill write it to stock_transaction (deduped with the
    KiteTicker WS path on source_ref) and publish it to the live_trades SSE channel.

    Public by necessity — Kite sends no auth header, so the per-account checksum IS
    the authentication. An unroutable user_id or a bad checksum is rejected before
    any DB work.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return {"status": "ignored"}

    user_id  = str(body.get("user_id") or "")
    order_id = str(body.get("order_id") or "")
    ots      = str(body.get("order_timestamp") or "")
    checksum = str(body.get("checksum") or "")
    status   = (body.get("status") or "").upper()

    route = _zerodha_pb_routes().get(user_id)
    if not route:
        logger.warning(f"Zerodha postback: unknown user_id (order {order_id}) — ignored")
        return {"status": "ignored"}  # 200 so Kite doesn't retry a mis-routed event

    expected = hashlib.sha256(f"{order_id}{ots}{route['api_secret']}".encode()).hexdigest()
    if not hmac.compare_digest(expected, checksum):
        logger.warning(f"Zerodha postback checksum mismatch for {route['entity_name']} order {order_id}")
        raise HTTPException(status_code=401, detail="bad checksum")

    if status != "COMPLETE":
        logger.info(f"Zerodha postback {route['entity_name']} order {order_id} status={status} — no fill to record")
        return {"status": "ok"}

    symbol   = body.get("tradingsymbol")
    side     = (body.get("transaction_type") or "").upper()
    qty      = float(body.get("filled_quantity") or 0)
    price    = float(body.get("average_price") or 0)
    exchange = body.get("exchange")
    if side not in ("BUY", "SELL") or qty <= 0 or price <= 0 or not order_id:
        logger.warning(f"Zerodha postback incomplete fill {route['entity_name']} order {order_id}: "
                       f"side={side} qty={qty} price={price}")
        return {"status": "ok"}

    # Trade date from the exchange (fall back to order) timestamp, else today.
    ex_ts = body.get("exchange_timestamp") or ots
    tdate = None
    if isinstance(ex_ts, str) and len(ex_ts) >= 10:
        try:
            tdate = datetime.strptime(ex_ts[:19], "%Y-%m-%d %H:%M:%S").date()
        except ValueError:
            tdate = None
    if tdate is None:
        tdate = date.today()

    sref = f"zerodha:live:{order_id}"
    conn = None
    entity_id = None
    amount = qty * price
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM entity WHERE entity_name = %s", (route["entity_name"],))
        erow = cur.fetchone()
        if not erow:
            logger.error(f"Zerodha postback: entity '{route['entity_name']}' not found")
            return {"status": "ok"}
        entity_id = erow["id"]

        cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (sref,))
        if cur.fetchone():
            logger.info(f"Zerodha postback dup {sref} — already recorded (WS or replay)")
            return {"status": "ok"}

        from workers.import_tradebooks_multi import get_or_create_security
        sec_id = get_or_create_security(cur, None, symbol, exchange, "INR", True)
        cur.execute(
            """INSERT INTO stock_transaction
               (entity_id, security_id, transaction_date, transaction_type, quantity,
                price, amount, amount_inr, currency, exchange, source, source_ref, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,'zerodha',%s,NOW())""",
            (entity_id, sec_id, tdate, side, qty, price, amount, amount, exchange, sref),
        )
        conn.commit()
    except HTTPException:
        raise
    except psycopg2.errors.UniqueViolation:
        # Benign: the WS daemon won the race and already wrote this exact fill (the
        # composite unique index rejects the duplicate). Not an error.
        if conn:
            conn.rollback()
        logger.info(f"Zerodha postback {sref} already recorded (dedup race with WS) — ok")
        return {"status": "ok"}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Zerodha postback DB error for {sref}: {e}")
        return {"status": "ok"}  # logged; 200 so Kite doesn't hammer retries
    finally:
        release_db_connection(conn)

    # Publish to the live SSE channel in the same shape live_trade_daemon uses.
    if redis_client is not None:
        try:
            redis_client.publish(LIVE_TRADES_CHANNEL, json.dumps({
                "entity": route["entity_name"], "entity_id": entity_id, "broker": "zerodha",
                "symbol": symbol, "side": side, "qty": qty, "price": price,
                "amount": round(amount, 2), "date": str(tdate),
                "ts": ex_ts if isinstance(ex_ts, str) else None, "order_id": order_id,
            }))
        except Exception as e:
            logger.error(f"Zerodha postback redis publish failed for {sref}: {e}")

    logger.info(f"Zerodha postback FILL {route['entity_name']} {side} {symbol} {qty} @ {price} "
                f"(order {order_id})  [recorded + published]")
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
    Assistant scope: always the whole family. Per-entity scoping was removed
    (owner decision 2026-07-16) — the assistant now answers over ALL entities'
    data regardless of any requested entity_id, matching the uniform data-access
    model (the only member restrictions are Manual Data + user management).
    """
    return None  # always whole-family; requested_entity_id intentionally ignored


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


# ---------------------------------------------------------------------------
# Live trades — SSE stream of real-time broker fills (published by live_trade_daemon)
# ---------------------------------------------------------------------------
LIVE_TRADES_CHANNEL = "live_trades"


@app.get("/api/v1/live/trades")
def stream_live_trades(request: Request, authorization: Optional[str] = Header(None)):
    """Server-Sent Events stream of live order fills.

    The live_trade_daemon publishes each fill (the instant the broker's order-update
    WebSocket reports it) to the Redis `live_trades` channel; this relays them to the
    browser's EventSource with sub-second latency. Auth is the normal token — an
    EventSource sends the httponly cookie same-origin. Members see all entities (role
    model), so there is no server-side entity filter; the UI narrows to the selected
    entity itself.
    """
    _require_auth(request, authorization)

    def event_stream():
        if redis_client is None:
            yield f"data: {json.dumps({'type': 'error', 'message': 'live feed unavailable'})}\n\n"
            return
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(LIVE_TRADES_CHANNEL)
        try:
            # Open the stream immediately so the client's onopen fires without waiting
            # for the first fill; SSE comment lines (":" prefix) are ignored by clients.
            yield ": connected\n\n"
            while True:
                # Blocking read in a threadpool (sync generator) — does not stall the
                # event loop. The 15s timeout doubles as the keepalive cadence.
                msg = pubsub.get_message(timeout=15.0)
                if msg and msg.get("type") == "message":
                    yield f"data: {msg['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass  # client disconnected — Starlette closes the generator
        finally:
            try:
                pubsub.unsubscribe(LIVE_TRADES_CHANNEL)
                pubsub.close()
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Manual trade register — "our own tradebook"
#
# Admin-entered trades/corporate actions that broker tradebooks and API feeds
# never carry: demat transfers, demerger/IPO/bonus/rights allotments, off-market
# deals, and recent trades whose tradebook export hasn't been uploaded yet.
#
# Rows land in stock_transaction with source='manual' and the broker column set,
# so they flow into the SAME pipelines as imported tradebooks:
#   - equity_txn_metrics_worker merges them with the broker's imported history
#     (FIFO lots -> XIRR/CAGR/YTD/first_invested_date)
#   - report_generator's realised-gains FIFO picks them up automatically
#     (it reads all INR stock_transaction rows)
#
# IMPORTANT: do not re-enter trades that a broker tradebook import already
# covers — the quantity reconstruction check will fail and metrics degrade.
# ---------------------------------------------------------------------------

MANUAL_TRADE_BUY_KINDS  = {"buy", "transfer_in", "demerger", "ipo", "bonus", "rights"}
MANUAL_TRADE_SELL_KINDS = {"sell", "transfer_out"}
MANUAL_TRADE_BROKERS    = {"zerodha", "angel_one", "dhan", "other"}


class ManualTradeRequest(BaseModel):
    entity_id: int
    broker: str                      # zerodha | angel_one | dhan | other
    symbol: str = Field(min_length=1, max_length=40)
    isin: Optional[str] = None       # required only if the symbol can't be resolved
    kind: str                        # buy|sell|transfer_in|transfer_out|demerger|ipo|bonus|rights
    trade_date: date
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)       # per-share INR; 0 allowed for bonus/demerger allotments
    notes: Optional[str] = Field(default=None, max_length=500)


def _resolve_manual_security(cursor, entity_id: int, symbol: str, isin: Optional[str]):
    """security_master id for a manual trade. Resolution order: explicit ISIN ->
    entity's own equity_holding symbol (any broker) -> security_master by name.
    Creates the security row when an ISIN is given but unknown. Returns
    (security_id, isin) or raises HTTPException(400) asking for the ISIN."""
    sym = symbol.strip().upper()
    if isin:
        isin = isin.strip().upper()
        if not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
            raise HTTPException(status_code=400, detail=f"'{isin}' is not a valid ISIN (INxxxxxxxxxx)")
        cursor.execute("SELECT id, isin FROM security_master WHERE isin = %s", (isin,))
        row = cursor.fetchone()
        if row:
            return row["id"], row["isin"]
        cursor.execute(
            """INSERT INTO security_master (security_name, isin, security_type, currency, exchange)
               VALUES (%s, %s, 'EQUITY', 'INR', 'NSE') RETURNING id""",
            (sym, isin),
        )
        return cursor.fetchone()["id"], isin
    # no ISIN given: try the entity's holdings, then security_master by name
    cursor.execute(
        """SELECT isin FROM equity_holding
           WHERE entity_id = %s AND isin IS NOT NULL
             AND UPPER(REGEXP_REPLACE(symbol, '-(EQ|ST|SM|BE|BZ|GB)$', '')) =
                 REGEXP_REPLACE(%s, '-(EQ|ST|SM|BE|BZ|GB)$', '')
           LIMIT 1""",
        (entity_id, sym),
    )
    row = cursor.fetchone()
    if row:
        return _resolve_manual_security(cursor, entity_id, sym, row["isin"])
    cursor.execute(
        "SELECT id, isin FROM security_master WHERE UPPER(security_name) = %s AND isin IS NOT NULL LIMIT 1",
        (sym,),
    )
    row = cursor.fetchone()
    if row:
        return row["id"], row["isin"]
    raise HTTPException(
        status_code=400,
        detail=f"Cannot resolve '{sym}' to an ISIN — not in this entity's holdings. Please supply the ISIN.",
    )


@app.get("/api/v1/manual-trades")
def list_manual_trades(request: Request, entity_id: Optional[int] = None,
                       authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        if _live_role(cursor, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        where, params = "st.source = 'manual'", []
        if entity_id:
            where += " AND st.entity_id = %s"; params.append(entity_id)
        cursor.execute(f"""
            SELECT st.id, st.entity_id, e.entity_name, st.broker,
                   sm.security_name AS symbol, sm.isin,
                   st.transaction_date, st.transaction_type, st.quantity, st.price,
                   st.amount, st.notes, st.created_at
            FROM stock_transaction st
            JOIN entity e ON e.id = st.entity_id
            JOIN security_master sm ON sm.id = st.security_id
            WHERE {where}
            ORDER BY st.transaction_date DESC, st.id DESC""", params)
        return {"trades": [dict(r) for r in cursor.fetchall()]}
    finally:
        release_db_connection(conn)


@app.post("/api/v1/manual-trades")
@limiter.limit("30/minute")
def add_manual_trade(request: Request, body: ManualTradeRequest,
                     authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        if _live_role(cursor, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin only")

        kind = body.kind.strip().lower()
        if kind not in MANUAL_TRADE_BUY_KINDS | MANUAL_TRADE_SELL_KINDS:
            raise HTTPException(status_code=400, detail=f"Unknown kind '{kind}'")
        broker = body.broker.strip().lower()
        if broker not in MANUAL_TRADE_BROKERS:
            raise HTTPException(status_code=400, detail=f"Unknown broker '{broker}'")
        if body.trade_date > date.today():
            raise HTTPException(status_code=400, detail="trade_date is in the future")
        cursor.execute("SELECT 1 FROM entity WHERE id = %s", (body.entity_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Entity not found")

        sec_id, isin = _resolve_manual_security(cursor, body.entity_id, body.symbol, body.isin)
        side = "BUY" if kind in MANUAL_TRADE_BUY_KINDS else "SELL"
        note = f"[{kind}]" + (f" {body.notes.strip()}" if body.notes and body.notes.strip() else "")
        ref = f"manual:{uuid.uuid4().hex[:16]}"
        cursor.execute(
            """INSERT INTO stock_transaction
                 (entity_id, security_id, transaction_date, transaction_type,
                  quantity, price, amount, currency, source, source_ref, broker, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'INR','manual',%s,%s,%s)
               RETURNING id""",
            (body.entity_id, sec_id, body.trade_date, side,
             body.quantity, body.price, round(body.quantity * body.price, 2),
             ref, broker, note),
        )
        new_id = cursor.fetchone()["id"]
        conn.commit()
        logger.info(f"Manual trade #{new_id} added by {payload['email']}: "
                    f"entity={body.entity_id} {broker} {side} {body.quantity} x {body.symbol} ({isin}) [{kind}]")
        return {"id": new_id, "isin": isin, "side": side,
                "message": "Trade recorded. Metrics refresh on the next worker run (or within the day via the intraday runs)."}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"add_manual_trade failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to record trade")
    finally:
        release_db_connection(conn)


@app.delete("/api/v1/manual-trades/{trade_id}")
def delete_manual_trade(trade_id: int, request: Request,
                        authorization: Optional[str] = Header(None)):
    conn = None
    try:
        payload = _require_auth(request, authorization)
        conn = get_db_connection()
        cursor = conn.cursor()
        if _live_role(cursor, payload["email"]) != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        cursor.execute("DELETE FROM stock_transaction WHERE id = %s AND source = 'manual' RETURNING id",
                       (trade_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Manual trade not found (imported rows cannot be deleted here)")
        conn.commit()
        logger.info(f"Manual trade #{trade_id} deleted by {payload['email']}")
        return {"deleted": trade_id}
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    finally:
        release_db_connection(conn)
