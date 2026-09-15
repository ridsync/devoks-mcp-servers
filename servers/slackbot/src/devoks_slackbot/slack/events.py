"""Slack event payload parsing — pure functions (TASK-004).

Distinguishes ``url_verification`` handshakes from ``event_callback``/
``app_mention`` events, filters out the bot's own messages before any work
starts (``EDGE-SB-011``, ``AC-SB-006-3``), and gives every other module in
this package a single place to ask "who asked this?" (``extract_user_id``,
``EDGE-SB-019``).

``CTR-SB-006`` assumes the questioner's identifier is ``event.user``, a
``U...``-shaped Slack user ID. That assumption is unverified against a real
workspace until ``TASK-030`` — Enterprise Grid can vary the ID's shape per
workspace, and ``authorizations[].user_id`` (the app *installer*, not the
person asking) looks identical in shape at a glance. Every caller in this
codebase must go through ``extract_user_id`` rather than reaching into
``payload["event"]["user"]`` directly, so that when ``TASK-030`` confirms or
corrects the real field/shape against a live payload, this one function's
body is the only edit required — no other module should ever index the
payload for the questioner itself.

Like ``slack/signature.py`` (TASK-003), this module takes a plain
``Mapping``/``str``/``dict`` in and returns plain values out; it never
imports anything HTTP/ASGI/Lambda-shaped (``DSN-SB-007``). Every function
here never raises — a malformed or unexpected payload shape (a missing
``event``, a non-dict ``event``, a missing/non-string field, an unknown
``type``) resolves to ``False``/``None``, never an exception, so a real-world
payload surprise degrades to "skip this event" rather than a 500.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

#: Slack's top-level payload ``type`` values this module distinguishes.
TYPE_URL_VERIFICATION = "url_verification"
TYPE_EVENT_CALLBACK = "event_callback"

#: The only inner ``event.type`` this Stage acts on (FRD §1 — @-mentions).
EVENT_TYPE_APP_MENTION = "app_mention"


def is_url_verification(payload: Mapping[str, Any]) -> bool:
    """Return True iff ``payload`` is Slack's ``url_verification`` handshake.

    ``EDGE-SB-003``: the caller is responsible for verifying the request
    signature *before* trusting this — this function makes no such check.
    """
    return _get_str(payload, "type") == TYPE_URL_VERIFICATION


def extract_challenge(payload: Mapping[str, Any]) -> str | None:
    """Return the ``challenge`` value to echo back, or None if absent/malformed.

    Intended to be called after ``is_url_verification(payload)`` is True; it
    does not itself check ``type``. A missing or non-string ``challenge`` —
    a payload variant this module must survive without raising — returns
    None so the caller decides how to respond instead of crashing.
    """
    return _get_str(payload, "challenge")


def is_app_mention_event(payload: Mapping[str, Any]) -> bool:
    """Return True iff ``payload`` is an ``event_callback`` wrapping an
    ``app_mention`` inner event (``CTR-SB-006``'s payload shape).

    Any other top-level ``type`` (unknown, absent, or non-string), a
    missing/non-dict ``event``, or an inner ``event.type`` other than
    ``app_mention`` all resolve to False rather than raising.
    """
    if _get_str(payload, "type") != TYPE_EVENT_CALLBACK:
        return False
    event = _get_event(payload)
    if event is None:
        return False
    return _get_str(event, "type") == EVENT_TYPE_APP_MENTION


def is_bot_self_message(payload: Mapping[str, Any], *, bot_user_id: str) -> bool:
    """Return True iff this event is the bot re-triggering on its own message
    (``EDGE-SB-011``, ``AC-SB-006-3``).

    Checks **both** signals a bot's own message can carry — either alone can
    miss a case Slack actually sends:
    - ``event.user == bot_user_id`` — the bot posted as itself.
    - ``event.bot_id`` present — bot messages can carry ``bot_id`` with no
      ``user`` field at all.

    Must be checked before any work starts (``EDGE-SB-011``): a false
    negative here means the bot's own answer re-triggers itself, and the
    cost compounds without bound. ``bot_user_id`` is a plain keyword
    argument rather than read from settings, so this function stays a pure,
    config-independent unit — supplying ``SLACK_BOT_USER_ID`` is the
    caller's job.
    """
    event = _get_event(payload)
    if event is None:
        return False
    user_id = _get_str(event, "user")
    if user_id is not None and user_id == bot_user_id:
        return True
    return _get_str(event, "bot_id") is not None


def extract_user_id(payload: Mapping[str, Any]) -> str | None:
    """Return the Slack user ID of whoever triggered this event, or None.

    **This is the one function ``CTR-SB-006``'s user-mapping lookup, and
    every other caller in this package, must go through** — never index
    ``payload["event"]["user"]`` directly elsewhere. The reason is
    ``EDGE-SB-019``: this function currently assumes the questioner is
    ``event.user``, but that shape is unverified against a real Slack
    workspace. ``payload["authorizations"][0]["user_id"]`` is *not* the
    questioner — it is whoever installed/authorized the app — and it looks
    identical in shape (``U...``), so it is deliberately not read here.
    When ``TASK-030`` confirms or corrects the real field/shape against a
    live payload, this function's body is the only edit needed; every
    caller is unaffected by construction.

    Returns None for a missing/non-dict ``event``, a missing ``user``, an
    empty string, or a non-string value — never raises.
    """
    event = _get_event(payload)
    if event is None:
        return None
    return _get_str(event, "user")


def extract_event_id(payload: Mapping[str, Any]) -> str | None:
    """Return the top-level ``event_id`` used as the idempotency key, or None.

    None signals "no idempotency key available in this payload" — deciding
    how to treat that (reject, log, fall back) is the caller's job; this
    function does not fabricate a substitute key.
    """
    return _get_str(payload, "event_id")


def extract_channel(payload: Mapping[str, Any]) -> str | None:
    """Return the Slack channel ID the reply should be posted to, or None.

    ``AC-SB-006-1``'s post site (``slack/client.py``'s ``post_message``
    ``channel`` argument) -- worker.py (``TASK-014``) is this function's
    caller. Returns None for a missing/non-dict ``event`` or a missing/
    non-string ``channel`` -- never raises, same contract as every other
    extractor in this module.
    """
    event = _get_event(payload)
    if event is None:
        return None
    return _get_str(event, "channel")


def extract_question_text(payload: Mapping[str, Any]) -> str | None:
    """Return the raw ``event.text`` field, or None.

    Raw means exactly what Slack sent -- for an ``app_mention``, this
    includes the triggering ``<@BOT_USER_ID>`` mention token verbatim (e.g.
    ``"<@U0LAN0Z89> question"``, the shape this module's own test suite pins
    against the official example payload). Stripping that token is
    interpretation, not parsing, so it deliberately does **not** happen here
    -- ``worker.py`` (``TASK-014``) owns that step, keeping this module's
    responsibility limited to "what did the payload literally say"
    (``DSN-SB-007``). Returns None for a missing/non-dict ``event`` or a
    missing/non-string ``text`` -- never raises.
    """
    event = _get_event(payload)
    if event is None:
        return None
    return _get_str(event, "text")


def extract_reply_target_ts(payload: Mapping[str, Any]) -> str | None:
    """Return the timestamp to post the reply to (``AC-SB-006-1``).

    ``event.thread_ts`` when the mention happened inside an existing thread,
    else ``event.ts`` — a top-level mention starts its own thread. Returns
    None if neither is present as a non-empty string.
    """
    event = _get_event(payload)
    if event is None:
        return None
    thread_ts = _get_str(event, "thread_ts")
    if thread_ts is not None:
        return thread_ts
    return _get_str(event, "ts")


def _get_event(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    event = payload.get("event")
    if isinstance(event, Mapping):
        return cast(Mapping[str, Any], event)
    return None


def _get_str(source: Mapping[str, Any], key: str) -> str | None:
    value = source.get(key)
    if isinstance(value, str) and value:
        return value
    return None
