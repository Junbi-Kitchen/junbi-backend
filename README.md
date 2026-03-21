# Gook Backend

FastAPI backend for Gook — Grocery and Cooking app.

## Prerequisites

- Python 3.13 ([pyenv](https://github.com/pyenv/pyenv) recommended)

## Local Development Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd gook-backend
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
JWT_SECRET_KEY=your-secret-key-here
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=30
GOOGLE_CLIENT_ID=your-google-client-id  # required for Google SSO
```

To generate a secure `JWT_SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5. Run the development server

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`.

## API Docs

Once running, interactive docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Health Check

```bash
curl http://localhost:8000/health
```

## Project Structure

```
gook-backend/
├── app/
│   ├── api/
│   │   └── routes/        # Route handlers (auth, users, recipes, etc.)
│   ├── core/              # Security, dependencies
│   ├── data/              # Mock data
│   ├── schemas/           # Pydantic schemas
│   ├── services/          # Business logic
│   └── main.py            # App entry point
├── config.py              # Settings (loaded from .env)
└── requirements.txt
```
