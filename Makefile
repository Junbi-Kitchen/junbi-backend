dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	dbmate up

seed:
	python scripts/seed/seed_usda.py
	python scripts/seed/seed_mock.py
