import os

from dotenv import load_dotenv

# Real local config first: .env (if present) populates the environment, so
# integration tests can reach the actual dev database.
load_dotenv()

# CI / keyless fallbacks so app modules remain importable with no .env at all.
# setdefault never overrides anything .env provided.
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("DATABASE_URL", "sqlite:///test.db")
