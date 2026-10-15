PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE sources (
 source_id TEXT PRIMARY KEY, publisher TEXT, url TEXT NOT NULL,
 revision_url TEXT, revision_id TEXT, last_edited_text TEXT,
 fetched_at_utc TEXT, sha256 TEXT, license TEXT
);
CREATE TABLE heroes (
 hero_id TEXT PRIMARY KEY, name_en TEXT NOT NULL, name_ko TEXT,
 role TEXT, subrole TEXT, health_hp REAL, armor_hp REAL, shields_hp REAL,
 total_hp REAL, mode_scope TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources,
 review_required INTEGER NOT NULL, data_json TEXT NOT NULL
);
CREATE TABLE abilities (
 ability_id TEXT PRIMARY KEY, hero_id TEXT NOT NULL REFERENCES heroes,
 name_en TEXT NOT NULL, kind TEXT NOT NULL, input_slot TEXT,
 is_perk INTEGER NOT NULL, cooldown_s REAL, max_charges INTEGER,
 ultimate_charge_cost REAL, description TEXT, source_id TEXT NOT NULL REFERENCES sources,
 review_required INTEGER NOT NULL, data_json TEXT NOT NULL
);
CREATE INDEX abilities_hero ON abilities(hero_id);
CREATE TABLE ability_stats (
 stat_id INTEGER PRIMARY KEY, ability_id TEXT NOT NULL REFERENCES abilities,
 stat_key TEXT NOT NULL, label TEXT NOT NULL, value_text TEXT,
 value_real REAL, unit TEXT, status TEXT NOT NULL,
 review_required INTEGER NOT NULL, components_json TEXT NOT NULL,
 tooltips_json TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources
);
CREATE INDEX ability_stats_key ON ability_stats(stat_key, ability_id);
CREATE TABLE effects (
 ability_id TEXT NOT NULL REFERENCES abilities, effect_key TEXT NOT NULL,
 label TEXT NOT NULL, effect_and_exceptions TEXT,
 PRIMARY KEY(ability_id,effect_key)
);
CREATE TABLE checks (
 check_id TEXT PRIMARY KEY, hero_id TEXT NOT NULL REFERENCES heroes,
 ability_id TEXT REFERENCES abilities, field_path TEXT NOT NULL,
 expected_json TEXT NOT NULL, observed_json TEXT, status TEXT NOT NULL,
 patch_date TEXT NOT NULL, source_url TEXT NOT NULL, rationale TEXT NOT NULL
);
CREATE TABLE patch_index (
 patch_date TEXT PRIMARY KEY, title TEXT NOT NULL, source_url TEXT NOT NULL,
 indexed_hero_updates INTEGER NOT NULL
);
CREATE TABLE manual_reviews (
 review_id TEXT PRIMARY KEY,hero_id TEXT NOT NULL REFERENCES heroes,
 ability_id TEXT NOT NULL REFERENCES abilities,reason TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources,status TEXT NOT NULL
);
CREATE VIRTUAL TABLE ability_search USING fts5(
 ability_id UNINDEXED, hero_id UNINDEXED, name_en, hero_names, description, stats, effects, notes,
 tokenize='unicode61'
);
CREATE VIEW cooldowns AS
 SELECT h.hero_id,h.name_en AS hero_name,h.name_ko,a.name_en AS ability,
 a.cooldown_s,a.max_charges,a.review_required,s.revision_url
 FROM abilities a JOIN heroes h USING(hero_id) JOIN sources s ON a.source_id=s.source_id
 WHERE a.cooldown_s IS NOT NULL AND a.is_perk=0;
CREATE VIEW review_queue AS
 SELECT check_id,hero_id,ability_id,field_path,expected_json,observed_json,status,patch_date,source_url
 FROM checks WHERE status NOT IN ('matched','matched_presence');
