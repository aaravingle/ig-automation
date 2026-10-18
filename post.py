"""Post one image to Instagram per day via the Instagram API (Instagram Login).

Safe to run several times a day: it skips if something was already posted today.
Env: IG_ACCESS_TOKEN (required), POST_TZ, DRY_RUN=1, FORCE=1
"""
import datetime as dt
import glob
import os
import sys
import time
import zoneinfo

import requests

API = "https://graph.instagram.com/v23.0"
TOKEN = os.environ["IG_ACCESS_TOKEN"]
TZ = zoneinfo.ZoneInfo(os.environ.get("POST_TZ", "Asia/Kolkata"))
REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
DRY = os.environ.get("DRY_RUN") == "1"
FORCE = os.environ.get("FORCE") == "1"
TIMEOUT = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # Instagram's JPEG limit is 8 MB


def log(msg):
    print(str(msg).replace(TOKEN, "***"), flush=True)


def call(method, path, retries=3, **params):
    """Call the API with timeout + retry on network/5xx/429. API errors are not retried."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(
                method, f"{API}/{path}",
                params={**params, "access_token": TOKEN}, timeout=TIMEOUT,
            )
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"API error: {data['error']}")
            return data
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            log(f"{path}: attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt * 3)
    raise RuntimeError(f"{path} failed after {retries} attempts: {last}")


def refresh_token():
    """Extends the token's 60-day life. Non-fatal if it fails."""
    try:
        r = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": TOKEN},
            timeout=TIMEOUT,
        )
        data = r.json()
        if "error" in data:
            log(f"WARNING: token refresh failed: {data['error']}")
            return
        days = data.get("expires_in", 0) // 86400
        log(f"Token refreshed, valid for ~{days} more days")
        if data.get("access_token") and data["access_token"] != TOKEN:
            log("WARNING: token string changed; update the IG_ACCESS_TOKEN secret.")
    except Exception as e:  # noqa: BLE001
        log(f"WARNING: token refresh error: {e}")


def posted_today():
    data = call("GET", "me/media", fields="timestamp", limit=10)
    today = dt.datetime.now(TZ).date()
    for item in data.get("data", []):
        ts = dt.datetime.strptime(item["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if ts.astimezone(TZ).date() == today:
            return True
    return False


def pick_image():
    files = sorted(
        glob.glob("images/*.jpg") + glob.glob("images/*.jpeg")
        + glob.glob("images/*.JPG") + glob.glob("images/*.JPEG")
    )
    if not files:
        raise RuntimeError("No .jpg files found in images/ (empty state).")
    chosen = files[dt.datetime.now(TZ).date().toordinal() % len(files)]
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{chosen}"
    return chosen, url


def check_image(url):
    r = requests.get(url, stream=True, timeout=TIMEOUT)
    try:
        if r.status_code != 200:
            raise RuntimeError(f"Image URL returned HTTP {r.status_code} (is the repo public?)")
        if not r.headers.get("content-type", "").startswith("image/jpeg"):
            raise RuntimeError(f"Image is not JPEG: {r.headers.get('content-type')}")
        size = int(r.headers.get("content-length", 0))
        if size > MAX_IMAGE_BYTES:
            raise RuntimeError(f"Image too large: {size} bytes (max 8 MB)")
    finally:
        r.close()


def read_caption():
    with open("caption.txt", encoding="utf-8") as f:
        caption = f.read().strip()
    if not caption:
        raise RuntimeError("caption.txt is empty.")
    if len(caption) > 2200:
        raise RuntimeError("Caption exceeds 2200 characters.")
    if caption.count("#") > 30:
        raise RuntimeError("Caption has more than 30 hashtags.")
    return caption


def main():
    refresh_token()

    if not FORCE and posted_today():
        log("Already posted today; nothing to do.")
        return

    caption = read_caption()
    path, url = pick_image()
    log(f"Using {path}")
    check_image(url)

    if DRY:
        log(f"DRY RUN OK. Would post {url} with {len(caption)}-char caption.")
        return

    container = call("POST", "me/media", image_url=url, caption=caption)["id"]
    log(f"Container created: {container}")

    for _ in range(20):  # up to ~100 s
        status = call("GET", container, fields="status_code").get("status_code")
        log(f"Container status: {status}")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Container failed with status {status}")
        time.sleep(5)
    else:
        raise RuntimeError("Container never reached FINISHED (timeout).")

    try:
        # retries=1: never blindly re-publish, that could create duplicates
        post_id = call("POST", "me/media_publish", retries=1, creation_id=container)["id"]
        log(f"Published! Media id {post_id}")
    except Exception as e:  # noqa: BLE001
        log(f"Publish call errored ({e}); checking whether it went through anyway...")
        time.sleep(15)
        if posted_today():
            log("Post is live despite the error. Treating as success.")
            return
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"FAILED: {e}")
        sys.exit(1)
