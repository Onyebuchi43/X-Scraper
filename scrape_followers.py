# -*- coding: utf-8 -*-
"""
Scrape 50 followers from @elonmusk using Scweet.
"""

from __future__ import annotations

import json
import logging

from Scweet import Scweet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

AUTH_TOKEN = "6b832b12eb0d082ca93194b165f400cc1573732a"
CT0       = "633172eddbaab8f4baed6f453d3a3ec760ee0d67d3c3f5956965cf5bac53f1372cf665c5d9c55c4e7cd1aa63b86e095a21e79f32c3ca8f2fa99b90562de1ca43ee03efa6c6bef7b51e18b3cfb4d6f197"

def main() -> None:
    print("Initializing Scweet...")
    s = Scweet(
        cookies={"auth_token": AUTH_TOKEN, "ct0": CT0},
    )

    print("Fetching 50 followers of @elonmusk...")
    followers = s.get_followers(["elonmusk"], limit=50, save=True)

    print(f"\n[DONE] Collected {len(followers)} followers")
    if followers:
        # Pretty-print the first few entries so the user can see the shape
        print("\nFirst 5 entries (preview):")
        for user in followers[:5]:
            if isinstance(user, dict):
                name     = user.get("name") or user.get("username") or user.get("screen_name") or "—"
                handle   = user.get("username") or user.get("screen_name") or "—"
                followers_count = user.get("followers_count", "—")
                print(f"  @{handle} | {name} | followers: {followers_count}")
            else:
                print(f"  {user}")

        # Save a local JSON copy for easy inspection
        out_path = "elonmusk_followers.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(followers, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nFull results saved -> {out_path}")

if __name__ == "__main__":
    main()
