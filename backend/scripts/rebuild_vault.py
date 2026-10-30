#!/usr/bin/env python3
"""Rebuild vault notes from evidence_files table.

Usage: python rebuild_vault.py

For every evidence_files row with status verified, tampered, or quarantined
(ordered by seq), call sync_evidence_note; for tampered rows also call
mark_claims_stale. Prints how many notes were written.
"""

import sys
from pathlib import Path

# Ensure backend is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.db import init_db, get_connection
from app.vault.writer import get_writer


def main() -> None:
    settings = get_settings()
    
    print(f"Rebuilding vault at {settings.vault_dir}")
    
    init_db()
    conn = get_connection()
    try:
        writer = get_writer()
        
        rows = conn.execute(
            """
            SELECT * FROM evidence_files
            WHERE status IN ('verified', 'tampered', 'quarantined')
            ORDER BY seq
            """
        ).fetchall()
        
        count = 0
        for row in rows:
            writer.sync_evidence_note(dict(row))
            count += 1
            
            if row["status"] == "tampered":
                writer.mark_claims_stale(row["case_id"], row["file_id"])
        
        print(f"Written {count} evidence notes")
    finally:
        conn.close()


if __name__ == "__main__":
    main()