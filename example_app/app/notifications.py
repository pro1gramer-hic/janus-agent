import json
import os
import urllib.request


def notify(message: str) -> None:
    webhook = os.environ["NOTIFY_WEBHOOK_URL"]
    if webhook == "disabled":
        return
    data = json.dumps({"text": message}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5)
