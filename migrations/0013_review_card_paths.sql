-- Review cards hold paths. "members" belongs to archives, and meant two things.
UPDATE library_release_reviews SET cards_json = replace(cards_json, '"members":', '"paths":');
