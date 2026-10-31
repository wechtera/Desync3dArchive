-- Search reaches models, model groups and artifacts, not only collections. Same trigram and LIKE
-- approach as catalog_search. One table per level, because each rowid is that level's ID.
CREATE VIRTUAL TABLE catalog_search_models USING fts5(name, tokenize = 'trigram');
CREATE VIRTUAL TABLE catalog_search_groups USING fts5(name, tokenize = 'trigram');
CREATE VIRTUAL TABLE catalog_search_artifacts USING fts5(name, tokenize = 'trigram');

INSERT INTO catalog_search_models (rowid, name) SELECT id, name FROM catalog_models;

CREATE TRIGGER level_search_model_insert AFTER INSERT ON catalog_models BEGIN
    INSERT INTO catalog_search_models (rowid, name) VALUES (NEW.id, NEW.name);
END;
CREATE TRIGGER level_search_model_update AFTER UPDATE OF name ON catalog_models BEGIN
    UPDATE catalog_search_models SET name = NEW.name WHERE rowid = NEW.id;
END;
CREATE TRIGGER level_search_model_delete AFTER DELETE ON catalog_models BEGIN
    DELETE FROM catalog_search_models WHERE rowid = OLD.id;
END;

CREATE TRIGGER level_search_group_insert AFTER INSERT ON model_groups BEGIN
    INSERT INTO catalog_search_groups (rowid, name) VALUES (NEW.id, NEW.name);
END;
CREATE TRIGGER level_search_group_update AFTER UPDATE OF name ON model_groups BEGIN
    UPDATE catalog_search_groups SET name = NEW.name WHERE rowid = NEW.id;
END;
CREATE TRIGGER level_search_group_delete AFTER DELETE ON model_groups BEGIN
    DELETE FROM catalog_search_groups WHERE rowid = OLD.id;
END;

CREATE TRIGGER level_search_artifact_insert AFTER INSERT ON model_artifacts BEGIN
    INSERT INTO catalog_search_artifacts (rowid, name) VALUES (NEW.id, NEW.filename);
END;
CREATE TRIGGER level_search_artifact_delete AFTER DELETE ON model_artifacts BEGIN
    DELETE FROM catalog_search_artifacts WHERE rowid = OLD.id;
END;
