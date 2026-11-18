"""
Roblox Group Auto-Rank Bot
--------------------------
Watches your Roblox group and automatically moves every new member
(who lands in the default "entry" role) to the role you choose.
Optionally it also auto-accepts pending join requests.

Uses the official Roblox Open Cloud API with an API key, so no account
cookie is needed. Only uses the Python standard library.

Run:  python autorank_bot.py
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://apis.roblox.com/cloud/v2"
CONFIG_FILE = Path(__file__).with_name("config.json")


def load_config():
    if os.environ.get("ROBLOX_API_KEY"):
        # GitHub Actions / environment-variable mode
        cfg = {
            "api_key": os.environ["ROBLOX_API_KEY"],
            "group_id": os.environ.get("GROUP_ID"),
            "target_role": os.environ.get("TARGET_ROLE"),
            "entry_role": os.environ.get("ENTRY_ROLE") or None,
            "auto_accept_join_requests": os.environ.get("AUTO_ACCEPT", "").lower() == "true",
        }
    elif CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    else:
        raise SystemExit(f"Missing {CONFIG_FILE.name}. Copy config.example.json to config.json and fill it in.")
    for key in ("api_key", "group_id", "target_role"):
        if not cfg.get(key):
            raise SystemExit(f"config.json: '{key}' is required.")
    cfg.setdefault("poll_seconds", 30)
    cfg.setdefault("auto_accept_join_requests", False)
    cfg.setdefault("entry_role", None)  # None = lowest rank above Guest (Roblox default)
    return cfg


class RobloxGroupAPI:
    def __init__(self, api_key, group_id):
        self.api_key = api_key
        self.group_id = str(group_id)

    def _request(self, method, path, params=None, body=None):
        url = f"{BASE}/groups/{self.group_id}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-api-key", self.api_key)
        if data is not None:
            req.add_header("Content-Type", "application/json")

        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:  # rate limited / server hiccup -> back off
                    wait = 2 ** attempt
                    print(f"  [!] HTTP {e.code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                detail = e.read().decode(errors="replace")
                raise RuntimeError(f"HTTP {e.code} on {method} {url}: {detail}") from None
        raise RuntimeError(f"Gave up after retries: {method} {url}")

    def _paginate(self, path, list_key, params=None):
        params = dict(params or {})
        params["maxPageSize"] = 100
        while True:
            page = self._request("GET", path, params)
            yield from page.get(list_key, [])
            token = page.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token

    def roles(self):
        return list(self._paginate("/roles", "groupRoles"))

    def members_in_role(self, role_id):
        role_path = f"groups/{self.group_id}/roles/{role_id}"
        return self._paginate("/memberships", "groupMemberships", {"filter": f"role == '{role_path}'"})

    def set_role(self, membership_path, role_id):
        membership_id = membership_path.rsplit("/", 1)[-1]
        self._request(
            "PATCH",
            f"/memberships/{membership_id}",
            body={"role": f"groups/{self.group_id}/roles/{role_id}"},
        )

    def join_requests(self):
        return self._paginate("/join-requests", "groupJoinRequests")

    def accept_join_request(self, join_request_path):
        jr_id = join_request_path.rsplit("/", 1)[-1]
        self._request("POST", f"/join-requests/{jr_id}:accept", body={})


def find_role(roles, wanted):
    """Match a role by name (case-insensitive), rank number, or role ID."""
    wanted_str = str(wanted).strip().lower()
    for r in roles:
        if r.get("displayName", "").lower() == wanted_str:
            return r
    for r in roles:
        if str(r.get("rank")) == wanted_str or str(r.get("id")) == wanted_str:
            return r
    names = ", ".join(f"{r['displayName']} (rank {r['rank']})" for r in roles)
    raise SystemExit(f"Role '{wanted}' not found. Available roles: {names}")


def main():
    cfg = load_config()
    api = RobloxGroupAPI(cfg["api_key"], cfg["group_id"])

    roles = sorted(api.roles(), key=lambda r: r["rank"])
    print("Roles in this group:")
    for r in roles:
        print(f"  rank {r['rank']:>3}  id {r['id']:<12}  {r['displayName']}")
    print(f"Settings: target_role={cfg['target_role']!r}  entry_role={cfg['entry_role']!r}")

    target = find_role(roles, cfg["target_role"])
    if cfg["entry_role"] is not None:
        entry = find_role(roles, cfg["entry_role"])
    else:
        entry = next(r for r in roles if r["rank"] > 0)  # default member role

    if entry["id"] == target["id"]:
        raise SystemExit(
            f"New members start in '{entry['displayName']}' (rank {entry['rank']}), and target_role "
            f"points to that same role - nothing to do. Set TARGET_ROLE to a higher rank from the list above."
        )

    print(f"Group {cfg['group_id']}: moving new members from "
          f"'{entry['displayName']}' -> '{target['displayName']}' every {cfg['poll_seconds']}s")
    if cfg["auto_accept_join_requests"]:
        print("Auto-accepting join requests: ON")

    run_once = os.environ.get("RUN_ONCE", "").lower() in ("1", "true")

    while True:
        try:
            if cfg["auto_accept_join_requests"]:
                for jr in list(api.join_requests()):
                    api.accept_join_request(jr["path"])
                    print(f"  [+] Accepted join request from {jr.get('user')}")

            ranked = 0
            for m in list(api.members_in_role(entry["id"])):
                api.set_role(m["path"], target["id"])
                ranked += 1
                print(f"  [*] Ranked {m.get('user')} -> {target['displayName']}")
            if run_once:
                print(f"Done. Ranked {ranked} member(s).")
                return
        except Exception as e:  # keep the bot alive on errors
            if run_once:
                raise SystemExit(f"Error: {e}")  # makes the GitHub run show as failed
            print(f"  [!] Error: {e}")

        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
