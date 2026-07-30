-- Campus Whispers Supabase Schema
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor → New Query)

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    real_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    handle TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    banned INTEGER NOT NULL DEFAULT 0,
    selected_badge TEXT DEFAULT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Rumors/posts table
CREATE TABLE IF NOT EXISTS rumors (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bumped_at TEXT,
    highlighted INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0,
    is_incognito INTEGER NOT NULL DEFAULT 0
);

-- Purchases table (shop items, badges, etc.)
CREATE TABLE IF NOT EXISTS purchases (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

-- Reactions table
CREATE TABLE IF NOT EXISTS reactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rumor_id INTEGER NOT NULL REFERENCES rumors(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    UNIQUE(user_id, rumor_id, kind)
);

-- Me-too table
CREATE TABLE IF NOT EXISTS me_too (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rumor_id INTEGER NOT NULL REFERENCES rumors(id) ON DELETE CASCADE,
    UNIQUE(user_id, rumor_id)
);

-- Comments table
CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rumor_id INTEGER NOT NULL REFERENCES rumors(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Tags table
CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- Rumor-Tags junction
CREATE TABLE IF NOT EXISTS rumor_tags (
    rumor_id INTEGER NOT NULL REFERENCES rumors(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (rumor_id, tag_id)
);

-- Tag follows
CREATE TABLE IF NOT EXISTS tag_follows (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, tag_id)
);

-- Challenge claims
CREATE TABLE IF NOT EXISTS challenge_claims (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    challenge_key TEXT NOT NULL,
    week_start TEXT NOT NULL,
    UNIQUE(user_id, challenge_key, week_start)
);

-- Weekly challenges
CREATE TABLE IF NOT EXISTS weekly_challenges (
    id SERIAL PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    target INTEGER NOT NULL,
    reward_points INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL
);

-- Insert default weekly challenges
INSERT INTO weekly_challenges (key, label, description, target, reward_points, week_start, week_end)
VALUES
    ('post_5', 'Chatterbox', 'Post 5 whispers this week', 5, 50, '2025-01-01', '2025-01-07'),
    ('post_10', 'Rumor Mill', 'Post 10 whispers this week', 10, 100, '2025-01-01', '2025-01-07'),
    ('post_20', 'Whisper Legend', 'Post 20 whispers this week', 20, 200, '2025-01-01', '2025-01-07')
ON CONFLICT (key) DO NOTHING;

-- Insert default tags
INSERT INTO tags (name) VALUES
    ('rumors'), ('exam'), ('general'), ('funny'), ('confession'), ('question'), ('help')
ON CONFLICT (name) DO NOTHING;

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_rumors_user_id ON rumors(user_id);
CREATE INDEX IF NOT EXISTS idx_rumors_created_at ON rumors(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rumors_featured ON rumors(featured) WHERE featured = 1;
CREATE INDEX IF NOT EXISTS idx_rumors_highlighted ON rumors(highlighted) WHERE highlighted = 1;
CREATE INDEX IF NOT EXISTS idx_purchases_user_id ON purchases(user_id);
CREATE INDEX IF NOT EXISTS idx_reactions_rumor_id ON reactions(rumor_id);
CREATE INDEX IF NOT EXISTS idx_comments_rumor_id ON comments(rumor_id);
CREATE INDEX IF NOT EXISTS idx_rumor_tags_tag_id ON rumor_tags(tag_id);

-- Enable Row Level Security (optional - for future)
-- ALTER TABLE users ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE rumors ENABLE ROW LEVEL SECURITY;

-- Grant permissions (Supabase handles this via roles)
GRANT USAGE ON SCHEMA public TO anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated;