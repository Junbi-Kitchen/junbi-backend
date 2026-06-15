-- migrate:up

-- ============================================================
-- MEAL PLANNING LOOP (final strategy: docs/meal-planning-final-strategy.md)
-- Bundle output, signal log, three-layer user model
-- ============================================================

-- Bundle shape: meals are a committed set; days are a soft suggestion
ALTER TABLE meal_plan_items RENAME COLUMN day_of_week TO suggested_day;
ALTER TABLE meal_plan_items ALTER COLUMN suggested_day DROP NOT NULL;
ALTER TABLE meal_plan_items
    ADD COLUMN is_probe      BOOLEAN DEFAULT false,
    ADD COLUMN alternates    JSONB DEFAULT '[]',
    ADD COLUMN rationale     TEXT,
    ADD COLUMN added_via     TEXT DEFAULT 'planner',   -- planner | user_swap | midweek_swap
    ADD COLUMN computed_cost DECIMAL(10,2);

-- The week's ranking artifact persists: it powers alternates/swaps/trades all week
ALTER TABLE meal_plans
    ADD COLUMN ranking_artifact JSONB,                 -- {ranking, pairing_cautions, rationales}
    ADD COLUMN computed_cost    DECIMAL(10,2),
    ADD COLUMN budget_trade     JSONB,                 -- {recipe_id, delta, taken}
    ADD COLUMN closed_at        TIMESTAMPTZ;

-- Append-only signal log — source of truth; learned state is a rebuildable projection
CREATE TABLE plan_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    meal_plan_id UUID REFERENCES meal_plans(id) ON DELETE SET NULL,
    recipe_id    UUID REFERENCES recipes(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL,
    -- swipe_like | swipe_pass | plan_accepted | item_swapped_out | item_swapped_in
    -- | never_show | budget_trade_taken | budget_trade_declined | recipe_opened
    -- | marked_cooked | rated_up | rated_down | midweek_swap | week_closed
    is_probe     BOOLEAN DEFAULT false,
    payload      JSONB,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_plan_events_user_created ON plan_events (user_id, created_at);

-- L2: learned per-recipe affinity (code-owned; rebuildable from plan_events)
CREATE TABLE user_recipe_affinity (
    user_id        TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    recipe_id      UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    score          DECIMAL(6,3) NOT NULL DEFAULT 0,
    confidence     DECIMAL(4,3) NOT NULL DEFAULT 0,
    last_signal_at TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, recipe_id)
);

-- L2: learned scalar features (repetition_tolerance, variety_appetite, budget_strictness, ...)
CREATE TABLE user_model_features (
    user_id     TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    feature_key TEXT NOT NULL,
    value       DECIMAL(8,3) NOT NULL,
    confidence  DECIMAL(4,3) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, feature_key)
);

-- L3: versioned LLM-written narrative (no numbers, no constraints; rollback = previous version)
CREATE TABLE user_model_narratives (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    version    INT NOT NULL,
    narrative  TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (user_id, version)
);

-- Audit trail for "reflection proposes, code applies" (bounded deltas only)
CREATE TABLE model_update_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,                         -- stats | reflection
    feature_key TEXT NOT NULL,
    delta       DECIMAL(8,3) NOT NULL,
    evidence    TEXT,
    applied_at  TIMESTAMPTZ DEFAULT now()
);

-- Recipe embeddings for affinity priors on unseen recipes (same model as ingredients: MiniLM 384)
ALTER TABLE recipes ADD COLUMN embedding vector(384);
CREATE INDEX ON recipes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Base prices for cost math (seeded synthetically; Kroger refresh later)
ALTER TABLE ingredients
    ADD COLUMN estimated_price DECIMAL(8,2),
    ADD COLUMN price_unit      TEXT;

-- migrate:down

ALTER TABLE ingredients DROP COLUMN price_unit, DROP COLUMN estimated_price;
ALTER TABLE recipes DROP COLUMN embedding;
DROP TABLE model_update_log;
DROP TABLE user_model_narratives;
DROP TABLE user_model_features;
DROP TABLE user_recipe_affinity;
DROP TABLE plan_events;
ALTER TABLE meal_plans
    DROP COLUMN closed_at,
    DROP COLUMN budget_trade,
    DROP COLUMN computed_cost,
    DROP COLUMN ranking_artifact;
ALTER TABLE meal_plan_items
    DROP COLUMN computed_cost,
    DROP COLUMN added_via,
    DROP COLUMN rationale,
    DROP COLUMN alternates,
    DROP COLUMN is_probe;
ALTER TABLE meal_plan_items ALTER COLUMN suggested_day SET NOT NULL;
ALTER TABLE meal_plan_items RENAME COLUMN suggested_day TO day_of_week;
