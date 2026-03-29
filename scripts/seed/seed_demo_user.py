#!/usr/bin/env python3
"""
Seed all mock data for a real Firebase test user.

Creates a fully populated account (recipes, pantry, collections, grocery list,
order, and waste log) so the app looks identical to the mock-data experience.

Usage:
    python scripts/seed/seed_demo_user.py <firebase_uid>

Example:
    python scripts/seed/seed_demo_user.py abc123XYZdef456

Re-running is safe: deletes the user's existing data first, then re-inserts.
"""

import sys
import os

# Allow running from the repo root
sys.path.insert(0, os.path.dirname(__file__))

import seed_mock

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed/seed_demo_user.py <firebase_uid>")
        print("\nGet the UID from Firebase Console → Authentication → Users → copy the UID column.")
        sys.exit(1)

    uid = sys.argv[1].strip()
    if not uid:
        print("Error: UID cannot be empty.", file=sys.stderr)
        sys.exit(1)

    print(f"Seeding demo data for Firebase UID: {uid}")
    # Override the demo user ID — seed_mock.seed() reads this as a global
    seed_mock.DEMO_USER_ID = uid
    seed_mock.main()
