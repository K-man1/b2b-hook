"""Report the project index to the course server. Runs at session start and end.

Both ends, deliberately. Sending only at SessionEnd meant a student who
installed the plugin, did some work, and never cleanly closed the session saw
0% on the website while their machine held correct numbers, because the
roll-up these figures are drawn from had never been posted. Sessions do not
reliably end: editors get force-quit, laptops sleep, terminals get closed.
Session start always happens, so it is the reliable half of the pair.


This is what powers the website's project picker. It sends aggregate metadata
about every repo the student has worked in, so they can later select which ones
counted toward a submission, the way Hackatime does.

Three rules it must obey:

  1. Never block. Wired as an async hook and given a short timeout, because a
     student on hotel wifi must not wait on our HTTP call to close a session.
  2. Never raise. A missing endpoint, no network, a 500, an expired key: all
     are non-events. Local tracking continues regardless and the next session
     start catches the reporting up.
  3. Never send content. Aggregates only. See registry.sync_payload.

This channel is for convenience and visibility, NOT for integrity. It sends
totals the student's own machine computed, so the server must treat them as a
claim and label them that way. The records streamed by stream.py are the thing
with evidentiary weight, because they were delivered as the work happened.
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


def report():
    """Post the current index. Separate from main() so stream.py can reuse it.

    Session start and session end are not the only moments the website's
    numbers need refreshing; mid-session delivery needs it too, and it needs
    exactly this, not a second copy of it that can drift.
    """
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


def main():
    report()


if __name__ == "__main__":
    C.guard(main)
