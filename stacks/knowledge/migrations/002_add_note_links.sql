CREATE TABLE IF NOT EXISTS note_links (
    source_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    target_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,
    score FLOAT,
    PRIMARY KEY (source_id, target_id, link_type)
);
