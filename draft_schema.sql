-- ============================================================
-- EXTENSIONS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE storage_location AS ENUM (
    'fridge', 'freezer', 'pantry'
);

CREATE TYPE added_via AS ENUM (
    'manual', 'receipt', 'scan', 'bulk'
);

CREATE TYPE recipe_difficulty AS ENUM (
    'easy', 'medium', 'hard'
);

CREATE TYPE social_platform AS ENUM (
    'instagram', 'tiktok', 'youtube', 'other'
);

CREATE TYPE order_status AS ENUM (
    'placed', 'preparing', 'ready', 'picked_up', 'cancelled'
);

CREATE TYPE receipt_status AS ENUM (
    'pending', 'confirmed', 'failed'
);

CREATE TYPE interaction_action AS ENUM (
    'saved', 'skipped', 'cooked'
);

-- ============================================================
-- USERS & AUTH
-- ============================================================

CREATE TABLE user_profiles (
    id                  TEXT PRIMARY KEY,
    email               TEXT NOT NULL,
    display_name        TEXT,
    avatar_url          TEXT,
    timezone            TEXT DEFAULT 'America/New_York',
    onboarding_complete BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE user_preferences (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    dietary_tags    TEXT[] DEFAULT '{}',
    allergies       TEXT[] DEFAULT '{}',
    cuisines        TEXT[] DEFAULT '{}',
    dislikes        TEXT[],
    household_size  INT DEFAULT 1,
    weekly_budget   DECIMAL(10,2),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE user_addresses (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    label       TEXT,
    street      TEXT,
    city        TEXT,
    state       TEXT,
    zip         TEXT,
    is_default  BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE connected_accounts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    provider         TEXT NOT NULL,
    handle           TEXT,
    provider_user_id TEXT,
    access_token     TEXT,
    refresh_token    TEXT,
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, provider)
);

-- ============================================================
-- INGREDIENTS
-- ============================================================

-- Must be created before ingredients (FK dependency)
CREATE TABLE ingredient_categories (
    id         SERIAL PRIMARY KEY,
    slug       TEXT UNIQUE NOT NULL,   -- e.g. 'produce', 'proteins'
    name       TEXT NOT NULL,          -- e.g. 'Produce', 'Meat & Seafood'
    usda_code  TEXT,                   -- USDA FoodData Central food group code
    usda_name  TEXT,                   -- USDA FoodData Central food group name
    icon       TEXT,
    sort_order INT DEFAULT 0
);

CREATE TABLE ingredients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    category_id     INT REFERENCES ingredient_categories(id),
    default_unit    TEXT,
    gram_weight     DECIMAL(10,2),
    embedding       vector(384),
    usda_fdc_id     TEXT,
    off_barcode     TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE ingredient_aliases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingredient_id   UUID REFERENCES ingredients(id) ON DELETE CASCADE,
    alias           TEXT UNIQUE NOT NULL
);

CREATE TABLE nutrients (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usda_id   INT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    unit      TEXT NOT NULL,
    rank      INT
);

CREATE TABLE ingredient_nutrients (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ingredient_id UUID REFERENCES ingredients(id) ON DELETE CASCADE,
    nutrient_id   UUID REFERENCES nutrients(id) ON DELETE CASCADE,
    amount        DECIMAL(10,4) NOT NULL,
    UNIQUE(ingredient_id, nutrient_id)
);

-- ============================================================
-- RECIPES
-- ============================================================

