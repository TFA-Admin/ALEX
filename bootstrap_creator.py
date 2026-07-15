# bootstrap_creator.py
"""
LOCAL-ONLY creator bootstrap.

Run this directly on the machine hosting A.L.E.X to bind a profile to the
'creator' role. This is deliberately a standalone script, not wired into
main.py, any router, or any chat/websocket path — granting creator can
only ever happen with local access to this machine, never over the network.

Usage:
    python bootstrap_creator.py <name>
"""
import asyncio
import re
import sys

from db.db import init_db, create_profile, update_fact, fetch_user_facts, profile_exists


def clean_name(raw: str) -> str:
    # Matches identity_manager.clean_text() so the bound name resolves
    # correctly when claimed later from any device.
    return re.sub(r'[^a-zA-Z]', '', raw.lower())


async def bootstrap(raw_name: str):
    name = clean_name(raw_name)

    if not name:
        print("❌ Not a valid name.")
        return

    await init_db()

    existed = await profile_exists(name)

    await create_profile(name)
    await update_fact(name, "role", "creator")

    facts = await fetch_user_facts(name)

    print(f"✅ Creator role bound to profile '{name}'.")
    if not existed:
        print("   (New profile created — she'll recognize this name from any device from now on.)")
    print(f"   role = {facts.get('role')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python bootstrap_creator.py <name>")
        sys.exit(1)

    asyncio.run(bootstrap(" ".join(sys.argv[1:])))
