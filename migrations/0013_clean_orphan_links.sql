-- A rebuild ran with foreign keys off and left these pointing at rows it had removed. IDs here are
-- never reused, so they can't attach to the wrong thing, but a check should come back clean.
DELETE FROM model_group_members
WHERE group_id NOT IN (SELECT id FROM model_groups)
   OR catalog_model_id NOT IN (SELECT id FROM catalog_models);
DELETE FROM catalog_model_tags WHERE catalog_model_id NOT IN (SELECT id FROM catalog_models);

-- Review cards hold paths. "members" belongs to archives, and meant two things.
UPDATE library_release_reviews SET cards_json = replace(cards_json, '"members":', '"paths":');
