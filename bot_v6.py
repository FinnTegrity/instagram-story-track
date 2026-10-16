import os
import json
import time
import base64
import hashlib
import re
import tempfile
import subprocess

import cv2
import numpy as np
from PIL import Image
from datetime import datetime
import shutil
from urllib.parse import urlparse, urlunparse, parse_qsl

import telebot
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


BASE_URL = "https://anonstories.com/"

ACCOUNT_DELAY = 3
CHECK_INTERVAL = 5600

path = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(path, ".env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_IDS_RAW = os.getenv("TELEGRAM_USER_ID", "")
TELEGRAM_USER_IDS = []
for value in TELEGRAM_USER_IDS_RAW.split(","):
    value = value.strip()
    if value:
        try:
            TELEGRAM_USER_IDS.append(int(value))
        except ValueError:
            raise ValueError(f"Invalid Telegram user ID: {value}")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_USER_IDS:
    raise ValueError(
        "TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID must be set in .env"
    )

# Remove accidental duplicate IDs while preserving order.
TELEGRAM_USER_IDS = list(dict.fromkeys(TELEGRAM_USER_IDS))

tb = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


# ============================================================
# LOG
# ============================================================

def log(message):
    timestamp = datetime.now().strftime("%d/%m/%Y - %H:%M:%S")
    text = f"[{timestamp}] {message}"

    print(text)

    with open(
        os.path.join(path, "log.txt"),
        "a",
        encoding="utf-8"
    ) as f:
        f.write(text + "\n")


# ============================================================
# DATA
# ============================================================

def load_data():
    """Load v3/v4/v5/v6 per-recipient history and migrate the old list format safely.

    Old format:
        ["legacy_story_key", ...]

    New format:
        {
            "version": 3,
            "recipients": {
                "123": ["story_key", ...],
                "456": ["story_key", ...]
            }
        }

    During migration, old keys are assigned only to the FIRST configured
    recipient. This preserves the old recipient's history without preventing
    newly-added recipients from receiving those stories.
    """
    filename = os.path.join(path, "data.json")

    if not os.path.exists(filename):
        data = {"version": 6, "recipients": {}}
        save_data(data)
        return data

    try:
        with open(filename, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # Already v3
        if isinstance(raw, dict) and raw.get("version") in (3, 4, 5, 6) and isinstance(raw.get("recipients"), dict):
            # Normalize recipient IDs and remove duplicate keys.
            recipients = {}
            for rid, keys in raw["recipients"].items():
                if isinstance(keys, list):
                    recipients[str(rid)] = list(dict.fromkeys(str(k) for k in keys))
            return {"version": 6, "recipients": recipients}

        # Migrate legacy list format.
        if isinstance(raw, list):
            backup = filename + ".legacy-backup"
            if not os.path.exists(backup):
                shutil.copy2(filename, backup)
                log(f"Created legacy data backup: {os.path.basename(backup)}")

            first_id = str(TELEGRAM_USER_IDS[0])
            migrated = {
                "version": 6,
                "recipients": {first_id: list(dict.fromkeys(str(k) for k in raw))}
            }
            save_data(migrated)
            log(f"Migrated {len(raw)} legacy story key(s) to Telegram ID {first_id}")
            return migrated

        raise ValueError("Unsupported data.json format")

    except Exception as e:
        log(f"Error loading data.json: {e}")
        raise


def save_data(data):
    """Atomically save duplicate history."""
    filename = os.path.join(path, "data.json")
    temp_filename = filename + ".tmp"

    with open(temp_filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_filename, filename)


# ============================================================
# ACCOUNTS
# ============================================================

def load_accounts():

    filename = os.path.join(
        path,
        "accounts.txt"
    )

    if not os.path.exists(filename):

        with open(
            filename,
            "w",
            encoding="utf-8"
        ) as f:
            f.write("")

        return []

    with open(
        filename,
        "r",
        encoding="utf-8"
    ) as f:

        accounts = [
            line.strip()
            for line in f
            if line.strip()
        ]

    return list(
        dict.fromkeys(accounts)
    )


# ============================================================
# DECODE SOURCE
# ============================================================

def decode_url(url):

    try:

        encoded = urlparse(
            url
        ).path.lstrip(
            "/"
        ).split("/")[0]

        encoded += "=" * (
            -len(encoded) % 4
        )

        decoded = base64.urlsafe_b64decode(
            encoded
        )

        return decoded.decode(
            "utf-8",
            errors="replace"
        )

    except Exception as e:

        log(f"Decode error: {e}")

        return None


# ============================================================
# STORY FINGERPRINT / DUPLICATE PROTECTION
# ============================================================

VOLATILE_QUERY_KEYS = {
    "token", "expires", "exp", "signature", "sig", "auth",
    "authorization", "policy", "session", "timestamp", "ts",
    "ttl", "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "fbclid", "gclid"
}

STABLE_ID_PATTERNS = [
    re.compile(r"/stories/[^/]+/(\d+)", re.I),
    re.compile(r"/story/[^/]+/(\d+)", re.I),
    re.compile(r"/p/([A-Za-z0-9_-]+)", re.I),
    re.compile(r"/reel/([A-Za-z0-9_-]+)", re.I),
]


def normalize_url(url):
    if not url:
        return ""

    try:
        parsed = urlparse(url.strip())
        kept = []

        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True
        ):
            if key.lower() not in VOLATILE_QUERY_KEYS:
                kept.append((key, value))

        query = "&".join(
            f"{k}={v}"
            for k, v in kept
        )

        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            query,
            ""
        ))

    except Exception:
        return url.strip()


