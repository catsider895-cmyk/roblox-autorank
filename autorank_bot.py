"""
Roblox Group Auto-Rank Bot
--------------------------
Gives everyone who recently joined your Roblox group the role you choose
(TARGET_ROLE), no matter which role Roblox put them in.

- Only touches members who joined in the last NEW_MEMBER_MINUTES (default 60),
  so existing members are never changed.
- Never touches admin/owner roles (rank PROTECTED_RANK and above, default 254).
- Optionally auto-accepts pending join requests (AUTO_ACCEPT=true).

Uses the official Roblox Open Cloud API with an API key. Standard library only.
Run locally:  python autorank_bot.py   (uses config.json)
GitHub Actions: set RUN_ONCE=true and the settings as environment variables.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
            "auto_accept_join_requests": os.environ.get("AUTO_ACCEPT", "").lower() == "true",
        }
        if os.environ.get("NEW_MEMBER_MINUTES"):
            cfg["new_member_minutes"] = int(os.environ["NEW_MEMBER_MINUTES"])
        if os.environ.get("PROTECTED_RANK"):
            cfg["protected_rank"] = int(os.environ["PROTECTED_RANK"])
    elif CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    else:
        raise SystemExit(f"Missing {CONFIG_FILE.name}. Copy config.example.json to config.json and fill it in.")
    for key in ("api_key", "group_id", "target_role"):
        if not cfg.get(key):
            raise SystemExit(f"Setting '{key}' is required.")
    cfg.setdefault("poll_seconds", 30)
    cfg.setdefault("auto_accept_join_requests", False)
    cfg.setdefault("new_member_minutes", 60)
    cfg.setdefault("protected_rank", 254)
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

    def members(self):
        return self._paginate("/memberships", "groupMemberships")

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
    """Match a role by role ID, name (case-insensitive) or rank number."""
    wanted_str = str(wanted).strip().lower()
    for r in roles:
        if str(r.get("id")) == wanted_str:
            return r
    for r in roles:
        if r.get("displayName", "").lower() == wanted_str:
            return r
    matches = [r for r in roles if str(r.get("rank")) == wanted_str]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(f"More than one role has rank {wanted}. Use the role ID instead.")
    raise SystemExit(f"Role '{wanted}' not found. Use one of the role IDs listed above.")


def parse_time(value):
    """Parse an RFC 3339 time like 2026-11-18T12:34:56.789Z."""
    value = value.replace("Z", "").split("+")[0]
    main = value.split(".")[0]
    return datetime.strptime(main, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def run_once(api, cfg, target, roles_by_id):
    if cfg["auto_accept_join_requests"]:
        for jr in list(api.join_requests()):
            api.accept_join_request(jr["path"])
            print(f"  [+] Accepted join request from {jr.get('user')}")

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg["new_member_minutes"])
    ranked = 0
    no_time = 0
    members = list(api.members())
    print(f"  Checked {len(members)} member(s). Newest joiners:")
    newest = sorted((m for m in members if m.get("createTime")),
                    key=lambda m: m["createTime"], reverse=True)[:5]
    for m in newest:
        r = roles_by_id.get(m.get("role", "").rsplit("/", 1)[-1])
        print(f"    {m.get('user')}  joined {m['createTime']}  role: {r['displayName'] if r else m.get('role')}")
    for m in members:
        created = m.get("createTime")
        if not created:
            no_time += 1
            continue
        if parse_time(created) < cutoff:
            continue  # not a new member -> leave alone
        role_id = m.get("role", "").rsplit("/", 1)[-1]
        if role_id == str(target["id"]):
            continue  # already has the right role
        current = roles_by_id.get(role_id)
        if current and current["rank"] >= cfg["protected_rank"]:
            continue  # never touch admins/owner
        api.set_role(m["path"], target["id"])
        ranked += 1
        name = current["displayName"] if current else role_id
        print(f"  [*] {m.get('user')}: {name} -> {target['displayName']}")
    if no_time:
        print(f"  [!] {no_time} member(s) had no join time from Roblox and were skipped.")
    return ranked


def main():
    cfg = load_config()
    api = RobloxGroupAPI(cfg["api_key"], cfg["group_id"])

    roles = sorted(api.roles(), key=lambda r: r["rank"])
    print("Roles in this group:")
    for r in roles:
        print(f"  rank {r['rank']:>3}  id {r['id']:<12}  {r['displayName']}")

    target = find_role(roles, cfg["target_role"])
    roles_by_id = {str(r["id"]): r for r in roles}
    print(f"Giving '{target['displayName']}' to everyone who joined in the last "
          f"{cfg['new_member_minutes']} min (ranks {cfg['protected_rank']}+ are never changed).")

    once = os.environ.get("RUN_ONCE", "").lower() in ("1", "true")
    while True:
        try:
            ranked = run_once(api, cfg, target, roles_by_id)
            if once:
                print(f"Done. Ranked {ranked} member(s).")
                return
        except Exception as e:
            if once:
                raise SystemExit(f"Error: {e}")  # makes the GitHub run show as failed
            print(f"  [!] Error: {e}")
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
