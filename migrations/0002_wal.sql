-- no-transaction
-- SQLite can't change journal mode inside a transaction. Readers stop blocking on writers.
PRAGMA journal_mode = WAL;
