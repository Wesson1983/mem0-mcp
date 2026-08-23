"""Unit tests for Pydantic schemas (task 6.1, change: test-suite-foundation).

Task 6.1 covers ``AddMemoryArgs`` and ``ToolMessage`` (``schemas.py:22-69``):

``ToolMessage`` (``schemas.py:22-24``):
- A minimal ``BaseModel`` with two required string fields, ``role`` and
  ``content`` (both ``Field(..., ...)`` — the ``...`` makes them required).
- Validates the ``role``/``content`` structure: omitting either field raises
  ``ValidationError`` naming the missing field.

``AddMemoryArgs`` (``schemas.py:42-69``):
- Accepts ``text`` (a plain string) OR ``messages`` (a list of
  ``ToolMessage``); the model itself does not enforce "one of" — that
  disjunction is enforced by the ``add_memory`` tool function
  (``server.py:449-458``), not the schema. The schema only validates field
  types and the ``expiration_date`` format.
- Every field except ``text``/``messages`` is ``Optional`` with a ``None``
  default, so ``model_dump(exclude_none=True)`` omits unset optional fields —
  the payload sent to the OSS server stays flat and contains only the fields
  the caller actually set. This is the contract the tool functions rely on
  (``server.py:448``: ``payload = args.model_dump(exclude_none=True)``).
- ``expiration_date`` is validated by ``_validate_iso_date``
  (``schemas.py:11-19``): a non-``YYYY-MM-DD`` value raises
  ``ValidationError`` with the message ``expiration_date must be in
  YYYY-MM-DD format``.
- ``messages`` accepts a list of plain dicts (Pydantic coerces them into
  ``ToolMessage`` instances), and a malformed dict (missing ``content``)
  raises ``ValidationError`` whose ``loc`` points at
  ``('messages', 0, 'content')``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mem0_mcp_server.schemas import AddMemoryArgs, ToolMessage

# ---------------------------------------------------------------------------
# Task 6.1 — ``ToolMessage``
# ---------------------------------------------------------------------------
#
# ``ToolMessage`` is a minimal ``BaseModel`` with two required string fields.
# The ``Field(..., description=...)`` form (the ``...`` sentinel) marks each
# field as required, so omitting either raises ``ValidationError``. The tests
# pin both the happy path (both fields present -> ``model_dump`` round-trips
# them) and the two missing-field error paths, asserting the failing field's
# name via ``error['loc'][0]`` so a regression that swapped the required
# markers or renamed a field is caught precisely.


def test_toolmessage_accepts_role_and_content() -> None:
    """``ToolMessage(role=..., content=...)`` validates and round-trips both
    fields through ``model_dump``.

    The two fields are required strings (``schemas.py:23-24``); supplying both
    produces a model whose ``model_dump()`` returns
    ``{"role": ..., "content": ...}`` with no extra keys and no coercion
    surprises. ``role`` accepts arbitrary strings (the model does not
    enumerate roles — that is the caller's responsibility), so ``"user"`` and
    ``"assistant"`` are both accepted.
    """
    msg = ToolMessage(role="user", content="I like pizza")

    assert msg.role == "user"
    assert msg.content == "I like pizza"
    assert msg.model_dump() == {"role": "user", "content": "I like pizza"}


@pytest.mark.parametrize(
    ("kwargs", "missing_field"),
    [
        ({"content": "hi"}, "role"),
        ({"role": "user"}, "content"),
    ],
    ids=["missing-role", "missing-content"],
)
def test_toolmessage_rejects_missing_required_field(
    kwargs: dict[str, str], missing_field: str
) -> None:
    """Omitting a required field (``role`` or ``content``) raises
    ``ValidationError`` whose first error's ``loc[0]`` names the missing
    field.

    Both fields use ``Field(..., ...)`` (``schemas.py:23-24``), so each is
    required. The assertion reads ``errors()[0]["loc"][0]`` rather than
    matching the whole error string, so a regression that renamed a field or
    dropped the ``...`` sentinel is isolated to the specific field that broke.
    """
    with pytest.raises(ValidationError) as exc_info:
        ToolMessage(**kwargs)

    errors = exc_info.value.errors()
    assert errors, "ValidationError produced no error entries"
    assert errors[0]["loc"][0] == missing_field


def test_toolmessage_accepts_assistant_role() -> None:
    """``role`` is a free-form string — ``"assistant"`` (the other common
    conversational role) is accepted alongside ``"user"``.

    The model does not constrain ``role`` to an enum (``schemas.py:23``), so
    any string validates. This pins that contract: a future change that added
    a role enum without updating this test would break it, surfacing the
    narrowing.
    """
    msg = ToolMessage(role="assistant", content="Sure, I'll remember that.")

    assert msg.role == "assistant"
    assert msg.content == "Sure, I'll remember that."


# ---------------------------------------------------------------------------
# Task 6.1 — ``AddMemoryArgs``: accepts ``text`` or ``messages``
# ---------------------------------------------------------------------------
#
# The schema itself does NOT enforce "exactly one of text/messages" — both
# can be set, and both can be omitted, without a ValidationError. The
# disjunction is enforced by the ``add_memory`` tool function
# (``server.py:449-458``), which returns ``_error("messages_missing", ...)``
# when neither is set. The schema tests therefore cover what the schema
# actually validates: type coercion (``messages`` dicts -> ``ToolMessage``)
# and the ``expiration_date`` format. The "neither supplied" tool-level
# error path is covered by task 8.3, not here.


def test_add_memory_args_accepts_text_only() -> None:
    """``AddMemoryArgs(text=...)`` with no other fields set validates, and
    ``model_dump(exclude_none=True)`` returns ``{"text": ...}`` only — every
    other field is ``Optional`` with a ``None`` default and is excluded.

    This is the shape ``add_memory`` builds when the caller passes ``text``
    (``server.py:436-448``): the schema accepts it, and the exclude_none dump
    keeps the payload flat so the OSS server receives only the fields that
    were set.
    """
    args = AddMemoryArgs(text="likes pizza")  # type: ignore[call-arg]

    assert args.text == "likes pizza"
    assert args.messages is None
    assert args.model_dump(exclude_none=True) == {"text": "likes pizza"}


def test_add_memory_args_accepts_messages_only() -> None:
    """``AddMemoryArgs(messages=[...])`` with no other fields set validates,
    and ``model_dump(exclude_none=True)`` returns ``{"messages": [...]}``
    only.

    ``messages`` is typed ``Optional[list[ToolMessage]]``
    (``schemas.py:46``); passing a list of plain dicts exercises Pydantic's
    coercion — each dict is validated and converted into a ``ToolMessage``
    instance. The dumped form serializes each ``ToolMessage`` back to a
    ``{"role", "content"}`` dict, matching the shape the OSS server expects.
    """
    args = AddMemoryArgs(  # type: ignore[call-arg]
        messages=[
            {"role": "user", "content": "I like pizza"},  # type: ignore[list-item]
            {"role": "assistant", "content": "Noted."},  # type: ignore[list-item]
        ]
    )

    assert args.text is None
    assert args.messages is not None
    assert len(args.messages) == 2
    # Pydantic coerced the dicts into ToolMessage instances.
    assert all(isinstance(m, ToolMessage) for m in args.messages)
    assert args.messages[0].role == "user"
    assert args.messages[0].content == "I like pizza"
    assert args.messages[1].role == "assistant"
    assert args.messages[1].content == "Noted."

    assert args.model_dump(exclude_none=True) == {
        "messages": [
            {"role": "user", "content": "I like pizza"},
            {"role": "assistant", "content": "Noted."},
        ]
    }


def test_add_memory_args_accepts_text_and_messages_simultaneously() -> None:
    """The schema does not enforce "one of text/messages" — both can be set
    without a ``ValidationError``.

    The disjunction is enforced by the ``add_memory`` tool function
    (``server.py:449-460``: if ``messages`` is set it pops ``text`` from the
    payload), not by the schema. This test pins that schema-level contract:
    both fields present validates, and ``model_dump(exclude_none=True)``
    includes both. A regression that added a model-level "one of" validator
    would break this test, surfacing the change.
    """
    args = AddMemoryArgs(  # type: ignore[call-arg]
        text="likes pizza",
        messages=[{"role": "user", "content": "I like pizza"}],  # type: ignore[list-item]
    )

    assert args.text == "likes pizza"
    assert args.messages is not None
    dumped = args.model_dump(exclude_none=True)
    assert "text" in dumped
    assert "messages" in dumped


def test_add_memory_args_accepts_no_text_and_no_messages() -> None:
    """``AddMemoryArgs()`` with neither ``text`` nor ``messages`` validates
    (both are ``Optional`` with ``None`` defaults) and dumps to ``{}``.

    The schema does not reject the empty case — the ``add_memory`` tool
    function does, returning ``_error("messages_missing", ...)``
    (``server.py:455-458``). That tool-level error path is task 8.3's scope;
    here we pin that the schema itself accepts the empty construction so the
    tool function is the sole enforcer of the "at least one" rule.
    """
    args = AddMemoryArgs()  # type: ignore[call-arg]

    assert args.text is None
    assert args.messages is None
    assert args.model_dump(exclude_none=True) == {}


# ---------------------------------------------------------------------------
# Task 6.1 — ``AddMemoryArgs``: ``model_dump(exclude_none=True)`` shape
# ---------------------------------------------------------------------------
#
# The tool functions rely on ``exclude_none=True`` to keep the wire payload
# flat (no ``"field": null`` keys). These tests pin that contract for every
# optional field: an unset field is absent from the dump, and a set field is
# present with its value. A regression that changed a field default from
# ``None`` to a sentinel, or that dropped ``exclude_none=True`` at the call
# site, would leak ``null`` keys and fail these assertions.


def test_add_memory_args_exclude_none_omits_all_unset_optional_fields() -> None:
    """With only ``text`` set, ``model_dump(exclude_none=True)`` omits every
    other optional field (``messages``, ``user_id``, ``agent_id``, ``run_id``,
    ``metadata``, ``expiration_date``, ``infer``, ``memory_type``, ``prompt``).

    The full set of optional fields is enumerated explicitly so a regression
    that added a new optional field with a non-``None`` default would be
    caught (the new field would appear in the dump). The expected dump is
    exactly ``{"text": ...}`` — nothing else.
    """
    args = AddMemoryArgs(text="likes pizza")  # type: ignore[call-arg]

    dumped = args.model_dump(exclude_none=True)

    assert dumped == {"text": "likes pizza"}
    # Explicit negative guards for every optional field, so a regression that
    # flipped a default to a non-None value (leaking a key) is caught by name.
    for absent_field in (
        "messages",
        "user_id",
        "agent_id",
        "run_id",
        "metadata",
        "expiration_date",
        "infer",
        "memory_type",
        "prompt",
    ):
        assert absent_field not in dumped, f"{absent_field} leaked into exclude_none dump"


def test_add_memory_args_exclude_none_includes_only_set_optional_fields() -> None:
    """When several optional fields are set, ``model_dump(exclude_none=True)``
    includes exactly those fields (plus any set among ``text``/``messages``)
    and omits the rest.

    Setting ``text``, ``user_id``, ``infer``, and ``metadata`` produces a dump
    with exactly those four keys — the unset fields (``messages``,
    ``agent_id``, ``run_id``, ``expiration_date``, ``memory_type``, ``prompt``)
    are absent. This is the realistic ``add_memory`` payload shape
    (``server.py:436-448``).
    """
    args = AddMemoryArgs(  # type: ignore[call-arg]
        text="likes pizza",
        user_id="user-1",
        infer=True,
        metadata={"source": "test"},
    )

    dumped = args.model_dump(exclude_none=True)

    assert dumped == {
        "text": "likes pizza",
        "user_id": "user-1",
        "infer": True,
        "metadata": {"source": "test"},
    }
    for absent_field in (
        "messages",
        "agent_id",
        "run_id",
        "expiration_date",
        "memory_type",
        "prompt",
    ):
        assert absent_field not in dumped


def test_add_memory_args_exclude_none_keeps_falsy_but_set_values() -> None:
    """``exclude_none=True`` drops only ``None`` — falsy-but-set values
    (``infer=False``, ``metadata={}``, ``text=""``) are kept.

    Pydantic's ``exclude_none`` filters by ``is None``, not by truthiness, so
    ``False``, ``""``, and ``{}`` survive the dump. This matters for
    ``infer=False`` in particular: the caller explicitly disabling fact
    extraction must reach the OSS server as ``"infer": false``, not be
    silently dropped. A regression that switched to ``exclude_defaults=True``
    (which drops falsy defaults) would drop these and fail.
    """
    args = AddMemoryArgs(text="", infer=False, metadata={})  # type: ignore[call-arg]

    dumped = args.model_dump(exclude_none=True)

    assert dumped == {"text": "", "infer": False, "metadata": {}}


# ---------------------------------------------------------------------------
# Task 6.1 — ``AddMemoryArgs``: ``expiration_date`` validation
# ---------------------------------------------------------------------------
#
# ``expiration_date`` is validated by ``_validate_iso_date``
# (``schemas.py:11-19``) via ``field_validator("expiration_date")``
# (``schemas.py:69``). A valid ``YYYY-MM-DD`` string is accepted; anything
# else raises ``ValidationError`` whose message is
# ``"expiration_date must be in YYYY-MM-DD format"``. The validator returns
# ``None`` for ``None`` input, so an unset ``expiration_date`` is excluded by
# ``exclude_none=True`` (covered above).


@pytest.mark.parametrize(
    "date",
    ["2026-08-23", "2025-01-01", "2099-12-31", "2026-8-23"],
    ids=["today", "new-year", "far-future", "single-digit-month"],
)
def test_add_memory_args_accepts_valid_iso_expiration_date(date: str) -> None:
    """A ``YYYY-MM-DD`` string is accepted as ``expiration_date`` and appears
    in the ``exclude_none`` dump.

    ``_validate_iso_date`` (``schemas.py:11-19``) parses the string with
    ``datetime.strptime(value, "%Y-%m-%d")``; a successful parse returns the
    string unchanged. Note that ``strptime``'s ``%m``/``%d`` directives are
    *not* strict about zero-padding — ``"2026-8-23"`` (single-digit month)
    parses successfully (verified: ``datetime.strptime("2026-8-23",
    "%Y-%m-%d")`` -> ``2026-08-23``). So the validator accepts both
    zero-padded and single-digit month/day forms; this is the actual
    ``strptime`` behavior, not a regex-based strict ``YYYY-MM-DD`` check.
    The ``single-digit-month`` row pins that acceptance so a future change
    to a stricter regex would break it and surface the narrowing.
    """
    args = AddMemoryArgs(expiration_date=date)  # type: ignore[call-arg]

    assert args.expiration_date == date
    assert args.model_dump(exclude_none=True) == {"expiration_date": date}


def test_add_memory_args_rejects_non_leap_year_february_29() -> None:
    """``2026-02-29`` (Feb 29 in a non-leap year) raises ``ValidationError``.

    ``strptime`` is calendar-aware: it rejects Feb 29 for non-leap years
    (verified: ``datetime.strptime("2026-02-29", "%Y-%m-%d")`` raises
    ``ValueError: day 29 must be in range 1..28 for month 2 in year 2026``).
    This pins that the validator uses ``strptime`` (calendar-aware) rather
    than a naive regex that would accept any ``MM=02, DD=29`` regardless of
    year. A regression that swapped to a regex would accept this input and
    break this test.
    """
    with pytest.raises(ValidationError, match="expiration_date must be in YYYY-MM-DD format"):
        AddMemoryArgs(expiration_date="2026-02-29")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "date",
    [
        "not-a-date",
        "2026/08/23",
        "23-08-2026",
        "2026-08",
        "",
    ],
    ids=[
        "garbage",
        "slashes",
        "day-first",
        "year-month-only",
        "empty-string",
    ],
)
def test_add_memory_args_rejects_invalid_expiration_date(date: str) -> None:
    """A non-``YYYY-MM-DD`` ``expiration_date`` raises ``ValidationError``
    whose message names the expected format.

    ``_validate_iso_date`` (``schemas.py:17-18``) raises
    ``ValueError("expiration_date must be in YYYY-MM-DD format")``, which
    Pydantic wraps into a ``ValidationError``. The match string checks the
    message text so a regression that changed the message would be caught.
    The empty string (``""``) is included: ``strptime("", "%Y-%m-%d")`` raises
    ``ValueError``, so the validator rejects it rather than treating it as
    unset (``None`` is the unset sentinel, not ``""``).

    Note: ``"2026-8-23"`` (single-digit month) is *not* in this reject list
    because ``strptime`` accepts it (see
    ``test_add_memory_args_accepts_valid_iso_expiration_date``). The reject
    cases here are the ones ``strptime`` genuinely refuses: wrong separators
    (``/``), wrong field order (``DD-MM-YYYY``), missing fields
    (``YYYY-MM``), and unparseable strings.
    """
    with pytest.raises(ValidationError, match="expiration_date must be in YYYY-MM-DD format"):
        AddMemoryArgs(expiration_date=date)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Task 6.1 — ``AddMemoryArgs``: malformed ``messages`` validation
# ---------------------------------------------------------------------------
#
# ``messages`` is ``Optional[list[ToolMessage]]`` (``schemas.py:46``). Pydantic
# coerces each list element through ``ToolMessage``'s validation, so a dict
# missing ``content`` (or ``role``) raises ``ValidationError`` whose ``loc``
# points at the offending list index and field. This is what the
# ``add_memory`` tool function catches and converts into
# ``_error("invalid_messages", ...)`` (``server.py:431-433``); the schema
# test pins the underlying ValidationError shape.


def test_add_memory_args_rejects_messages_missing_content() -> None:
    """A ``messages`` entry dict missing ``content`` raises
    ``ValidationError`` whose error ``loc`` is
    ``('messages', 0, 'content')``.

    Pydantic coerces the dict into a ``ToolMessage`` (``schemas.py:46``),
    which requires ``content`` (``schemas.py:24``). The error location
    identifies the list index (``0``) and the missing field (``content``),
    so the tool function can surface a precise diagnostic. The ``loc`` is
    asserted exactly (as a tuple) so a regression that changed the field name
    or the list nesting would be caught.
    """
    with pytest.raises(ValidationError) as exc_info:
        AddMemoryArgs(messages=[{"role": "user"}])  # type: ignore[call-arg, list-item]

    errors = exc_info.value.errors()
    assert errors, "ValidationError produced no error entries"
    assert errors[0]["loc"] == ("messages", 0, "content")


def test_add_memory_args_rejects_messages_missing_role() -> None:
    """A ``messages`` entry dict missing ``role`` raises ``ValidationError``
    whose error ``loc`` is ``('messages', 0, 'role')``.

    Symmetric to the missing-content case: ``role`` is required
    (``schemas.py:23``), so omitting it fails at list index 0.
    """
    with pytest.raises(ValidationError) as exc_info:
        AddMemoryArgs(messages=[{"content": "hi"}])  # type: ignore[call-arg, list-item]

    errors = exc_info.value.errors()
    assert errors, "ValidationError produced no error entries"
    assert errors[0]["loc"] == ("messages", 0, "role")


def test_add_memory_args_rejects_messages_with_non_string_content() -> None:
    """A ``messages`` entry with a non-string ``content`` (e.g. an int)
    raises ``ValidationError`` pointing at ``('messages', 0, 'content')``.

    ``ToolMessage.content`` is typed ``str`` (``schemas.py:24``); Pydantic
    does not coerce ``int`` -> ``str`` in strict mode, so an int content
    fails validation. This pins that the field is string-typed, not
    ``Any`` — a regression that widened it to ``Any`` would accept the int
    and break this test.
    """
    with pytest.raises(ValidationError) as exc_info:
        AddMemoryArgs(messages=[{"role": "user", "content": 123}])  # type: ignore[call-arg, list-item]

    errors = exc_info.value.errors()
    assert errors, "ValidationError produced no error entries"
    assert errors[0]["loc"] == ("messages", 0, "content")
