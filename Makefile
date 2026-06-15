VENV := venv/bin

dev:
	$(VENV)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	dbmate up

seed:
	$(VENV)/python scripts/seed/seed_usda.py
	$(VENV)/python scripts/seed/seed_mock.py

seed-catalog:
	$(VENV)/python scripts/seed/seed_recipe_catalog.py
	$(VENV)/python scripts/seed/seed_demo_pantry.py

adk:
	$(VENV)/adk web app/agents/
