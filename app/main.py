from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import food, users

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

app.include_router(food.router)
app.include_router(users.router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}