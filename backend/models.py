"""Data models for the re:Invent Session Assistant backend.

Design-aligned dataclasses (see design.md "Data Models"). Only the models
needed for Phase 1 (auth) and Phase 2 (MCP connectivity) are exercised here;
the full set is defined so later phases can build on a stable contract.

Security note: TokenBundle holds sensitive material. It is NEVER serialized
toward the browser and NEVER logged. See auth.py / app.py for enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AuthState(str, Enum):
    """Application-level authentication/registration state.

    These are the ONLY values exposed to the browser via GET /auth/status.
    No OAuth internals are ever surfaced.
    """

    UNAUTHENTICATED = "unauthenticated"  # State 1: no valid app session
    AUTHENTICATED = "authenticated"      # transient: signed in, registration not yet probed
    NOT_REGISTERED = "not_registered"    # State 2: AWS Events does not authorize this identity
    AUTHORIZED = "authorized"            # State 3: AWS Events authorizes access to reinvent2026
    ERROR = "error"                      # transient/service error determining access


@dataclass
class SessionTime:
    date: Optional[str] = None       # e.g. "2026-12-01"
    time: Optional[str] = None       # local America/Los_Angeles, e.g. "10:30"
    length: Optional[int] = None     # minutes


@dataclass
class Session:
    """A re:Invent session as returned by the AWS Events MCP tools."""

    session_id: str
    abbreviation: str = ""            # session code, e.g. "ANT301"
    title: str = ""
    type: Optional[str] = None        # "Breakout session", "Workshop", ...
    level: Optional[str] = None       # "300 - Advanced"
    room: Optional[str] = None
    venue: Optional[str] = None
    services: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)   # names only
    session_time: Optional[SessionTime] = None
    is_reservable: bool = False
    is_all_day: bool = False
    abstract: Optional[str] = None    # only populated by GetSession

    @staticmethod
    def from_mcp(item: dict) -> "Session":
        """Map a raw AWS Events MCP session item into a Session.

        All fields are treated as OPTIONAL and never fabricated when missing.
        """
        st = item.get("sessionTime") or {}
        length = st.get("length")
        try:
            length = int(length) if length is not None else None
        except (TypeError, ValueError):
            length = None
        speakers = [
            s.get("name")
            for s in (item.get("speakers") or [])
            if isinstance(s, dict) and s.get("name")
        ]
        return Session(
            session_id=item.get("sessionId", ""),
            abbreviation=item.get("abbreviation", "") or "",
            title=item.get("title", "") or "",
            type=item.get("type"),
            level=item.get("level"),
            room=item.get("room"),
            venue=item.get("venue"),
            services=list(item.get("services") or []),
            topics=list(item.get("topics") or []),
            features=list(item.get("features") or []),
            speakers=speakers,
            session_time=SessionTime(
                date=st.get("date"), time=st.get("time"), length=length
            ) if st else None,
            is_reservable=bool(item.get("isReservable", False)),
            is_all_day=bool(item.get("isAllDaySession", False)),
            abstract=item.get("abstract"),
        )

    def to_public_dict(self) -> dict:
        """Serialize for the frontend. Contains no credentials."""
        d = {
            "sessionId": self.session_id,
            "sessionCode": self.abbreviation,
            "title": self.title,
            "type": self.type,
            "level": self.level,
            "room": self.room,
            "venue": self.venue,
            "services": self.services,
            "topics": self.topics,
            "features": self.features,
            "speakers": self.speakers,
            "isReservable": self.is_reservable,
            "isAllDay": self.is_all_day,
            "abstract": self.abstract,
        }
        if self.session_time:
            d["sessionTime"] = {
                "date": self.session_time.date,
                "time": self.session_time.time,
                "length": self.session_time.length,
            }
        # Drop keys whose value is None so absent fields are omitted, not faked.
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class ActionResult:
    """Structured result of a favorite/unfavorite operation (later phases)."""

    type: str                          # "favorite" | "unfavorite"
    status: str                        # "success" | "failed"
    session_id: str
    session_code: Optional[str] = None
    reason: Optional[str] = None

    def to_public_dict(self) -> dict:
        d = {
            "type": self.type,
            "status": self.status,
            "sessionId": self.session_id,
            "sessionCode": self.session_code,
            "reason": self.reason,
        }
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class TokenBundle:
    """Sensitive OAuth material. NEVER returned to the browser or logged."""

    access_token: str
    refresh_token: Optional[str]
    id_token_claims: dict              # sub, email (raw id token not retained)
    access_expires_at: int             # epoch seconds

    def redacted(self) -> dict:
        """Safe representation for debugging/logging (no secret values)."""
        return {
            "sub": self.id_token_claims.get("sub"),
            "has_refresh_token": self.refresh_token is not None,
            "access_expires_at": self.access_expires_at,
        }


@dataclass
class ConversationContext:
    """Reference-resolution state for later phases."""

    conversation_id: str
    ordinal_map: dict[int, str] = field(default_factory=dict)
    last_session_id: Optional[str] = None
    ttl: int = 0


@dataclass
class ChatRequest:
    """POST /chat request body."""

    conversation_id: str
    message: str

    @staticmethod
    def from_body(body: dict) -> "ChatRequest":
        conv = (body or {}).get("conversationId")
        msg = (body or {}).get("message")
        return ChatRequest(conversation_id=conv or "", message=msg or "")


@dataclass
class ChatResponse:
    """POST /chat response body. `sessions` is structured data derived from real
    MCP tool results — NEVER parsed from the assistant's prose."""

    conversation_id: str
    message: str
    sessions: list[Session] = field(default_factory=list)
    action: Optional[dict] = None  # always None this phase (favorites are later)
    # Structured Schedule_View from a show_schedule turn, rendered as cards by
    # the frontend so the model doesn't have to re-narrate every item (which,
    # on a dense day, blew the agent-turn budget). None unless show_schedule ran.
    schedule: Optional[dict] = None

    def to_public_dict(self) -> dict:
        d = {
            "conversationId": self.conversation_id,
            "message": self.message,
            "sessions": [s.to_public_dict() for s in self.sessions],
            "action": self.action,
        }
        if self.schedule is not None:
            d["schedule"] = self.schedule
        return d


