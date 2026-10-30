#!/usr/bin/env python3
"""Create demo vault data for a case with two evidence files.

Usage: python demo_vault_data.py CASE-001 EV-0001 EV-0002

Both evidence notes must already exist (the two ids may be equal).
Creates:
- CL-0001 from first file: statement "Witness says the suspect was at the villa at 10:00 PM.", source_ref page 1, entities: person Suspect A, place Goa villa
- CL-0002 from second file: transaction "ATM withdrawal in Panaji at 10:05 PM.", source_ref row 14, entities: person Suspect A, account 1234567890, place Panaji ATM
- FLAG-0001: type time, linking both claims, entity Suspect A, description "Suspect placed at the villa at 10:00 PM, but an ATM withdrawal in another city is recorded at 10:05 PM."
"""

import sys
from pathlib import Path

# Ensure backend is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.db import init_db, get_connection
from app.vault.writer import get_writer, ClaimInput, FlagInput, EntityRef


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python demo_vault_data.py CASE-001 EV-0001 EV-0002", file=sys.stderr)
        sys.exit(1)
    
    case_id = sys.argv[1]
    file_id_1 = sys.argv[2]
    file_id_2 = sys.argv[3]
    
    print("=" * 60)
    print("WARNING: Creating DEMO vault data - NOT for production use!")
    print("This creates realistic-looking claims and flags on a throwaway case.")
    print("=" * 60)
    
    settings = get_settings()
    init_db()
    conn = get_connection()
    try:
        writer = get_writer()
        
        # Ensure case folders exist
        writer.ensure_case(case_id)
        
        # Verify both evidence notes exist
        for fid in (file_id_1, file_id_2):
            note_path = writer._path(case_id, "evidence", f"{fid}.md")
            if not note_path.exists():
                print(f"Error: Evidence note not found: {fid}", file=sys.stderr)
                sys.exit(1)
        
        # CL-0001 from first file
        claim1 = ClaimInput(
            claim_id="CL-0001",
            source_file_id=file_id_1,
            source_ref="page 1",
            claim_type="statement",
            text="Witness says the suspect was at the villa at 10:00 PM.",
            when="2026-10-29T22:00:00+05:30",
            entities=(
                EntityRef("person", "Suspect A"),
                EntityRef("place", "Goa villa"),
            ),
            confidence=0.82,
        )
        writer.write_claim(case_id, claim1)
        print(f"Created claim: CL-0001 from {file_id_1}")
        
        # CL-0002 from second file
        claim2 = ClaimInput(
            claim_id="CL-0002",
            source_file_id=file_id_2,
            source_ref="row 14",
            claim_type="transaction",
            text="ATM withdrawal in Panaji at 10:05 PM.",
            when="2026-10-29T22:05:00+05:30",
            entities=(
                EntityRef("person", "Suspect A"),
                EntityRef("account", "1234567890"),
                EntityRef("place", "Panaji ATM"),
            ),
            confidence=0.95,
        )
        writer.write_claim(case_id, claim2)
        print(f"Created claim: CL-0002 from {file_id_2}")
        
        # FLAG-0001
        flag = FlagInput(
            flag_id="FLAG-0001",
            conflict_type="time",
            claim_a="CL-0001",
            claim_b="CL-0002",
            description="Suspect placed at the villa at 10:00 PM, but an ATM withdrawal in another city is recorded at 10:05 PM.",
            entities=(EntityRef("person", "Suspect A"),),
        )
        writer.write_flag(case_id, flag)
        print("Created flag: FLAG-0001 (time conflict)")
        
        print("\nDone. Open the vault folder as an Obsidian vault to see the graph.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