CREATE TABLE recipes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    description     TEXT,
    blurhash        TEXT,
    prep_time_mins  INT,
    cook_time_mins  INT,
    servings        INT DEFAULT 4,
    difficulty      recipe_difficulty,
    cuisine         TEXT,
    source_url      TEXT,
    source_platform social_platform,
    source_type     TEXT,
    creator_handle  TEXT,
    image_url       TEXT,
    -- Pre-computed nutrition summary (per serving)
    calories        INT,
    protein_g       DECIMAL(6,2),
    carbs_g         DECIMAL(6,2),
    fat_g           DECIMAL(6,2),
    is_global       BOOLEAN DEFAULT false,
    times_cooked    INT DEFAULT 0,
    last_cooked_at  TIMESTAMPTZ,
    created_by      TEXT REFERENCES user_profiles(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE recipe_ingredients (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipe_id     UUID REFERENCES recipes(id) ON DELETE CASCADE,
    ingredient_id UUID REFERENCES ingredients(id),
    quantity      DECIMAL(10,2),
    unit          TEXT,
    is_optional   BOOLEAN DEFAULT false,
    display_text  TEXT,   -- free-text fallback when ingredient_id is null
    order_index   INT
);

CREATE TABLE recipe_steps (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipe_id        UUID REFERENCES recipes(id) ON DELETE CASCADE,
    step_number      INT NOT NULL,
    instruction      TEXT NOT NULL,
    duration_minutes INT,
    photo_url        TEXT
);

CREATE TABLE recipe_tags (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipe_id UUID REFERENCES recipes(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    UNIQUE(recipe_id, tag)
);

-- ============================================================
-- USER RECIPE INTERACTIONS
-- ============================================================

CREATE TABLE user_recipe_interactions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    recipe_id  UUID REFERENCES recipes(id) ON DELETE CASCADE,
    action     interaction_action NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, recipe_id, action)
);

CREATE TABLE collections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    cover_photo_url TEXT,
    emoji           TEXT DEFAULT '📁',
    is_default      BOOLEAN DEFAULT false,
    sort_order      INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE collection_recipes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id UUID REFERENCES collections(id) ON DELETE CASCADE,
    recipe_id     UUID REFERENCES recipes(id) ON DELETE CASCADE,
    added_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(collection_id, recipe_id)
);

-- ============================================================
-- MEAL PLANS
-- ============================================================

CREATE TABLE meal_plans (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    week_start    DATE NOT NULL,
    status        TEXT DEFAULT 'draft',
    budget_target DECIMAL(10,2),
    budget_actual DECIMAL(10,2),
    generated_by  TEXT DEFAULT 'ai',
    notes         TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, week_start)
);

CREATE TABLE meal_plan_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meal_plan_id UUID REFERENCES meal_plans(id) ON DELETE CASCADE,
    recipe_id    UUID REFERENCES recipes(id) ON DELETE SET NULL,
    day_of_week  INT NOT NULL,
    meal_type    TEXT NOT NULL,
    custom_name  TEXT,
    sort_order   INT DEFAULT 0
);

-- ============================================================
-- PANTRY
-- ============================================================

CREATE TABLE pantry_items (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    ingredient_id    UUID NOT NULL REFERENCES ingredients(id),
    quantity         DECIMAL(10,2),
    unit             TEXT,
    location         storage_location DEFAULT 'pantry',
    expiry_date      DATE,
    emoji            TEXT,
    photo_url        TEXT,
    added_via        added_via DEFAULT 'manual',
    is_active        BOOLEAN DEFAULT true,
    freshness_status TEXT GENERATED ALWAYS AS (
        CASE
            WHEN expiry_date IS NULL THEN 'staple'
            WHEN expiry_date <= CURRENT_DATE THEN 'expired'
            WHEN expiry_date <= CURRENT_DATE + INTERVAL '2 days' THEN 'expiring'
            WHEN expiry_date <= CURRENT_DATE + INTERVAL '6 days' THEN 'use_soon'
            ELSE 'fresh'
        END
    ) STORED,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE receipts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    image_url  TEXT NOT NULL,
    store_name TEXT,
    scanned_at TIMESTAMPTZ DEFAULT now(),
    status     receipt_status DEFAULT 'pending'
);

CREATE TABLE receipt_line_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id    UUID REFERENCES receipts(id) ON DELETE CASCADE,
    raw_text      TEXT NOT NULL,
    ingredient_id UUID REFERENCES ingredients(id),
    quantity      DECIMAL(10,2),
    unit          TEXT,
    price         DECIMAL(10,2)
);

