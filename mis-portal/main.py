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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
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
        logger.error(f"Audit log write failed: {e}")

def is_token_blacklisted(token: str) -> bool:
    try:
        if redis_client is None:
            return False
        return redis_client.exists(f"blacklist:{token}") > 0
    except Exception as e:
        logger.error(f"Redis blacklist check failed: {e}")
        return False

def blacklist_token(token: str, expiry_seconds: int = 2592000):
    try:
        if redis_client is None:
            logger.warning("Redis unavailable - token not blacklisted")
            return
        redis_client.setex(f"blacklist:{token}", expiry_seconds, "revoked")
    except Exception as e:
        logger.error(f"Redis blacklist set failed: {e}")

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
    password: str = Field(min_length=1, max_length=255)

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

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

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
                status_code=423,
                detail=f"Account locked until {user_row['locked_until'].strftime('%H:%M UTC')}. Try again later."
            )

        if not verify_password(password, user_row["password_hash"]):
            new_attempts = (user_row.get("failed_attempts") or 0) + 1
            lock_until = None

            if new_attempts >= 10:
                lock_until = datetime.utcnow() + timedelta(minutes=30)
                logger.warning(f"Account locked: {email} after {new_attempts} attempts")

            cursor2 = conn.cursor()
            cursor2.execute(
                "UPDATE users SET failed_attempts = %s, locked_until = %s WHERE id = %s",
                (new_attempts, lock_until, user_row["id"])
            )
            conn.commit()
            cursor2.close()

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
            "exp": datetime.utcnow() + timedelta(days=30)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        logger.info(f"Successful login: {email}")

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=2592000
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
        blacklist_token(token)
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
