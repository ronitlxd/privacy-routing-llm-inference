"""Create an encrypted driver profile, optionally starting from an existing private database.

    python scripts/create_profile.py --name Jordan --from-db demo.sqlite3

The password is asked for interactively (or read from JLR_PROFILE_PASSWORD for
automation). Profiles are written to JLR_PROFILES_DIR (default: ./profiles).
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jlr.profiles import ProfileError, ProfileRegistry  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--from-db", help="SQLite private database to copy into the profile")
    parser.add_argument("--dir", default=os.environ.get("JLR_PROFILES_DIR", "profiles"))
    args = parser.parse_args()
    password = os.environ.get("JLR_PROFILE_PASSWORD")
    if password is None:
        password = getpass.getpass("Profile password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match.")
    try:
        store = ProfileRegistry(args.dir).create(args.name, password, source=args.from_db)
    except ProfileError as exc:
        sys.exit(str(exc))
    print(f"Created profile {store.profile['name']} ({store.profile['id']}) in {args.dir}")


if __name__ == "__main__":
    main()
