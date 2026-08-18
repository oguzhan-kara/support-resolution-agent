"""
Test doubles for the platform clients.

Every fake records what was asked of it. The point is to assert on *effects* —
what the agent would actually have done to a customer — rather than on the text
it produced. The inherited harness could not do this: it called the real `run()`,
which posts real replies and fills the real Tier-2 queue (eval_harness.py:44 via
sra_runtime.py:132,135,143).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #


@dataclass
class FakeToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeResponse:
    text: str = ""
    tool_calls: list[FakeToolCall] = field(default_factory=list)
    prompt_tokens: int = 1_000
    completion_tokens: int = 100


class FakeLLM:
    """Replays a scripted sequence of responses; repeats the last one forever."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses) or [FakeResponse(text="{}")]
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_messages(self) -> list[dict[str, Any]]:
        return self.calls[-1]["messages"]


def replies(body: str, confidence: float = 0.9) -> FakeResponse:
    import json

    return FakeResponse(
        text=json.dumps({"action": "reply", "body": body, "confidence": confidence})
    )


def decides(payload: object) -> FakeResponse:
    import json

    return FakeResponse(text=json.dumps(payload))


def calls_tool(name: str, **args: Any) -> FakeResponse:
    return FakeResponse(tool_calls=[FakeToolCall(name=name, args=args)])


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #


def doc(text: str, version: str = "v14", age_days: int = 5) -> dict[str, Any]:
    return {"text": text, "version": version, "age_days": age_days}


class FakeKB:
    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self.chunks = chunks if chunks is not None else [doc("Default documentation.")]
        self.fetch_product_brain_calls = 0
        self.search_calls: list[str] = []
        self.known_issues_calls: list[str] = []

    def fetch_product_brain(self) -> list[dict[str, Any]]:
        self.fetch_product_brain_calls += 1
        return self.chunks

    def search(self, query: str = "", **_: Any) -> list[dict[str, Any]]:
        self.search_calls.append(query)
        return self.chunks[:1]

    def known_issues(self, query: str = "", **_: Any) -> list[dict[str, Any]]:
        self.known_issues_calls.append(query)
        return []


class BrokenKB(FakeKB):
    """Raises a chosen exception from every tool call."""

    def __init__(self, error: BaseException, chunks: list[dict[str, Any]] | None = None) -> None:
        super().__init__(chunks)
        self.error = error
        self.attempts = 0

    def search(self, query: str = "", **_: Any) -> list[dict[str, Any]]:
        self.attempts += 1
        raise self.error


# --------------------------------------------------------------------------- #
# CRM
# --------------------------------------------------------------------------- #


def customer(
    account_id: str = "acct-1",
    status: str = "active",
    product_version: str = "v14",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "status": status,
        "product_version": product_version,
        "email_domains": extra.pop("email_domains", ["customer.com"]),
        **extra,
    }


def admin_contacts(
    emails: tuple[str, ...] = ("admin@customer.com",), age_days: int | None = 10
) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    return {
        "admin_emails": list(emails),
        "last_updated": None if age_days is None else (now - timedelta(days=age_days)).isoformat(),
    }


def directory(*users: dict[str, Any]) -> list[dict[str, Any]]:
    return list(users) or [
        {"email": "jane@customer.com", "active": True, "modules": [], "display_name": "Jane Doe"}
    ]


class FakeCRM:
    def __init__(
        self,
        record: dict[str, Any] | None = None,
        admin_contacts: dict[str, Any] | None = None,
        directory: list[dict[str, Any]] | None = None,
    ) -> None:
        self.record = record if record is not None else customer()
        self.admin_contacts = admin_contacts if admin_contacts is not None else {}
        self.directory = directory if directory is not None else []
        self.fetch_customer_record_calls = 0

    def fetch_customer_record(self, account_id: str) -> dict[str, Any]:
        self.fetch_customer_record_calls += 1
        return self.record

    def get_field(self, account_id: str, field_name: str, **_: Any) -> Any:
        return self.record.get(field_name)

    def fetch_admin_contacts(self, account_id: str) -> dict[str, Any]:
        return self.admin_contacts

    def fetch_directory(self, account_id: str) -> list[dict[str, Any]]:
        return self.directory


# --------------------------------------------------------------------------- #
# Ticketing
# --------------------------------------------------------------------------- #


class FakeTicketing:
    def __init__(self, recent: list[dict[str, Any]] | None = None) -> None:
        self.recent = recent or []
        self.post_reply_calls: list[dict[str, Any]] = []
        self.add_note_calls: list[tuple[str, str]] = []
        self.assign_to_queue_calls: list[tuple[str, str]] = []

    def recent_tickets(self, account_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.recent[:limit]

    def history(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.recent

    def post_reply(self, ticket_id: str, body: str, close: bool = False) -> None:
        self.post_reply_calls.append({"ticket_id": ticket_id, "body": body, "close": close})

    def add_note(self, ticket_id: str, note: str) -> None:
        self.add_note_calls.append((ticket_id, note))

    def assign_to_queue(self, ticket_id: str, queue: str) -> None:
        self.assign_to_queue_calls.append((ticket_id, queue))


# --------------------------------------------------------------------------- #
# Provisioning — the only client that changes a customer's environment
# --------------------------------------------------------------------------- #


class FakeProvisioning:
    """
    Records every call. Can be told to fail, so that the executor's behaviour on
    an ambiguous outcome — the state where our records and the customer's
    reality may disagree — is testable.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str]] = []
        self.revoke_calls: list[tuple[str, str, str]] = []

    def fail_with(self, error: BaseException) -> None:
        self.error = error

    def grant_module_access(self, account_id: str, user_email: str, module: str) -> dict[str, Any]:
        self.calls.append((account_id, user_email, module))
        if self.error is not None:
            raise self.error
        return {"granted": True}

    def revoke_module_access(self, account_id: str, user_email: str, module: str) -> dict[str, Any]:
        self.revoke_calls.append((account_id, user_email, module))
        return {"revoked": True}


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def make_deps(
    llm: Any = None,
    kb: Any = None,
    crm: Any = None,
    ticketing: Any = None,
    provisioning: Any = None,
):
    """Build a Deps bundle with fakes and a no-op sleep, so tests never wait."""
    from sra_runtime import Deps

    return Deps(
        llm=llm or FakeLLM(replies("ok")),
        kb=kb or FakeKB(),
        crm=crm or FakeCRM(),
        ticketing=ticketing or FakeTicketing(),
        provisioning=provisioning or FakeProvisioning(),
        sleep=lambda _seconds: None,
    )
