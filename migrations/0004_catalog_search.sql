-- Trigram index behind catalog search. Search keeps its LIKE semantics: FTS5 narrows the candidates
-- and SQLite re-checks each row with the same LIKE, so substrings and wildcards still work.
-- Tags join with a newline, one row per model, and the triggers below keep this in step.
CREATE VIRTUAL TABLE catalog_search USING fts5(name, designer_name, tags, tokenize = 'trigram');

INSERT INTO catalog_search (rowid, name, designer_name, tags)
SELECT m.id, m.name, m.designer_name,
       (SELECT group_concat(t.tag, char(10)) FROM model_tags t WHERE t.model_id = m.id)
FROM models m;

CREATE TRIGGER catalog_search_model_insert AFTER INSERT ON models BEGIN
    INSERT INTO catalog_search (rowid, name, designer_name, tags)
    VALUES (new.id, new.name, new.designer_name,
            (SELECT group_concat(tag, char(10)) FROM model_tags WHERE model_id = new.id));
END;

CREATE TRIGGER catalog_search_model_update AFTER UPDATE OF name, designer_name ON models BEGIN
    UPDATE catalog_search SET name = new.name, designer_name = new.designer_name WHERE rowid = new.id;
END;

CREATE TRIGGER catalog_search_model_delete AFTER DELETE ON models BEGIN
    DELETE FROM catalog_search WHERE rowid = old.id;
END;

CREATE TRIGGER catalog_search_tag_insert AFTER INSERT ON model_tags BEGIN
    UPDATE catalog_search
    SET tags = (SELECT group_concat(tag, char(10)) FROM model_tags WHERE model_id = new.model_id)
    WHERE rowid = new.model_id;
END;

CREATE TRIGGER catalog_search_tag_delete AFTER DELETE ON model_tags BEGIN
    UPDATE catalog_search
    SET tags = (SELECT group_concat(tag, char(10)) FROM model_tags WHERE model_id = old.model_id)
    WHERE rowid = old.model_id;
END;
