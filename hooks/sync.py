"""SessionEnd: report the project index to the course server.

This is what powers the website's project picker. It sends aggregate metadata
about every repo the student has worked in, so they can later select which ones
counted toward a submission, the way Hackatime does.

Three rules it must obey:

  1. Never block. Wired as an async hook and given a short timeout, because a
     student on hotel wifi must not wait on our HTTP call to close a session.
  2. Never raise. A missing endpoint, no network, a 500, an expired key: all
     are non-events. Local tracking continues regardless and the ledger in the
     repo remains the authoritative record.
  3. Never send content. Aggregates only. See registry.sync_payload.

This channel is for convenience and visibility, NOT for integrity. Everything
it sends originates on a machine the student controls, so the server must treat
it as a claim. Trustworthy numbers come only from server-side verification of
pushed git history.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
from core import config, registry  # noqa: E402

TIMEOUT = 8


def post(url, body, key):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "ai-attribution/" + C.VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status


def main():
    if not config.sync_enabled():
        return  # purely local install, nothing to do

    payload = registry.sync_payload(config.student_id(), C.VERSION)
    if not payload["projects"]:
        return

    url = config.endpoint() + config.sync_path()
    try:
        post(url, payload, config.api_key())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        # Offline, endpoint down, bad key. All fine: the repo ledger is the
        # record that matters and it is already on disk. Reporting catches up
        # on the next session.
        pass


if __name__ == "__main__":
    C.guard(main)
