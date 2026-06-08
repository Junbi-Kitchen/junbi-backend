import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

import firebase_admin
from firebase_admin import credentials

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from app.db import open_pool, close_pool, get_async_pool
from app.agents.ingredient_resolver.resources import set_pool_provider
from app.api.routes import users, recipes, pantry, grocery, collections, stores, orders, savings, agents

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


if settings.FIREBASE_SERVICE_ACCOUNT_KEY:
    logger.info("Firebase: initializing with service account key")
    cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_KEY)
    firebase_admin.initialize_app(cred, options={"projectId": settings.FIREBASE_PROJECT_ID})
else:
    logger.info("Firebase: initializing with ADC (Cloud Run / GCP environment expected)")
    firebase_admin.initialize_app(options={"projectId": settings.FIREBASE_PROJECT_ID})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    # Let the in-process agent reuse the app's pool instead of opening its own.
    set_pool_provider(get_async_pool)
    yield
    await close_pool()


app = FastAPI(
    title="Gook Backend",
    description="API for Gook — Grocery and Cooking",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(recipes.router)
app.include_router(pantry.router)
app.include_router(grocery.router)
app.include_router(collections.router)
app.include_router(stores.router)
app.include_router(orders.router)
app.include_router(savings.router)
app.include_router(agents.router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
