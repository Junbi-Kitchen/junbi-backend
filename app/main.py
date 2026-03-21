from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, users, recipes, pantry, grocery, collections, stores, orders

app = FastAPI(
    title="Gook Backend",
    description="API for Gook — Grocery and Cooking",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(recipes.router)
app.include_router(pantry.router)
app.include_router(grocery.router)
app.include_router(collections.router)
app.include_router(stores.router)
app.include_router(orders.router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
