"""
Roblox Group Auto-Rank Bot
--------------------------
Checks EVERY member of your Roblox group and gives them TARGET_ROLE,
except members whose role is in BYPASS_ROLES.

Settings (GitHub Actions variables / environment):
  ROBLOX_API_KEY  (secret)  Open Cloud API key with group read + write
  GROUP_ID                  your group's number
  TARGET_ROLE               role everyone should get (role ID, name, or rank)
  BYPASS_ROLES              roles to leave alone, comma separated (IDs, names or ranks),
                            e.g. "918998033, Founder, Holder". Use "none" for no bypass.
  AUTO_ACCEPT               "true" to auto-accept pending join requests (optional)

The group owner (rank 255) is always skipped because Roblox doesn't allow changing it.
Run locally with config.json, or on GitHub Actions with RUN_ONCE=true.
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
        cfg = {
            "api_key": os.environ["ROBLOX_API_KEY"],
            "group_id": os.environ.get("GROUP_ID"),
            "target_role": os.environ.get("TARGET_ROLE"),
            "bypass_roles": os.environ.get("BYPASS_ROLES", ""),
            "auto_accept_join_requests": os.environ.get("AUTO_ACCEPT", "").lower() == "true",
        }
    elif CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    else:
        raise SystemExit(f"Missing {CONFIG_FILE.name}. Copy config.example.json to config.json and fill it in.")
    for key in ("api_key", "group_id", "target_role"):
        if not cfg.get(key):
            raise SystemExit(f"Setting '{key}' is required.")
    if not str(cfg.get("bypass_roles", "")).strip():
        raise SystemExit(
            "BYPASS_ROLES is not set. Without it the bot would change EVERY member's role.\n"
            "Set it to the roles to leave alone (e.g. '918998033, Founder') or to 'none'."
        )
    cfg.setdefault("poll_seconds", 30)
    cfg.setdefault("auto_accept_join_requests", False)
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


def parse_bypass(roles, value):
    value = str(value).strip()
    if value.lower() == "none":
        return []
    return [find_role(roles, part) for part in value.split(",") if part.strip()]


def run_once(api, cfg, target, bypass_ids, roles_by_id):
    if cfg["auto_accept_join_requests"]:
        for jr in list(api.join_requests()):
            api.accept_join_request(jr["path"])
            print(f"  [+] Accepted join request from {jr.get('user')}")

    ranked = skipped = failed = 0
    members = list(api.members())
    for m in members:
        role_id = m.get("role", "").rsplit("/", 1)[-1]
        current = roles_by_id.get(role_id)
        name = current["displayName"] if current else f"unknown role {role_id}"

        if role_id == str(target["id"]):
            continue  # already correct
        if role_id in bypass_ids or (current and current["rank"] >= 255):
            skipped += 1
            continue  # bypass role or group owner

        try:
            api.set_role(m["path"], target["id"])
            ranked += 1
            print(f"  [*] {m.get('user')}: {name} -> {target['displayName']}")
        except Exception as e:  # one failure shouldn't stop the others
            failed += 1
            print(f"  [!] Could not rank {m.get('user')} ({name}): {e}")

    print(f"Checked {len(members)} member(s): ranked {ranked}, bypassed {skipped}, failed {failed}.")
    return failed


def main():
    cfg = load_config()
    api = RobloxGroupAPI(cfg["api_key"], cfg["group_id"])

    roles = sorted(api.roles(), key=lambda r: r["rank"])
    print("Roles in this group:")
    for r in roles:
        print(f"  rank {r['rank']:>3}  id {r['id']:<12}  {r['displayName']}")

    target = find_role(roles, cfg["target_role"])
    bypass = parse_bypass(roles, cfg["bypass_roles"])
    bypass_ids = {str(r["id"]) for r in bypass}
    if str(target["id"]) in bypass_ids:
        raise SystemExit("TARGET_ROLE is also in BYPASS_ROLES - remove it from BYPASS_ROLES.")
    roles_by_id = {str(r["id"]): r for r in roles}

    print(f"Target role: {target['displayName']}")
    print(f"Bypass roles: {', '.join(r['displayName'] for r in bypass) or 'none'} (+ group owner)")

    once = os.environ.get("RUN_ONCE", "").lower() in ("1", "true")
    while True:
        try:
            failed = run_once(api, cfg, target, bypass_ids, roles_by_id)
            if once:
                if failed:
                    raise SystemExit(1)  # mark the GitHub run as failed so you notice
                return
        except SystemExit:
            raise
        except Exception as e:
            if once:
                raise SystemExit(f"Error: {e}")
            print(f"  [!] Error: {e}")
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