CREATE TABLE waste_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    item_name       TEXT NOT NULL,
    action          TEXT NOT NULL,
    estimated_value DECIMAL(10,2),
    pantry_item_id  UUID,
    recipe_id       UUID REFERENCES recipes(id) ON DELETE SET NULL,
    logged_at       TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- ORDERS & STORES
-- ============================================================

CREATE TABLE stores (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    logo            TEXT,
    address         TEXT,
    latitude        DECIMAL(9,6),
    longitude       DECIMAL(9,6),
    supports_pickup BOOLEAN DEFAULT true,
    is_instacart    BOOLEAN DEFAULT false
);

CREATE TABLE orders (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    store_id     UUID REFERENCES stores(id),
    address_id   UUID REFERENCES user_addresses(id),
    status       order_status DEFAULT 'placed',
    total        DECIMAL(10,2),
    placed_at    TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE order_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id      UUID REFERENCES orders(id) ON DELETE CASCADE,
    ingredient_id UUID REFERENCES ingredients(id),
    quantity      DECIMAL(10,2),
    unit          TEXT,
    price         DECIMAL(10,2)
);

-- ============================================================
-- GROCERY
-- ============================================================

CREATE TABLE grocery_lists (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    name            TEXT DEFAULT 'My List',
    status          TEXT DEFAULT 'active',
    meal_plan_id    UUID REFERENCES meal_plans(id) ON DELETE SET NULL,
    estimated_total DECIMAL(10,2),
    store_name      TEXT,
    order_id        UUID REFERENCES orders(id) ON DELETE SET NULL,
    ordered_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE grocery_items (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    list_id          UUID REFERENCES grocery_lists(id) ON DELETE CASCADE,
    ingredient_id    UUID NOT NULL REFERENCES ingredients(id),
    quantity         DECIMAL(10,2),
    unit             TEXT,
    is_checked       BOOLEAN DEFAULT false,
    is_in_pantry     BOOLEAN DEFAULT false,
    estimated_price  DECIMAL(10,2),
    aisle            TEXT,
    sort_order       INT DEFAULT 0,
    source_recipe_id UUID REFERENCES recipes(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- AI CHAT HISTORY
-- ============================================================

CREATE TABLE chat_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    JSONB,
    model_used  TEXT,
    tokens_used INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- PRIORITY CARDS
-- ============================================================

CREATE TABLE priority_cards (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT REFERENCES user_profiles(id) ON DELETE CASCADE,
    card_type    TEXT NOT NULL,
    title        TEXT NOT NULL,
    subtitle     TEXT,
    priority     INT DEFAULT 0,
    data         JSONB,
    is_dismissed BOOLEAN DEFAULT false,
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Ingredient categories
CREATE INDEX ON ingredient_categories(slug);

-- Ingredients
CREATE INDEX ON ingredients USING gin(to_tsvector('english', name));
CREATE INDEX ON ingredients USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON ingredient_aliases(alias);
CREATE INDEX ON ingredients(usda_fdc_id);
CREATE INDEX ON ingredients(off_barcode);
CREATE INDEX ON ingredients(category_id);

-- Nutrients
CREATE INDEX ON ingredient_nutrients(ingredient_id);
CREATE INDEX ON ingredient_nutrients(nutrient_id);

-- Pantry
CREATE INDEX ON pantry_items(user_id);
CREATE INDEX ON pantry_items(ingredient_id);
CREATE INDEX ON pantry_items(expiry_date);
CREATE INDEX idx_pantry_expiring
    ON pantry_items(user_id, expiry_date)
    WHERE is_active = true;
CREATE INDEX idx_pantry_user_active
    ON pantry_items(user_id)
    WHERE is_active = true;

-- Recipes
CREATE INDEX ON recipe_ingredients(recipe_id);
CREATE INDEX ON recipe_ingredients(ingredient_id);
CREATE INDEX ON recipe_tags(tag);
CREATE INDEX ON recipes(created_by);
CREATE INDEX ON recipes(is_global);

-- User interactions
CREATE INDEX ON user_recipe_interactions(user_id, action);
CREATE INDEX ON user_recipe_interactions(recipe_id);

-- Collections
CREATE INDEX ON collection_recipes(collection_id);
CREATE INDEX ON collection_recipes(recipe_id);

-- Meal plans
CREATE INDEX ON meal_plans(user_id);
CREATE INDEX ON meal_plan_items(meal_plan_id);

-- Grocery
CREATE INDEX ON grocery_lists(user_id);
CREATE INDEX ON grocery_lists(user_id, status);
CREATE INDEX ON grocery_items(list_id);
CREATE INDEX ON grocery_items(ingredient_id);

-- Orders
CREATE INDEX ON orders(user_id, status);
CREATE INDEX ON orders(store_id);

-- Receipts
CREATE INDEX ON receipt_line_items(receipt_id);
CREATE INDEX ON receipts(user_id);

-- Waste log
CREATE INDEX ON waste_log(user_id);
CREATE INDEX ON waste_log(logged_at);

-- Chat
CREATE INDEX idx_chat_user_recent
    ON chat_messages(user_id, created_at DESC);

-- Priority cards
CREATE INDEX idx_priority_active
    ON priority_cards(user_id, priority DESC)
    WHERE is_dismissed = false;

-- ============================================================
-- VIEWS
-- ============================================================

CREATE OR REPLACE VIEW user_savings AS
SELECT
    user_id,
    SUM(CASE WHEN action = 'used'
        THEN estimated_value ELSE 0 END)       AS total_saved,
    SUM(CASE WHEN action = 'tossed'
        THEN estimated_value ELSE 0 END)       AS total_wasted,
    COUNT(CASE WHEN action = 'used'
        THEN 1 END)                            AS items_saved,
    COUNT(CASE WHEN action = 'tossed'
        THEN 1 END)                            AS items_wasted,
    SUM(CASE
        WHEN action = 'used'
        AND logged_at >= date_trunc('month', now())
        THEN estimated_value ELSE 0
    END)                                       AS saved_this_month,
    SUM(CASE
        WHEN action = 'tossed'
        AND logged_at >= date_trunc('month', now())
        THEN estimated_value ELSE 0
    END)                                       AS wasted_this_month
FROM waste_log
GROUP BY user_id;

-- ============================================================
-- FUNCTIONS & TRIGGERS
-- ============================================================

CREATE OR REPLACE FUNCTION handle_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_profiles
    FOR EACH ROW EXECUTE FUNCTION handle_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON pantry_items
    FOR EACH ROW EXECUTE FUNCTION handle_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON recipes
    FOR EACH ROW EXECUTE FUNCTION handle_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON meal_plans
    FOR EACH ROW EXECUTE FUNCTION handle_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON grocery_lists
    FOR EACH ROW EXECUTE FUNCTION handle_updated_at();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION handle_updated_at();

CREATE OR REPLACE FUNCTION get_expiring_items(
    p_user_id TEXT,
    p_days INT DEFAULT 3
)
RETURNS SETOF pantry_items AS $$
    SELECT * FROM pantry_items
    WHERE user_id = p_user_id
      AND is_active = true
      AND expiry_date IS NOT NULL
      AND expiry_date <= CURRENT_DATE + (p_days || ' days')::INTERVAL
    ORDER BY expiry_date ASC;
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION log_pantry_action(
    p_item_id UUID,
    p_action  TEXT,
    p_estimated_value DECIMAL DEFAULT 0
)
RETURNS void AS $$
DECLARE
    v_item pantry_items;
BEGIN
    SELECT * INTO v_item
    FROM pantry_items
    WHERE id = p_item_id;

    UPDATE pantry_items
    SET is_active = false,
        updated_at = now()
    WHERE id = p_item_id;

    INSERT INTO waste_log (
        user_id,
        item_name,
        action,
        estimated_value,
        pantry_item_id
    )
    VALUES (
        v_item.user_id,
        (SELECT name FROM ingredients WHERE id = v_item.ingredient_id),
        p_action,
        p_estimated_value,
        p_item_id
    );
END;
$$ LANGUAGE plpgsql;
