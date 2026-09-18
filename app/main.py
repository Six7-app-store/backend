import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import (
    admin_apps,
    apps,
    auth_keycloak,
    courses,
    dashboard,
    deployments,
    lti,
    openstack_credentials,
    openstack_resources,
    quotas,
    tasks,
    teams,
    users,
)
from app.services.celery_event_listener import start_event_listener
from app.services.deployment_pubsub import pubsub
from app.services.reconciler import run_reconciler

logger = logging.getLogger(__name__)


# ``DISABLE_BACKGROUND_TASKS`` short-circuits the lifespan body so the
# app is fully wired but the Celery listener and reconciler are not
# started. Used by the test suite, where per-TestClient lifespans would
# otherwise stack daemon threads and exhaust the DB connection pool.
def _background_tasks_disabled() -> bool:
    return os.getenv("DISABLE_BACKGROUND_TASKS", "").lower() in ("1", "true", "yes")


# ----------------------------------------------------------------
# STARTUP/SHUTDOWN
# ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("=== Application Starting ===")
    logger.info("ℹ️  Use 'alembic upgrade head' to apply database migrations")

    if _background_tasks_disabled():
        # Test path: keep ``app`` fully functional but skip the Celery
        # listener + reconciler.
        logger.info(
            "DISABLE_BACKGROUND_TASKS set — skipping Celery listener "
            "and reconciler (test mode)"
        )
        try:
            yield
        finally:
            logger.info("=== Application Shutting Down (test mode) ===")
        return

    # Bind the FastAPI event loop to the deployment pubsub *before*
    # spawning the Celery listener. The listener thread pushes into
    # the pubsub from a non-asyncio thread; without a loop reference
    # those pushes would be silently dropped.
    pubsub.set_loop(asyncio.get_running_loop())
    logger.info("Deployment pubsub bound to event loop")

    # Start Celery event listener in background thread
    listener_thread = threading.Thread(target=start_event_listener, daemon=True)
    listener_thread.start()
    logger.info("Celery event listener started in background")

    # Reconciler is the safety net for events the listener missed (lost
    # event, backend restart during dispatch, broker hiccups). It runs
    # as an asyncio task so we can cancel it cleanly on shutdown.
    reconciler_task = asyncio.create_task(run_reconciler())
    logger.info("Reconciler loop scheduled")

    logger.info("Application started")

    try:
        yield
    finally:
        # Shutdown
        logger.info("=== Application Shutting Down ===")
        reconciler_task.cancel()
        try:
            await reconciler_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Reconciler task raised on shutdown")
        logger.info("Shutdown complete")


# ----------------------------------------------------------------
# FASTAPI APP
# ----------------------------------------------------------------
app = FastAPI(
    title="Backend API",
    description="FastAPI Backend with Auth, Git & Celery Integration",
    version="1.0.0",
    lifespan=lifespan
)

# ----------------------------------------------------------------
# CORS
# ----------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------
# ROUTERS
# ----------------------------------------------------------------
app.include_router(auth_keycloak.router, prefix="/auth", tags=["Authentication"])
# LTI 1.3 launch endpoints. Unauthenticated by design — they are the
# entry point from Moodle and carry their own proof in the id_token.
app.include_router(lti.router, prefix="/lti", tags=["LTI"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(courses.router, prefix="/courses", tags=["Courses"])
app.include_router(apps.router, prefix="/apps", tags=["Apps"])
app.include_router(admin_apps.router, prefix="/admin", tags=["Admin"])
app.include_router(deployments.router, prefix="/deployments", tags=["Deployments"])
app.include_router(tasks.router, prefix="/tasks", tags=["Tasks"])
app.include_router(teams.router, prefix="/teams", tags=["Teams"])
app.include_router(quotas.router, prefix="/quotas", tags=["Quotas"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(openstack_credentials.router, tags=["OpenStack Credentials"])
# Read API for OpenStack resources (Networks, Flavors, Images, ...),
# used by the wizard's value-help dropdowns so users don't have to type
# UUIDs from Horizon.
app.include_router(
    openstack_resources.router,
    prefix="/me/openstack/resources",
    tags=["OpenStack Resources"],
)


# ----------------------------------------------------------------
# HEALTH CHECK
# ----------------------------------------------------------------
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "backend-api",
        "version": "1.0.0"
    }
