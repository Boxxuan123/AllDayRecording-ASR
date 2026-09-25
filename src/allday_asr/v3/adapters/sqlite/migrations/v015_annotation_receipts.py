"""Recovery targets for immutable legacy operation receipts (no payload rewrite)."""
SQL = """
CREATE TABLE annotation_receipt_recovery (
 operation_id TEXT PRIMARY KEY REFERENCES client_operations(operation_id),
 resource_results_json TEXT NOT NULL
);
"""
