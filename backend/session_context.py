"""Conversation context: compact stored representation of returned sessions
and reference resolution ("#2", "the second one", "that session").

Single-user personal app: conversation context is persisted in the same
DynamoDB table (item CONV#<appSessionId>#<conversationId>) with a short TTL, so
references like "#2" survive across Lambda invocations. Only a COMPACT
representation is stored (ordinal -> id/code/title), never the full
abstract/catalog, to keep the model prompt and storage small.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import config
from models import Session

# Ordinal words -> number, for "the second one" style references.
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6,
    "7th": 7, "8th": 8, "9th": 9, "10th": 10,
}

# Session code pattern, e.g. ANT301, AIM244-R, DVT210
_CODE_RE = re.compile(r"\b([A-Z]{2,4}\d{2,4}(?:-[A-Z0-9]+)?)\b")
# "#2" / "# 2"
_HASH_RE = re.compile(r"#\s*(\d{1,3})\b")
# "number 2", "session 2", "option 2"
_NUM_WORD_RE = re.compile(r"\b(?:number|session|option|no\.?)\s+(\d{1,3})\b", re.IGNORECASE)


def _now() -> int:
    return int(time.time())


def _conv_key(app_session_id: str, conversation_id: str) -> dict:
    # Namespacing by appSessionId guarantees per-user isolation of context.
    return {
        "PK": {"S": f"CONV#{app_session_id}#{conversation_id}"},
        "SK": {"S": "META"},
    }


def _history_key(app_session_id: str, conversation_id: str) -> dict:
    return {
        "PK": {"S": f"CONV#{app_session_id}#{conversation_id}"},
        "SK": {"S": "HISTORY"},
    }


class ConversationStore:
    """Reads/writes the compact conversation context in DynamoDB."""

    def __init__(self, ddb):
        self.ddb = ddb

    def save_sessions(self, app_session_id: str, conversation_id: str,
                      sessions: list[Session]) -> None:
        """Replace the ordinal map with the most recently shown list."""
        ordinal_entries = []
        for idx, s in enumerate(sessions, start=1):
            ordinal_entries.append({"M": {
                "position": {"N": str(idx)},
                "sessionId": {"S": s.session_id},
                "sessionCode": {"S": s.abbreviation or ""},
                "title": {"S": (s.title or "")[:200]},
            }})
        item = {
            **_conv_key(app_session_id, conversation_id),
            "ordinalList": {"L": ordinal_entries},
            "ttl": {"N": str(_now() + config.CONVERSATION_CTX_TTL_SECONDS)},
        }
        if sessions:
            item["lastSessionId"] = {"S": sessions[0].session_id}
        self.ddb.put_item(TableName=config.DDB_TABLE_NAME, Item=item)

    def set_last_session(self, app_session_id: str, conversation_id: str,
                         session_id: str) -> None:
        """Record the session the user most recently acted on ('that session')."""
        key = _conv_key(app_session_id, conversation_id)
        self.ddb.update_item(
            TableName=config.DDB_TABLE_NAME,
            Key=key,
            UpdateExpression="SET lastSessionId = :s, #ttl = :t",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": {"S": session_id},
                ":t": {"N": str(_now() + config.CONVERSATION_CTX_TTL_SECONDS)},
            },
        )

    def load(self, app_session_id: str, conversation_id: str) -> dict:
        """Return {ordinal_map: {int: {...}}, last_session_id: str|None}."""
        resp = self.ddb.get_item(
            TableName=config.DDB_TABLE_NAME,
            Key=_conv_key(app_session_id, conversation_id),
        )
        item = resp.get("Item")
        if not item:
            return {"ordinal_map": {}, "last_session_id": None}
        if int(item.get("ttl", {}).get("N", "0")) < _now():
            return {"ordinal_map": {}, "last_session_id": None}
        ordinal_map: dict[int, dict] = {}
        for entry in item.get("ordinalList", {}).get("L", []):
            m = entry.get("M", {})
            pos = int(m.get("position", {}).get("N", "0"))
            ordinal_map[pos] = {
                "sessionId": m.get("sessionId", {}).get("S"),
                "sessionCode": m.get("sessionCode", {}).get("S"),
                "title": m.get("title", {}).get("S"),
            }
        return {
            "ordinal_map": ordinal_map,
            "last_session_id": item.get("lastSessionId", {}).get("S"),
        }


    def append_history(self, app_session_id: str, conversation_id: str,
                       user_text: str, assistant_text: str) -> None:
        """Append one user+assistant turn to the conversation history (a
        separate CONV#..#..|HISTORY item), capping to the most recent
        MAX_HISTORY_TURNS and truncating long messages."""
        cap_chars = config.MAX_HISTORY_MSG_CHARS
        turn = {"M": {
            "u": {"S": (user_text or "")[:cap_chars]},
            "a": {"S": (assistant_text or "")[:cap_chars]},
        }}
        existing = self.load_history_raw(app_session_id, conversation_id)
        existing.append(turn)
        # Keep only the most recent N turns.
        existing = existing[-config.MAX_HISTORY_TURNS:]
        self.ddb.put_item(
            TableName=config.DDB_TABLE_NAME,
            Item={
                **_history_key(app_session_id, conversation_id),
                "turns": {"L": existing},
                "ttl": {"N": str(_now() + config.CONVERSATION_CTX_TTL_SECONDS)},
            },
        )

    def load_history_raw(self, app_session_id: str, conversation_id: str) -> list:
        resp = self.ddb.get_item(
            TableName=config.DDB_TABLE_NAME,
            Key=_history_key(app_session_id, conversation_id),
        )
        item = resp.get("Item")
        if not item or int(item.get("ttl", {}).get("N", "0")) < _now():
            return []
        return list(item.get("turns", {}).get("L", []))

    def load_history_messages(self, app_session_id: str, conversation_id: str) -> list:
        """Return prior turns as Strands `messages` (role/content blocks) to
        seed the agent so it remembers earlier context."""
        messages = []
        for turn in self.load_history_raw(app_session_id, conversation_id):
            m = turn.get("M", {})
            u = m.get("u", {}).get("S")
            a = m.get("a", {}).get("S")
            if u:
                messages.append({"role": "user", "content": [{"text": u}]})
            if a:
                messages.append({"role": "assistant", "content": [{"text": a}]})
        return messages


def extract_reference(message: str, context: dict) -> Optional[str]:
    """Resolve a reference in `message` to a sessionId using `context`.

    Resolution order:
      1. Explicit session code (e.g. "ANT301") present in the ordinal map.
      2. "#N" / "number N" / "the Nth one" -> ordinal N of the most recent list.
      3. "that/this/the previous session" -> lastSessionId.
    Returns None if nothing resolvable (caller then asks for clarification or
    treats the message as a fresh search).
    """
    ordinal_map: dict[int, dict] = context.get("ordinal_map", {}) or {}
    last_session_id = context.get("last_session_id")

    # 1) explicit code that matches something we've shown
    for code in _CODE_RE.findall(message.upper()):
        for entry in ordinal_map.values():
            if entry.get("sessionCode", "").upper() == code:
                return entry["sessionId"]

    # 2) ordinal references
    n = _extract_ordinal(message)
    if n is not None:
        entry = ordinal_map.get(n)
        if entry:
            return entry["sessionId"]
        # Out of range -> explicitly NOT resolved (never fabricate).
        return None

    # 3) "that/this/the previous" session
    if re.search(r"\b(that|this|the previous|the last|it)\b", message, re.IGNORECASE) \
            and re.search(r"\bsession|one|workshop|talk\b", message, re.IGNORECASE):
        return last_session_id

    return None


def _extract_ordinal(message: str) -> Optional[int]:
    m = _HASH_RE.search(message)
    if m:
        return int(m.group(1))
    m = _NUM_WORD_RE.search(message)
    if m:
        return int(m.group(1))
    lowered = message.lower()
    for word, num in _ORDINAL_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return num
    return None


def explicit_code_in(message: str) -> Optional[str]:
    """Return a session code if the user typed one directly (e.g. GetSession by code)."""
    codes = _CODE_RE.findall(message.upper())
    return codes[0] if codes else None