@dataclass
class PersonalTimeEntry:
    """A personal-time block on the user's schedule (Req 17.2, 18.1).

    `start`/`end` are stored in the UTC contract form the MCP tools expect:
    "YYYY-MM-DDTHH:MM:SS" with seconds == "00". Optional fields are never
    fabricated when absent.
    """

    personal_time_id: str
    title: str
    start: str                        # UTC YYYY-MM-DDTHH:MM:SS (contract form)
    end: str                          # UTC YYYY-MM-DDTHH:MM:SS
    description: Optional[str] = None
    location: Optional[str] = None

    @staticmethod
    def from_mcp(item: dict) -> "PersonalTimeEntry":
        """Map a raw AWS Events personal-time item into a PersonalTimeEntry.

        Accepts the common key spellings without fabricating missing values.
        """
        item = item or {}
        return PersonalTimeEntry(
            personal_time_id=item.get("personalTimeId")
            or item.get("personal_time_id")
            or "",
            title=item.get("title", "") or "",
            start=item.get("startDateTime") or item.get("start") or "",
            end=item.get("endDateTime") or item.get("end") or "",
            description=item.get("description"),
            location=item.get("location"),
        )

    def to_public_dict(self) -> dict:
        """Serialize for the frontend. Contains no credentials."""
        d = {
            "personalTimeId": self.personal_time_id,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "description": self.description,
            "location": self.location,
        }
        # Drop keys whose value is None so absent fields are omitted, not faked.
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class ScheduleView:
    """Structured view of the user's schedule (Req 17.2).

    `reserved` and `favorites` reuse the existing `Session` dataclass;
    `personal_time` holds the user's personal-time blocks.
    """

    reserved: list[Session] = field(default_factory=list)       # reuse Session
    favorites: list[Session] = field(default_factory=list)      # reuse Session
    personal_time: list[PersonalTimeEntry] = field(default_factory=list)
    # Honest-completeness marker. For each kind ("reserved" / "favorites") whose
    # session details could not ALL be loaded (size cap, fetch deadline, or
    # sessions AWS Events lists but cannot return), maps kind -> {"loaded": n,
    # "total": m}. Empty when everything loaded, so a cut-off list is never
    # mistaken for the whole schedule.
    incomplete: dict = field(default_factory=dict)
    # Provenance marker. When some sessions' details came from the catalog index
    # rather than a live read, maps kind ("reserved" / "favorites") -> how many,
    # plus "ageSeconds": how old that index is. Empty when everything was read
    # live. Lets the UI and the assistant say so instead of implying the details
    # were just re-checked.
    indexed: dict = field(default_factory=dict)

    def to_public_dict(self) -> dict:
        """Serialize for the frontend. Contains no credentials."""
        d = {
            "reserved": [s.to_public_dict() for s in self.reserved],
            "favorites": [s.to_public_dict() for s in self.favorites],
            "personalTime": [p.to_public_dict() for p in self.personal_time],
        }
        if self.incomplete:
            d["incomplete"] = {k: dict(v) for k, v in self.incomplete.items()}
        if any(self.indexed.get(k) for k in ("reserved", "favorites")):
            d["indexed"] = dict(self.indexed)
        return d
