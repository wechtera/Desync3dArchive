"""Catalog search through the FTS5 index must return exactly what the old LIKE query returned."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server

OLD_SEARCH = """
    SELECT m.id FROM collections m
    WHERE m.name LIKE ? OR m.designer_name LIKE ? OR m.id IN (SELECT model_id FROM collection_tags WHERE tag LIKE ?)
"""
NEW_SEARCH = """
    SELECT m.id FROM collections m WHERE m.id IN (
        SELECT rowid FROM catalog_search WHERE name LIKE ?
        UNION SELECT rowid FROM catalog_search WHERE designer_name LIKE ?
        UNION SELECT model_id FROM collection_tags WHERE tag LIKE ?
            AND model_id IN (SELECT rowid FROM catalog_search WHERE tags LIKE ?)
    )
"""
MODELS = [
    (1, "Ancient Red Dragon", "Loot Studios"),
    (2, "dragonborn knight", "Bestiarum"),
    (3, "Élan Vital Statue", "Émile Forge"),
    (4, "élan vital bust", "émile forge"),
    (5, "100% Scale_Test", "Under_Score Co"),
    (6, "100 Scale Test", "Under Score Co"),
    (7, "Plain", None),
    (8, "日本の龍", "龍工房"),
]
TAGS = [(1, "dragon"), (1, "fantasy"), (2, "knight"), (3, "statue"), (5, "ab"), (5, "cd"), (6, "a%d"), (7, "red"), (7, "dragonfly")]
TERMS = ["dragon", "DRAGON", "dra", "dr", "d", "é", "É", "élan", "ÉLAN", "émile", "%", "_", "100%", "e_T", "a%d", "ab%cd",
         "b\ncd", "龍", "日本", "fantasy", "fan", "red", "nothing here", "'", '"', "NEAR(a b)", "a AND b", "*", "tag:x"]


def seed(conn):
    for model_id, name, designer in MODELS:
        conn.execute(
            "INSERT INTO collections (id, name, designer_name, raw_backup_path) VALUES (?, ?, ?, '')", (model_id, name, designer)
        )
    conn.executemany("INSERT INTO collection_tags (model_id, tag) VALUES (?, ?)", TAGS)
    conn.commit()


class SearchCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"
        create_database(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("DELETE FROM collections")
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def assert_same(self, term):
        pattern = f"%{term}%"
        old = {row[0] for row in self.conn.execute(OLD_SEARCH, [pattern] * 3)}
        new = {row[0] for row in self.conn.execute(NEW_SEARCH, [pattern] * 4)}
        self.assertEqual(new, old, repr(term))

    def assert_all_same(self):
        for term in TERMS:
            self.assert_same(term)

    def hits(self, term):
        pattern = f"%{term}%"
        return {row[0] for row in self.conn.execute(NEW_SEARCH, [pattern] * 4)}


class SameResultsTests(SearchCase):
    def test_every_term_matches_the_old_query(self):
        self.assert_all_same()

    def test_substrings_inside_a_word_still_match(self):
        self.assertEqual(self.hits("ragon"), {1, 2, 7})

    def test_terms_shorter_than_a_trigram_still_match(self):
        self.assertEqual(self.hits("dr"), {1, 2, 7})

    def test_a_wildcard_does_not_span_two_tags(self):
        self.assertEqual(self.hits("ab%cd"), set())


class IndexStaysCurrentTests(SearchCase):
    def test_new_model_with_tags(self):
        self.conn.execute("INSERT INTO collections (id, name, raw_backup_path) VALUES (20, 'Wyvern', '')")
        self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (20, 'beast')")
        self.assertEqual(self.hits("wyvern"), {20})
        self.assertEqual(self.hits("beast"), {20})
        self.assert_all_same()

    def test_upsert_rename(self):
        self.conn.execute("""
            INSERT INTO collections (id, name, designer_name, raw_backup_path) VALUES (1, 'Renamed', 'Someone', '')
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, designer_name = excluded.designer_name
        """)
        self.assertNotIn(1, self.hits("Ancient"))
        self.assertIn(1, self.hits("Renamed"))
        self.assertIn(1, self.hits("Someone"))
        self.assert_all_same()

    def test_other_updates_keep_the_row(self):
        self.conn.execute("UPDATE collections SET vault_path = '/x' WHERE id = 1")
        self.assertIn(1, self.hits("Ancient"))

    def test_retagging(self):
        self.conn.execute("DELETE FROM collection_tags WHERE model_id = 1")
        self.assertNotIn(1, self.hits("fantasy"))
        self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (1, 'scifi')")
        self.assertIn(1, self.hits("scifi"))
        self.assert_all_same()

    def test_deleting_a_model_cascades(self):
        self.conn.execute("DELETE FROM collections WHERE id = 1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM catalog_search WHERE rowid = 1").fetchone()[0], 0)
        self.assert_all_same()

    def test_the_index_passes_its_integrity_check(self):
        self.conn.execute("INSERT INTO catalog_search (catalog_search) VALUES ('integrity-check')")


class MigrationTests(unittest.TestCase):
    def test_existing_rows_are_indexed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "library.db"
            create_database(path)
            conn = sqlite3.connect(path)
            conn.execute("DROP TABLE catalog_search")
            conn.execute("CREATE VIRTUAL TABLE catalog_search USING fts5(name, designer_name, tags, tokenize = 'trigram')")
            seed_sql = Path(__file__).resolve().parents[1] / "migrations" / "0004_catalog_search.sql"
            backfill = seed_sql.read_text().split("INSERT INTO catalog_search", 1)[1].split(";", 1)[0]
            backfill = backfill.replace("FROM models m", "FROM collections m").replace("FROM model_tags", "FROM collection_tags")
            conn.execute("DELETE FROM collections")
            for trigger in ("model_insert", "tag_insert"):
                conn.execute(f"DROP TRIGGER catalog_search_{trigger}")
            seed(conn)
            conn.execute("INSERT INTO catalog_search" + backfill)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM catalog_search").fetchone()[0], len(MODELS))
            tags = conn.execute("SELECT tags FROM catalog_search WHERE rowid = 5").fetchone()[0]
            self.assertEqual(sorted(tags.split("\n")), ["ab", "cd"])
            conn.close()


class ApiTests(unittest.TestCase):
    def test_route_and_count(self):
        create_database(server.DB_PATH)
        conn = sqlite3.connect(server.DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM collections")
        seed(conn)
        conn.close()
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        data = client.get("/api/models", params={"search": "dragon", "limit": 1}).json()
        # Model 7 matches through a tag, but the route hides models with no designer.
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["models"]), 1)
        self.assertEqual(client.get("/api/models", params={"search": "%"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
