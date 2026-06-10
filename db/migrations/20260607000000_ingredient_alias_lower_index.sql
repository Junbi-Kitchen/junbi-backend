-- migrate:up
CREATE INDEX IF NOT EXISTS idx_ingredient_aliases_alias_lower
    ON ingredient_aliases (lower(alias));

-- migrate:down
DROP INDEX IF EXISTS idx_ingredient_aliases_alias_lower;
