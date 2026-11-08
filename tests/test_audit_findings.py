"""E59: dismissing a Vault Audit finding, and saying when a saved scan came from another version."""

import json

from fastapi.testclient import TestClient
from test_grouping import GroupingCase

from desync_archival.api import server
from desync_archival.services import audit_findings
from desync_archival.services.db import get_db_connection
from desync_archival.services.reconciler import VaultReconciler

ISSUE = {"file_id": None, "filename": "metadata.json", "expected_path": "/Volumes/STL/Loot/September 2025/metadata.json",
         "defect": "METADATA_MISSING", "detail": "Embedded metadata.json is missing."}


class FindingCase(GroupingCase):
    def save_scan(self, issues_by_model, version=None):
        report = {
            "summary": {"total_audited": 10, "healthy": 10 - len(issues_by_model), "discrepancies": len(issues_by_model)},
            "discrepancies": [{"model_id": mid, "model_name": f"Model {mid}", "creator_name": "Loot Studios", "source": "linked",
                               "expected_dir": "/x", "issues": issues} for mid, issues in issues_by_model.items()],
        }
        if version:
            report["app_version"] = version
        with self.conn:
            self.conn.execute("""INSERT INTO reconciliation_runs (status, scan_mode, total_audited, healthy_count, discrepancy_count,
                                 report_json, completed_at) VALUES ('completed', 'deep', 10, 8, 2, ?, CURRENT_TIMESTAMP)""",
                              (json.dumps(report),))

    def status(self):
        return VaultReconciler(self.db).get_status()


class DismissTests(FindingCase):
    def test_a_dismissed_finding_moves_aside_and_the_count_follows(self):
        self.save_scan({2000002530: [ISSUE], 2000002529: [ISSUE]})
        audit_findings.dismiss(self.conn, 2000002530, ISSUE["defect"], ISSUE["filename"], ISSUE["detail"], "September 2025 PDFs")
        status = self.status()
        self.assertEqual([m["model_id"] for m in status["discrepancies"]], [2000002529])
        self.assertEqual(status["summary"]["discrepancies"], 1)
        self.assertEqual([(d["model_id"], d["defect"]) for d in status["dismissed"]], [(2000002530, "METADATA_MISSING")])

    def test_a_finding_that_reads_differently_shows_again(self):
        partial = {**ISSUE, "filename": "a.stl", "defect": "PARTIAL_FILE", "detail": "File truncated (10 B vs expected 20 B)."}
        audit_findings.dismiss(self.conn, 7, partial["defect"], partial["filename"], partial["detail"])
        self.save_scan({7: [{**partial, "detail": "File truncated (5 B vs expected 20 B)."}]})
        status = self.status()
        self.assertEqual(len(status["discrepancies"]), 1)
        self.assertEqual(status["dismissed"], [])

    def test_restore_brings_it_back(self):
        self.save_scan({7: [ISSUE]})
        audit_findings.dismiss(self.conn, 7, ISSUE["defect"], ISSUE["filename"], ISSUE["detail"])
        audit_findings.restore(self.conn, 7, ISSUE["defect"], ISSUE["filename"])
        self.assertEqual(len(self.status()["discrepancies"]), 1)
        with self.assertRaises(audit_findings.FindingRefused):
            audit_findings.restore(self.conn, 7, ISSUE["defect"], ISSUE["filename"])

    def test_only_the_named_issue_of_a_model_is_set_aside(self):
        other = {**ISSUE, "defect": "FILE_NOT_FOUND", "filename": "a.stl", "detail": "File does not exist at destination."}
        self.save_scan({7: [ISSUE, other]})
        audit_findings.dismiss(self.conn, 7, ISSUE["defect"], ISSUE["filename"], ISSUE["detail"])
        status = self.status()
        self.assertEqual([i["defect"] for i in status["discrepancies"][0]["issues"]], ["FILE_NOT_FOUND"])
        self.assertEqual(status["summary"]["discrepancies"], 1)

    def test_the_routes_dismiss_and_restore(self):
        self.save_scan({7: [ISSUE]})
        app = server.create_app(manage_workers=False)
        app.state.open_database = lambda: get_db_connection(self.db, shared=True)
        client = TestClient(app, base_url="http://localhost")
        body = {"model_id": 7, "defect": ISSUE["defect"], "filename": ISSUE["filename"], "detail": ISSUE["detail"]}
        self.assertEqual(client.post("/api/reconcile/findings/dismiss", json=body).status_code, 200)
        self.assertEqual(self.status()["discrepancies"], [])
        self.assertEqual(client.post("/api/reconcile/findings/restore", json=body).status_code, 200)
        self.assertEqual(client.post("/api/reconcile/findings/restore", json=body).status_code, 409)


class VersionTests(FindingCase):
    def test_a_scan_saved_by_another_version_says_so(self):
        self.save_scan({7: [ISSUE]}, version="1.0.0")
        status = self.status()
        self.assertEqual(status["saved_version"], "1.0.0")
        self.assertEqual(status["current_version"], audit_findings.app_version())
        self.assertNotEqual(status["current_version"], "unknown")

    def test_an_older_scan_has_no_version(self):
        self.save_scan({7: [ISSUE]})
        self.assertIsNone(self.status()["saved_version"])

    def test_a_new_scan_records_the_running_version(self):
        reconciler = VaultReconciler(self.db)
        reconciler._save_run_snapshot("completed", "fast", {"summary": {"total_audited": 0, "healthy": 0, "discrepancies": 0}})
        self.assertEqual(self.status()["saved_version"], audit_findings.app_version())
