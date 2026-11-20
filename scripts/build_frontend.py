"""Stage the static frontend and shared agent modules for Pages or local preview."""

import argparse
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    dest = parser.parse_args().output.resolve()
    root = Path(__file__).resolve().parents[1]
    frontend = root / "frontend"
    agent = root / "agent"
    if dest == root or dest in root.parents or dest.is_relative_to(frontend) or dest.is_relative_to(agent):
        parser.error("output must be outside the source directories")
    shutil.copytree(frontend, dest, dirs_exist_ok=True)
    shutil.copytree(agent, dest / "agent", dirs_exist_ok=True)


if __name__ == "__main__":
    main()
