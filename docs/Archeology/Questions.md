# Questions

Open questions that do not block the migration. Move each one out when it is answered, and record the decision in `README.md`. Real blockers are raised with the user directly.

| # | Question | Why it matters | Needed by |
|---|---|---|---|
| Q1 | What OS will the LAN server run? | Affects path defaults and how the app runs as a service | Step 7 |
| Q2 | Would MMF issue an OAuth client secret for this application? The API README says clients are created in account settings, and the code grant is the only documented refreshable path for a non-mobile app | Without one there are no refresh tokens, so a 6-hour token can stop a long download run | Before the full download run |
| Q3 | Should rejected options be revisited now that development cost carries little weight? | SQLAlchemy and React or Svelte were rejected partly because of rewrite size. Their other objections still stand: no capability this app needs, and the risk of losing behavior | Step 3 for data access, Step 6 for frontend |
| Q4 | Is the MMF terms of service position on automated library access settled? | The scraper stays because no API replaces it, but stealth browsing may breach the terms | Step 5 |
| Q5 | Should the app ever support more than one user or library? | Current plan is single user, with no users table | Step 7 |
| Q6 | Should storage move behind an adapter for SMB or S3, or stay a mounted path? | Current plan keeps a mounted NAS path | Step 5 |
| Q7 | Which disk should production backups use? | `DESYNC_BACKUP_DIR` must not share a disk with the data directory | Step 9 |
| Q10 | Should the stash export success path get a snapshot, using a real small archive in the dev vault? | Only the failure path is recorded today | Step 0B |
| Q8 | Should the `rnd/` Blender experiments feed better previews? | They stayed in `scrapepage` and were never reviewed | Any time |