def extract_stable_id(url):
    if not url:
        return None

    for pattern in STABLE_ID_PATTERNS:
        match = pattern.search(url)

        if match:
            return match.group(1)

    try:
        parsed = urlparse(url)

        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True
        ):
            if (
                key.lower()
                in {
                    "story_id",
                    "storyid",
                    "media_id",
                    "mediaid",
                    "media_pk",
                    "pk",
                    "id"
                }
                and value
            ):
                return value

    except Exception:
        pass

    return None


def extract_filename(url):
    if not url:
        return None

    try:
        filename = os.path.basename(
            urlparse(url).path
        )

        if filename:
            return filename.lower()

    except Exception:
        pass

    return None


def hash_text(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def download_media_hash(page, media_url):
    """
    Calculate SHA-256 from the actual media bytes.

    This is the strongest exact duplicate check:
    if the URL changes but the downloaded bytes are identical,
    the content hash remains identical.
    """
    if not media_url:
        return None

    try:
        response = page.request.get(
            media_url,
            timeout=30000
        )

        if not response.ok:
            log(
                f"Media fingerprint HTTP {response.status}: "
                f"{media_url[:120]}"
            )
            return None

        body = response.body()

        if not body:
            return None

        return hashlib.sha256(body).hexdigest()

    except Exception as e:
        log(
            f"Media fingerprint failed: {e}"
        )
        return None


def _average_hash(frame, hash_size=8):
    """Return a compact perceptual hash for one image/frame."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (hash_size, hash_size), interpolation=cv2.INTER_AREA)
    mean_value = float(gray.mean())
    bits = (gray >= mean_value).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _hash_distance(hash_a, hash_b):
    """Hamming distance between two 64-bit average hashes."""
    try:
        return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()
    except Exception:
        return 999


def image_perceptual_fingerprint(body):
    """Perceptual fingerprint for a photo."""
    try:
        array = np.frombuffer(body, dtype=np.uint8)
        frame = cv2.imdecode(array, cv2.IMREAD_COLOR)

        if frame is None:
            return None

        return "img1:" + _average_hash(frame)
    except Exception as e:
        log(f"Image perceptual fingerprint failed: {e}")
        return None


def video_perceptual_fingerprint(body):
    """
    Create a fingerprint from 9 evenly-spaced video frames.

    The actual video bytes do not need to be identical. Re-encoding,
    filename changes, and most URL changes should still produce similar
    frame hashes.
    """
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".mp4",
            delete=False
        ) as temp:
            temp.write(body)
            temp.flush()
            temp_path = temp.name

        cap = cv2.VideoCapture(temp_path)

        if not cap.isOpened():
            return None

        frame_count = int(
            cap.get(cv2.CAP_PROP_FRAME_COUNT)
        )

        fps = float(
            cap.get(cv2.CAP_PROP_FPS)
        )

        duration = (
            frame_count / fps
            if fps > 0 and frame_count > 0
            else 0
        )

        if frame_count <= 0:
            cap.release()
            return None

        # Nine positions make the fingerprint much harder to collide
        # accidentally than a single-frame hash.
        positions = np.linspace(
            0,
            max(frame_count - 1, 0),
            9
        ).astype(int)

        hashes = []

        for position in positions:
            cap.set(
                cv2.CAP_PROP_POS_FRAMES,
                int(position)
            )

            ok, frame = cap.read()

            if not ok or frame is None:
                continue

            hashes.append(
                _average_hash(frame)
            )

        cap.release()

        if len(hashes) < 3:
            return None

        # Include a coarse duration bucket. It is only a secondary
        # discriminator and is not used alone to declare a duplicate.
        duration_bucket = round(duration, 1)

        return (
            "vid1:"
            + str(duration_bucket)
            + ":"
            + ",".join(hashes)
        )

    except Exception as e:
        log(f"Video perceptual fingerprint failed: {e}")
        return None

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass


def download_media_fingerprints(page, media_url, media_type):
    """
    Download the media once and calculate:
      - exact SHA-256
      - perceptual image/video fingerprint

    Returns:
        (content_hash, perceptual_fingerprint)
    """
    if not media_url:
        return None, None

    try:
        response = page.request.get(
            media_url,
            timeout=30000
        )

        if not response.ok:
            log(
                f"Media fingerprint HTTP {response.status}: "
                f"{media_url[:120]}"
            )
            return None, None

        body = response.body()

        if not body:
            return None, None

        content_hash = hashlib.sha256(body).hexdigest()

        if media_type == "photo":
            perceptual = image_perceptual_fingerprint(body)
        elif media_type == "video":
            perceptual = video_perceptual_fingerprint(body)
        else:
            perceptual = None

        return content_hash, perceptual

    except Exception as e:
        log(f"Media fingerprint download failed: {e}")
        return None, None


def parse_perceptual_fingerprint(value):
    """Parse v6 perceptual fingerprints into a comparable structure."""
    if not value:
        return None

    try:
        if value.startswith("img1:"):
            return {
                "type": "image",
                "hashes": [value.split(":", 1)[1]]
            }

        if value.startswith("vid1:"):
            _, duration, hashes = value.split(":", 2)
            return {
                "type": "video",
                "duration": float(duration),
                "hashes": hashes.split(",")
            }
    except Exception:
        return None

    return None


def perceptual_is_duplicate(current_fp, old_key):
    """
    Compare a current perceptual fingerprint against one stored key.

    Conservative thresholds are intentional: false positives are more
    acceptable here than accidentally sending a duplicate.
    """
    if not old_key.startswith("perceptual:"):
        return False

    old_fp = parse_perceptual_fingerprint(
        old_key[len("perceptual:"):]
    )
    new_fp = parse_perceptual_fingerprint(current_fp)

    if not old_fp or not new_fp:
        return False

    if old_fp["type"] != new_fp["type"]:
        return False

    if new_fp["type"] == "image":
        return (
            _hash_distance(
                new_fp["hashes"][0],
                old_fp["hashes"][0]
            ) <= 6
        )

    old_hashes = old_fp["hashes"]
    new_hashes = new_fp["hashes"]

    if len(old_hashes) < 3 or len(new_hashes) < 3:
        return False

    # Compare corresponding samples. Allow some encoding/cropping
    # variation, but require most frames to remain visually similar.
    distances = [
        _hash_distance(a, b)
        for a, b in zip(new_hashes, old_hashes)
    ]

    close = sum(distance <= 10 for distance in distances)

    duration_close = abs(
        new_fp["duration"] - old_fp["duration"]
    ) <= max(1.5, new_fp["duration"] * 0.08)

    return (
        duration_close
        and close >= max(6, int(len(distances) * 0.66))
    )


def make_story_keys(username, story, content_hash=None, perceptual_fingerprint=None):
    """
    Build multiple independent duplicate fingerprints.

    Strongest:
        - actual media SHA-256

    Then:
        - stable story/media ID
        - media filename
        - canonical media URL
        - canonical source URL
        - raw media URL (legacy compatibility)
    """
    username = (
        username or ""
    ).strip().lower().lstrip("@")

    media_url = story.get(
        "media_url"
    ) or ""

    source_url = story.get(
        "source_url"
    ) or ""

    keys = []

    # 1. Exact content hash.
    if content_hash:
        keys.append(
            "content:" + content_hash
        )

    # 2. Perceptual fingerprint (re-encoding resistant).
    if perceptual_fingerprint:
        keys.append(
            "perceptual:" + perceptual_fingerprint
        )

    # 3. Stable ID from source/media URL.
    for candidate, label in (
        (source_url, "source_id"),
        (media_url, "media_id")
    ):
        stable_id = extract_stable_id(
            candidate
        )

        if stable_id:
            keys.append(
                label + ":" + hash_text(
                    stable_id
                )
            )

    # 3. Filename.
    filename = extract_filename(
        media_url
    )

    if filename:
        keys.append(
            "filename:" + hash_text(
                filename
            )
        )

    # 4. Canonical media URL.
    normalized_media = normalize_url(
        media_url
    )

    if normalized_media:
        keys.append(
            "media:" + hash_text(
                normalized_media
            )
        )

    # 5. Canonical source URL.
    normalized_source = normalize_url(
        source_url
    )

    if normalized_source:
        keys.append(
            "source:" + hash_text(
                normalized_source
            )
        )

        keys.append(
            "user_source:" + hash_text(
                f"{username}|{normalized_source}"
            )
        )

    # 6. Raw media URL for v3/v4 compatibility.
    if media_url:
        keys.append(
            hash_text(media_url)
        )

    return list(
        dict.fromkeys(keys)
    )


# ============================================================
# CLOUDFLARE
# ============================================================

def cloudflare_detected(page):

    try:

        title = page.title().lower()

        body = page.locator(
            "body"
        ).inner_text(
            timeout=3000
        ).lower()

        keywords = [
            "just a moment",
            "checking your browser",
            "verify you are human",
            "cf-chl",
            "cloudflare"
        ]

        return any(
            keyword in title
            or keyword in body
            for keyword in keywords
        )

    except Exception:
        return False


# ============================================================
# EXTRACT STORIES
# ============================================================

def collect_stories(page):

    stories = []

    try:

        articles = page.locator(
            "article"
        )

        article_count = articles.count()

        log(
            f"Found {article_count} article(s)"
        )

        for i in range(article_count):

            try:

                article = articles.nth(i)

                # ------------------------------------------------
                # VIDEO
                # ------------------------------------------------

                video = article.locator(
                    "video"
                )

                video_count = video.count()

                # ------------------------------------------------
                # IMAGE
                # ------------------------------------------------

                image = article.locator(
                    "img"
                )

                image_count = image.count()

                # ------------------------------------------------
                # SOURCE
                # ------------------------------------------------

                source = article.locator(
                    'a[href*="embed.anonstories.com/"]'
                )

                source_url = None

                if source.count() > 0:

                    source_url = source.first.get_attribute(
                        "href"
                    )

                # ------------------------------------------------
                # MENTIONS
                # ------------------------------------------------

                mention_elements = article.locator(
                    'a#userMention'
                )

                mentions = []

                mention_count = mention_elements.count()

                for j in range(mention_count):

                    element = mention_elements.nth(j)

                    username = element.get_attribute(
                        "user"
                    )

                    if not username:

                        username = element.inner_text()

                    if username:

                        username = (
                            username
                            .strip()
                            .lstrip("@")
                        )

                        if (
                            username
                            and username not in mentions
                        ):

                            mentions.append(
                                username
                            )

                # ------------------------------------------------
                # DETERMINE MEDIA
                # ------------------------------------------------

                media_type = None
                media_url = None

                # VIDEO
                if video_count > 0:

                    media_type = "video"

                    media_url = video.first.get_attribute(
                        "src"
                    )

                # IMAGE
                elif image_count > 0:

                    media_type = "photo"

                    media_url = image.first.get_attribute(
                        "src"
                    )

                # ------------------------------------------------
                # FALLBACK TO SOURCE
                # ------------------------------------------------

                if not media_url and source_url:

                    media_url = decode_url(
                        source_url
                    )

                    if media_url:

                        lower = media_url.lower()

                        if (
                            ".mp4" in lower
                            or "video" in lower
                        ):

                            media_type = "video"

                        elif (
                            ".jpg" in lower
                            or ".jpeg" in lower
                            or ".png" in lower
                            or ".webp" in lower
                            or "image" in lower
                        ):

                            media_type = "photo"

                # ------------------------------------------------
                # IGNORE EMPTY ARTICLES
                # ------------------------------------------------

                if not media_url:

                    continue

                story = {
                    "media_type": media_type,
                    "media_url": media_url,
                    "source_url": source_url,
                    "mentions": mentions
                }

                stories.append(
                    story
                )

                # ------------------------------------------------
                # DEBUG LOG
                # ------------------------------------------------

                log(
                    f"Story {i + 1}: "
                    f"type={media_type}, "
                    f"mentions={mentions}"
                )

            except Exception as e:

                log(
                    f"Error reading article "
                    f"{i + 1}: {e}"
                )

    except Exception as e:

        log(
            f"Error collecting articles: {e}"
        )

    return stories


# ============================================================
# CAPTION
# ============================================================

def make_caption(
    username,
    mentions,
    media_type
):

    if media_type == "video":

        caption = (
            f"🎥 New story from @{username}"
        )

    else:

        caption = (
            f"📸 New story from @{username}"
        )

    if mentions:

        caption += "\n\n👤 Mention: "

        caption += ", ".join(
            f"@{mention}"
            for mention in mentions
        )

    return caption


# ============================================================
# SEND STORY
# ============================================================

def recipient_story_key(recipient_id, story_key):
    """Create a per-recipient sent marker.

    This is important when more than one Telegram user receives the same story:
    one user's successful delivery must not prevent another user from receiving it.
    """
    return f"recipient:{recipient_id}|{story_key}"


def send_story(username, story, data, page):
    """
    Send one story to every recipient.

    IMPORTANT:
    All duplicate keys are RESERVED and saved BEFORE Telegram send.
    This intentionally prioritizes the user's requirement:
    NEVER SEND THE SAME FINGERPRINT TWICE.

    Trade-off:
    If Telegram fails after reservation, that story will not be retried
    automatically for that recipient. This prevents duplicate delivery.
    """
    media_url = story.get("media_url")
    media_type = story.get("media_type")
    mentions = story.get("mentions")

    if not media_url:
        return

    # Download the media once and calculate both exact and perceptual
    # fingerprints. The perceptual fingerprint catches re-encoded media.
    content_hash, perceptual_fingerprint = download_media_fingerprints(
        page,
        media_url,
        media_type
    )

    if content_hash:
        log(
            f"{username}: content SHA256 = "
            f"{content_hash[:16]}..."
        )
    else:
        log(
            f"{username}: content SHA256 unavailable"
        )

    if perceptual_fingerprint:
        log(
            f"{username}: perceptual fingerprint generated"
        )
    else:
        log(
            f"{username}: perceptual fingerprint unavailable"
        )

    story_keys = make_story_keys(
        username,
        story,
        content_hash,
        perceptual_fingerprint
    )

    recipients = data.setdefault(
        "recipients",
        {}
    )

    caption = make_caption(
        username,
        mentions,
        media_type
    )

    for recipient_id in TELEGRAM_USER_IDS:
        rid = str(recipient_id)

        sent_keys = recipients.setdefault(
            rid,
            []
        )

        # ANY matching fingerprint means this exact story has already
        # been reserved/sent to this recipient.
        duplicate = any(
            key in sent_keys
            for key in story_keys
        )

        # Perceptual matching is fuzzy, so it is checked separately.
        # This catches the same photo/video after re-encoding.
        if not duplicate and perceptual_fingerprint:
            duplicate = any(
                perceptual_is_duplicate(
                    perceptual_fingerprint,
                    old_key
                )
                for old_key in sent_keys
            )

        if duplicate:
            log(
                f"{username}: DUPLICATE for "
                f"{recipient_id} - skipped"
            )
            continue

        # --------------------------------------------------------
        # RESERVE BEFORE SEND
        # --------------------------------------------------------
        #
        # This is deliberately done BEFORE Telegram.
        # If the program crashes after this point, it will not send
        # the same story again after restart.
        #
        sent_keys.extend(
            key
            for key in story_keys
            if key not in sent_keys
        )

        save_data(data)

        log(
            f"{username}: fingerprint reserved "
            f"for {recipient_id}"
        )

        try:
            if media_type == "video":
                log(
                    f"{username}: sending VIDEO "
                    f"to {recipient_id}..."
                )

                tb.send_video(
                    chat_id=recipient_id,
                    video=media_url,
                    caption=caption
                )

            elif media_type == "photo":
                log(
                    f"{username}: sending PHOTO "
                    f"to {recipient_id}..."
                )

                tb.send_photo(
                    chat_id=recipient_id,
                    photo=media_url,
                    caption=caption
                )

            else:
                log(
                    f"{username}: unsupported media "
                    f"type: {media_type}"
                )

                # Do NOT remove the reservation.
                # The no-duplicate guarantee has priority.
                continue

            log(
                f"{username}: sent successfully "
                f"to {recipient_id}"
            )

        except Exception as e:
            log(
                f"{username}: Telegram send failed "
                f"for {recipient_id}: {e}"
            )

            log(
                f"{username}: NOT retrying automatically "
                f"because fingerprint is already reserved"
            )


# ============================================================
# SEARCH USER
# ============================================================

def search_user(
    page,
    username
):

    log(
        f"Opening AnonStories for {username}"
    )

    page.goto(
        BASE_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    time.sleep(3)

    if cloudflare_detected(page):

        log(
            "Cloudflare detected. "
            "Complete verification manually."
        )

        input(
            "Press ENTER after verification: "
        )

        time.sleep(3)

    # --------------------------------------------
    # Search box
    # --------------------------------------------

    selectors = [
        'input[placeholder*="username" i]',
        'input[placeholder*="instagram" i]',
        'input[type="text"]',
        'input'
    ]

    search_box = None

    for selector in selectors:

        try:

            locator = page.locator(
                selector
            )

            if locator.count() > 0:

                search_box = locator.first

                break

        except Exception:
            pass

    if search_box is None:

        log(
            "Search box not found"
        )

        return []

    try:

        search_box.fill(
            username
        )

        time.sleep(1)

        search_box.press(
            "Enter"
        )

        log(
            f"Searching {username}"
        )

        time.sleep(6)

    except Exception as e:

        log(
            f"Search error: {e}"
        )

        return []

    if cloudflare_detected(page):

        log(
            "Cloudflare appeared after search."
        )

        input(
            "Press ENTER after verification: "
        )

        time.sleep(3)

    return collect_stories(
        page
    )


# ============================================================
# CHECK USER
# ============================================================

def check_user(
    page,
    username,
    data
):

    log(
        f"Checking {username}"
    )

    stories = search_user(
        page,
        username
    )

    log(
        f"{username}: found "
        f"{len(stories)} Story(s)"
    )

    if not stories:
        return

    for index, story in enumerate(
        stories,
        start=1
    ):

        log(
            f"{username}: processing "
            f"Story {index}/{len(stories)}"
        )

        log(
            f"{username}: type = "
            f"{story['media_type']}"
        )

        if story["mentions"]:

            log(
                f"{username}: mention = "
                + ", ".join(
                    f"@{x}"
                    for x in story["mentions"]
                )
            )

        else:

            log(
                f"{username}: no mention"
            )

        send_story(
            username,
            story,
            data,
            page
        )

        time.sleep(1)


# ============================================================
# MAIN
# ============================================================

def main():

    data = load_data()

    accounts = load_accounts()

    if not accounts:

        log(
            "accounts.txt is empty"
        )

        return

    log(
        f"Loaded {len(accounts)} account(s)"
    )

    with sync_playwright() as p:

        while True:

            browser = None

            try:

                log(
                    "================================"
                )

                log(
                    "Starting Chrome..."
                )

                browser = p.chromium.launch(
                    headless=False,
                    channel="chrome",
                    args=[
                        "--no-sandbox"
                    ]
                )

                context = browser.new_context(
                    viewport={
                        "width": 1366,
                        "height": 768
                    }
                )

                page = context.new_page()

                log(
                    "Checking for new stories..."
                )

                accounts = load_accounts()

                for username in accounts:

                    try:

                        check_user(
                            page,
                            username,
                            data
                        )

                    except Exception as e:

                        log(
                            f"{username}: Error: {e}"
                        )

                    log(
                        f"Waiting {ACCOUNT_DELAY} seconds..."
                    )

                    time.sleep(
                        ACCOUNT_DELAY
                    )

            except Exception as e:

                log(
                    f"Browser error: {e}"
                )

            finally:

                if browser:

                    try:
                        browser.close()
                    except Exception:
                        pass

                log(
                    "Browser closed."
                )

            log(
                "Batch completed"
            )

            log(
                f"Waiting {CHECK_INTERVAL} seconds..."
            )

            time.sleep(
                CHECK_INTERVAL
            )


if __name__ == "__main__":
    main()