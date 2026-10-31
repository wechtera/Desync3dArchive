# Questions

Open questions that do not block the migration. Move each one out when it is answered, and record the decision in `README.md`. Real blockers are raised with the user directly.

| # | Question | Why it matters | Needed by |
|---|---|---|---|
| Q1 | What OS will the LAN server run? | Affects path defaults and how the app runs as a service | Step 7 |
| Q3 | Should rejected options be revisited now that development cost carries little weight? | SQLAlchemy and React or Svelte were rejected partly because of rewrite size. Their other objections still stand: no capability this app needs, and the risk of losing behavior | Step 3 for data access, Step 6 for frontend |
| Q4 | Is the MMF terms of service position on automated library access settled? | The scraper stays because no API replaces it, but stealth browsing may breach the terms | Step 5 |
| Q5 | Should the app ever support more than one user or library? | Current plan is single user, with no users table | Step 7 |
| Q6 | Should storage move behind an adapter for SMB or S3, or stay a mounted path? | Current plan keeps a mounted NAS path | Step 5 |
| Q7 | Which disk should production backups use? | `DESYNC_BACKUP_DIR` must not share a disk with the data directory | Step 9 |
| Q12 | Is the rare browser test failure gone? | One test failed once. Test mocks that outlived a test are the likely cause and were removed. `playwright-last.log` keeps the output if it recurs | If it recurs |
| Q10 | Should the stash export success path get a snapshot, using a real small archive in the dev vault? | Only the failure path is recorded today | Step 0B |
| Q8 | Should the `rnd/` Blender experiments feed better previews? | They stayed in `scrapepage` and were never reviewed | Any time |
| Q13 | Should `app_settings` keep `key` as its primary key? | It is a dictionary of setting names with no outside IDs, and `ON CONFLICT(key)` is used throughout. The plan leaves it unless the increment rule should cover it too | Key decoupling |
| Q14 | When a library is unlinked, should stash items from it be removed, or kept and marked missing? | Today they are left orphaned and can attach to a new model | Key decoupling phase 2 |
| Q15 | Should every file in Loot Studios be hashed? | Duplicate detection needs it, but it means reading the whole library over the network | Key decoupling phase 6 |
| Q16 | Should default catalog order stay "newest on MMF"? | `ORDER BY id` becomes "newest added" once IDs are agnostic | Key decoupling phase 1 |
