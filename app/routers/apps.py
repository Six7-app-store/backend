import contextlib
import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.database import get_db
from app.models import User, UserRole
from app.schemas import (
    AppCreate,
    AppResponse,
    AppUpdate,
    AppVersionApprovalResponse,
    AppVersionApprovalSubmit,
    AppWithVersions,
)
from app.services.git_service import git_service
from app.utils.app_image import build_image_data_url, parse_image_data_url
from app.utils.auth import get_current_user
from app.utils.capabilities import (
    ensure_delete_app,
    ensure_edit_app,
    ensure_submit_app_version,
    ensure_view_app,
)

logger = logging.getLogger(__name__)


def _serialize_app(app):
    """Replace ``app.image`` (bytes) with the data-URL form in-place.

    The ORM model carries the raw bytes plus a separate mime column.
    The Pydantic ``AppResponse`` schema declares ``image: Optional[str]``
    and uses ``from_attributes=True``, so Pydantic reads ``app.image``
    directly. Overwriting that attribute with the rebuilt data-URL
    means the response serialiser sees a string and the wire format
    matches the schema. Returns ``app`` so callers can chain.
    """
    if app is None:
        return None
    raw_bytes = getattr(app, "image", None)
    if isinstance(raw_bytes, (bytes, memoryview, bytearray)):
        app.image = build_image_data_url(bytes(raw_bytes), getattr(app, "image_mime", None))
    return app


def _version_tag(version) -> str:
    """Extract the tag string from a git version entry.

    ``git_service.get_versions`` yields either a plain tag string or a
    dict carrying the tag under one of ``version`` / ``releaseTag`` /
    ``tag``. Returns ``""`` when nothing matches, so callers can treat
    the result uniformly (empty string is falsy and never a valid tag).
    """
    if isinstance(version, str):
        return version
    return version.get("version") or version.get("releaseTag") or version.get("tag", "")


router = APIRouter()


# ----------------------------------------------------------------
# Pydantic schema for the /apps/{id}/variables response
# ----------------------------------------------------------------
# Mirrors the exact keys ``_parse_one_variable`` returns (mixed
# snake/camelCase) so the frontend and generated OpenAPI stay in sync.
class _MarkerErrorPayload(BaseModel):
    variable: str
    message: str
    location: str
    code: str | None = None


class AppVariableResponse(BaseModel):
    """Shape of one entry in ``GET /apps/{id}/variables``.

    Keys match what ``_parse_one_variable`` writes to the dict exactly —
    the frontend reads ``osType``/``osMode``/``osMulti``/``osScope``/
    ``varScope``/``fileExtensions`` in camelCase and the rest in
    snake/lowercase. Keys are kept verbatim (no auto-aliasing).
    """

    # ``populate_by_name`` lets callers construct with either field name
    # or alias; the dict-style names are the canonical source.
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    type: str
    description: str | None = None
    # Default is typed (Number/Bool/List/Dict/None); ``Any`` is
    # deliberately broad because HCL covers a whole literal family.
    default: Any | None = None
    required: bool
    source: str
    osType: str | None = None
    osMode: str | None = None
    osMulti: bool | None = None
    osScope: str | None = None
    varScope: str | None = None
    fileExtensions: list[str] | None = None
    markerError: _MarkerErrorPayload | None = None
    # ``template_key`` is null for ``source = terraform`` variables and
    # carries the per-template key (``default`` for the legacy layout,
    # or the subdirectory name like ``webserver``/``database`` in
    # multi-image apps) for ``source = packer`` variables. Lets the
    # wizard group Packer variables per image and avoid name collisions
    # across templates.
    template_key: str | None = None


# ----------------------------------------------------------------
# OPENSTACK MARKER PARSING (HCL VARIABLES)
# ----------------------------------------------------------------
# Apps declare value-help for OpenStack resources exclusively via an
# explicit marker in the variable's ``description``. No heuristics, no
# name inference. A variable without a marker renders as free text.
#
# Grammar (positional, with defaults):
#
#     @openstack:<type>[:<mode>][:<multi>][:<var_scope>]
#
#   <type>   — one of the resource kinds in ``_OS_TYPES``, OR EMPTY. An
#              empty type slot is allowed when the marker only sets a
#              ``var_scope`` (e.g. ``@openstack:::user`` scopes an
#              otherwise free string variable per-user).
#   <mode>   — 'id' | 'name' (default 'name'; see ``_NAME_ONLY_TYPES``).
#   <multi>  — 'multi' | 'list' | 'single' ('list' is a synonym for
#              'multi'). Default derives from the HCL type:
#              ``list``/``set``/``tuple`` → multi, else single.
#              ``map(...)``/``object(...)`` count as single.
#   <var_scope> — 'all' | 'team' | 'user' (default 'all'). Controls
#              whether the wizard renders one input (``all``), one per
#              team, or one per user. ``team``/``user`` require a
#              ``map(...)`` HCL type. Packer variables allow only ``all``.
#
# Examples:
#     @openstack:network                    → network, name-mode, multi from HCL
#     @openstack:network:id                 → network, id-mode
#     @openstack:security_group:name:multi  → SG, name-mode, multi
#     @openstack:flavor::multi              → empty mode slot ⇒ default 'name'
#     @openstack:flavor:id:single:team      → one flavor ID per team; map(string)
#     @openstack:::user                     → free-text, per-user scoped; map(...)
#
# The marker may appear anywhere in the description but must terminate at
# a word boundary. With multiple markers, the first with a KNOWN type
# wins; markers with unknown types are skipped. If no marker has a known
# type, that's an error (reported with a "did you mean …?" hint).
#
# Error handling: malformed or invalid markers raise ``MarkerError``,
# caught per-variable and attached to the payload as ``markerError`` so
# that variable renders as free text with an inline hint while the rest
# stay usable.
# ----------------------------------------------------------------

# Supported OpenStack resource types. Must stay consistent with:
#  - backend/app/routers/openstack_resources.py (list endpoints)
#  - frontend/src/types/index.ts (`AppVariableOsType`)
#  - frontend/src/components/OpenStackResourcePicker.vue (render)
_OS_TYPES: set[str] = {
    "network",
    "subnet",
    "flavor",
    "image",
    "keypair",
    "security_group",
    "floating_ip_pool",
    "volume",
    "router",
    "availability_zone",
    # ``file`` is a special pseudo-resource: it doesn't pick from a
    # remote OpenStack API, it tells the wizard to render a file-upload
    # widget and route the bytes into ``userInputVar.terraform`` so the
    # template can drop them onto the VM via cloud-init ``write_files``.
    # The mode slot carries the scope (``all``/``team``/``user``); the
    # multi slot carries the mandatory extension filter (e.g. ``pdf`` or
    # ``pdf|docx``) — a file marker without a filter is rejected.
    "file",
}

# Allowed scope tokens for ``@openstack:file:<scope>``. Reuses the
# mode slot of the marker grammar — keeps the regex shape unchanged
# while teaching the parser to interpret the slot per-type.
_FILE_SCOPES: set[str] = {"all", "team", "user"}

# Mandatory extension filter in the fourth marker slot for file
# variables. Only letters/digits and ``|`` as separator; matched
# case-insensitively, lowercased internally. Examples: ``pdf``,
# ``pdf|docx|txt``. Empty is not allowed.
_FILE_EXTENSIONS_RE = re.compile(r"^[a-z0-9]+(?:\|[a-z0-9]+)*$")

# Allowed values for the general ``var_scope`` slot (fourth slot on
# non-file markers). ``all`` is the default — variables without a marker
# and markers without a 4th slot resolve to ``all``.
_VAR_SCOPES: set[str] = {"all", "team", "user"}

# Resource kinds that effectively have no UUID in OpenStack or are
# addressed by name throughout — e.g. keypairs (Nova uses names only),
# availability zones (no UUID at all), floating-IP pools (external
# networks, referenced by name in modules).
#
# The name-only default applies ONLY when the author omits the mode.
# ``@openstack:keypair`` → mode='name'. ``@openstack:keypair:id`` is
# respected but practically pointless (yields an empty ID list).
_NAME_ONLY_TYPES: set[str] = {"keypair", "availability_zone", "floating_ip_pool"}

# Marker regex. Matches the whole token at word boundaries so prose
# examples like ``"see @openstack:network in the docs"`` are recognised
# but ``"@openstackbar"`` is not. Slot content may not contain
# whitespace. Five or more colons = malformed (see
# ``_TOO_MANY_SEGMENTS_RE``).
#
# Right boundary: any non-identifier char — whitespace, line end, common
# punctuation ``. , ; : ! ? ) ] " '``. Left boundary: start or the same.
#
# Slot content:
#  * Slot 1 (type): ``[A-Za-z][A-Za-z0-9_]*`` or EMPTY. An empty type
#    slot means "only set var_scope, don't force a resource picker".
#  * Slots 2/3: ``[A-Za-z]*``.
#  * Slot 4 (var_scope for non-file, file-extensions filter for file):
#    ``[A-Za-z0-9|]*``. The ``|`` is only needed for the file-filter
#    case (e.g. ``pdf|docx``); the parser splits the semantics.
_MARKER_RE = re.compile(
    r"""
    (?:^|(?<=[\s.,;:!?()\[\]"']))   # Left boundary: start or whitespace/punctuation
    @openstack
    :([A-Za-z][A-Za-z0-9_]*)?       # 1: type (may be empty → scope-only marker)
    (?::([A-Za-z]*))?               # 2: mode slot (may be empty)
    (?::([A-Za-z]*))?               # 3: multi slot (may be empty)
    (?::([A-Za-z0-9|]*))?           # 4: var_scope / file-extensions (may be empty)
    (?=$|[\s.,;:!?)\]"'])           # Right boundary
    """,
    # Marker prefix is accepted case-insensitively (``@OpenStack:flavor``
    # == ``@openstack:flavor``); ``_parse_marker`` lowercases slot content.
    re.VERBOSE | re.IGNORECASE,
)

# Detects a ``<whitespace>:<token>`` continuation right after a match
# (e.g. ``@openstack:flavor :id``). ``_MARKER_RE`` stops at the
# whitespace, so we raise an explicit error to surface the typo.
_MARKER_WHITESPACE_CONT_RE = re.compile(r"\s+:\s*[A-Za-z]")

# A comma as a slot separator is a common typo — see the call site.
# Match form: ``<tail starts with>,<token-char>``.
_MARKER_COMMA_CONT_RE = re.compile(r",\s*[A-Za-z0-9|]")

# Quick check: does the marker have too many segments?
# ``@openstack:network:id:multi:team:extra`` → fail. Every 5+ segment
# must be non-empty, otherwise a trailing-colon 4-slot form would be
# wrongly caught.
_TOO_MANY_SEGMENTS_RE = re.compile(
    r"@openstack(?::[A-Za-z0-9_|]+){5,}",
    re.IGNORECASE,
)


# Detects a marker-attempted-but-malformed input: fires when the
# description contains ``@openstack:`` but the strict regex matches
# nothing (dash/slash/equals separators, whitespace, empty type, etc.).
# Matches ``@openstack`` followed by ``:`` or whitespace+``:``.
_BAD_PREFIX_RE = re.compile(
    r"@openstack\s*:",
    re.IGNORECASE,
)


class MarkerError(ValueError):
    """Raised when an ``@openstack`` marker is syntactically or
    semantically invalid. Translated to HTTP 400 in the endpoint so the
    app author sees the error on the first ``GET /apps/{id}/variables``
    instead of the variable silently rendering as free text.

    ``code`` is a stable machine-readable key (e.g. ``MARKER_WHITESPACE``);
    ``message`` is human-readable German for now.
    """

    def __init__(self, var_name: str, message: str, code: str = "MARKER_INVALID"):
        super().__init__(f"Variable '{var_name}': {message}")
        self.var_name = var_name
        self.message = message
        self.code = code


def _parse_var_scope(var_name: str, slot: str | None) -> str | None:
    """Validate and normalize the ``var_scope`` slot of a marker.

    Returns ``None`` for an empty slot; raises ``MarkerError`` for an
    unknown token (with a closest-match hint when one exists).
    """
    if slot is None or slot == "":
        return None
    rs = slot.lower()
    if rs in _VAR_SCOPES:
        return rs
    suggestion = _closest_match(rs, _VAR_SCOPES)
    hint = f"; meintest du '{suggestion}'?" if suggestion else ""
    raise MarkerError(
        var_name,
        f"ungültiger var_scope '{slot}'{hint} — erwartet "
        f"{sorted(_VAR_SCOPES)}",
        code="MARKER_INVALID_VAR_SCOPE",
    )


def _forbid_packer_team_user_scope(var_name: str, source: str, var_scope: str | None) -> None:
    """Reject ``team``/``user`` scopes on packer variables.

    Packer builds ONE image shared by all later VMs/teams/users, so a
    per-team/per-user value would have no effect. Called from both the
    scope-only and resource marker paths.
    """
    if source == "packer" and var_scope in ("team", "user"):
        raise MarkerError(
            var_name,
            f"packer-Variablen unterstützen nur ``var_scope = all``; "
            f"angegeben: '{var_scope}'. Begründung: Packer baut EIN "
            f"Image, das von allen späteren VMs/Teams/Usern geteilt "
            f"wird — ein Per-Team-Wert hätte keine Wirkung.",
            code="MARKER_PACKER_SCOPE_FORBIDDEN",
        )


def _reject_malformed_markers(var_name: str, description: str) -> None:
    """Raise ``MarkerError`` for the malformed-marker shapes a plain
    regex match would silently swallow.

    Covers: too many segments, whitespace between segments, and a comma
    used as a slot separator (the only valid separator is ``|``).
    """
    # Six+ segments (i.e. five+ colons after ``@openstack:``) are never
    # legitimate. Check this first, BEFORE the main regex (which stops
    # after four slots) even notices.
    if _TOO_MANY_SEGMENTS_RE.search(description):
        raise MarkerError(
            var_name,
            "marker hat zu viele Segmente — erlaubt: "
            "@openstack:<type>[:<mode>][:<multi>][:<var_scope>]",
            code="MARKER_TOO_MANY_SEGMENTS",
        )

    matches = list(_MARKER_RE.finditer(description))
    if not matches:
        # Strict hard-fail path: someone typed ``@openstack:`` but our
        # grammar doesn't match — e.g. whitespace, a dash, ``=``, or a
        # slash. Fail loudly rather than render the variable as free-text.
        if _BAD_PREFIX_RE.search(description):
            raise MarkerError(
                var_name,
                "marker konnte nicht geparst werden — erlaubt ist nur "
                "``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` mit "
                "Doppelpunkten als Trenner und ohne Whitespace zwischen "
                "den Segmenten",
                code="MARKER_UNPARSEABLE",
            )
        return

    # Whitespace between marker segments silently truncates:
    # ``_MARKER_RE`` stops at the first whitespace, so
    # ``@openstack:flavor :id`` parses only as ``@openstack:flavor``.
    # For each match, check for a ``<whitespace>:<token>`` continuation
    # and raise a clear error instead of swallowing the typo.
    for m in matches:
        tail = description[m.end():]
        if _MARKER_WHITESPACE_CONT_RE.match(tail):
            raise MarkerError(
                var_name,
                "marker enthält Whitespace zwischen den Segmenten — "
                "schreibe ihn ohne Leerzeichen (z.B. "
                "``@openstack:flavor:id:multi`` statt "
                "``@openstack:flavor :id :multi``)",
                code="MARKER_WHITESPACE",
            )

    # A comma as a slot separator is a common typo — the only allowed
    # separator is ``|`` (e.g. ``@openstack:file:all:pdf|docx``).
    # ``_MARKER_RE`` matches only up to the comma, so we detect
    # ``<match>,<token>`` explicitly and raise a clear error.
    for m in matches:
        tail = description[m.end():]
        if _MARKER_COMMA_CONT_RE.match(tail):
            raise MarkerError(
                var_name,
                "ungültiger Endungsfilter mit Komma — marker-Slots werden "
                "mit ``|`` getrennt, nicht mit Komma (z.B. "
                "``@openstack:file:all:pdf|docx`` statt "
                "``@openstack:file:all:pdf,docx``)",
                code="MARKER_FILE_INVALID_EXTENSIONS",
            )


def _select_marker(var_name: str, description: str):
    """Pick the effective marker match from the description.

    The first marker with a KNOWN type OR an empty type slot (= a
    scope-only marker) wins; markers with an unknown, non-empty type are
    skipped (tolerated). Returns the chosen
    ``(match, raw_type, raw_mode, raw_multi, raw_scope)`` tuple, or
    ``None`` when the description carries no marker at all. Raises
    ``MarkerError`` when every marker had an unknown type.
    """
    matches = list(_MARKER_RE.finditer(description))
    if not matches:
        return None

    first_unknown: tuple[str, str] | None = None  # (raw_type, suggestion)
    for m in matches:
        raw_type = (m.group(1) or "")
        os_type_candidate = raw_type.lower()
        if raw_type == "" or os_type_candidate in _OS_TYPES:
            return (m, raw_type, m.group(2), m.group(3), m.group(4))
        if first_unknown is None:
            first_unknown = (raw_type, _closest_match(os_type_candidate, _OS_TYPES) or "")

    # There were markers, but all with unknown types. Hard-fail with a
    # hint pointing at the first — that is very likely the author's typo.
    raw_type, suggestion = first_unknown  # type: ignore[misc]
    hint = f"; meintest du '{suggestion}'?" if suggestion else ""
    raise MarkerError(
        var_name,
        f"unbekannter resource-type '{raw_type}'{hint} — "
        f"erwartet: {sorted(_OS_TYPES)}",
        code="MARKER_UNKNOWN_OS_TYPE",
    )


def _parse_scope_only_marker(
    var_name: str, source: str, raw_mode: str | None, raw_multi: str | None, raw_scope: str | None
):
    """Parse a scope-only marker (empty type slot, e.g. ``@openstack:::team``).

    Such a marker has no type/mode/multi — only scope meaning. When the
    author uses a short form (``@openstack::team`` with two slots instead
    of four), ``team`` lands in the mode slot rather than the fourth. We
    take the first non-empty slot of mode/multi/scope and accept it as
    long as it is a var_scope token — this makes marker spelling robust
    against the number of colons. Several occupied slots at once remain
    an error (ambiguous).
    """
    candidates = [s for s in (raw_mode, raw_multi, raw_scope) if s not in (None, "")]
    if len(candidates) > 1:
        raise MarkerError(
            var_name,
            "leerer type-slot ist nur in Kombination mit ``var_scope`` "
            "erlaubt (z.B. ``@openstack:::team``); mehrere belegte "
            "Slots sind hier nicht zulässig",
            code="MARKER_EMPTY_TYPE_AMBIGUOUS",
        )
    var_scope = _parse_var_scope(var_name, candidates[0] if candidates else None)
    if var_scope is None:
        raise MarkerError(
            var_name,
            "leerer Marker — entweder einen resource-type angeben "
            "(z.B. ``@openstack:flavor``) oder einen var_scope "
            "(z.B. ``@openstack:::team``)",
            code="MARKER_EMPTY",
        )
    _forbid_packer_team_user_scope(var_name, source, var_scope)
    return (None, None, None, None, var_scope, None)


def _parse_file_marker(
    var_name: str, source: str, raw_mode: str | None, raw_multi: str | None, raw_scope: str | None
):
    """Parse a ``@openstack:file`` marker.

    File markers have their own slot semantics: the mode slot carries the
    scope (``all``/``team``/``user``) and the multi slot carries the
    MANDATORY extension filter (``pdf`` or ``pdf|docx``). Handled
    separately so the generic mode/multi logic stays untouched.
    """
    if source == "packer":
        # Packer builds an image — file variables would never reach the
        # build (the files path today merges hard-coded into
        # ``userInputVar.terraform``). Rather than a silent trap: a
        # marker error.
        raise MarkerError(
            var_name,
            "``@openstack:file`` ist in Packer-Variablen nicht "
            "unterstützt — Dateien werden ausschließlich im "
            "Terraform-Pfad zugestellt",
            code="MARKER_FILE_PACKER_FORBIDDEN",
        )

    file_scope: str | None = None
    if raw_mode is not None and raw_mode != "":
        rs = raw_mode.lower()
        if rs in _FILE_SCOPES:
            file_scope = rs
        else:
            scope_suggestion = _closest_match(rs, _FILE_SCOPES)
            hint = f"; meintest du '{scope_suggestion}'?" if scope_suggestion else ""
            raise MarkerError(
                var_name,
                f"ungültiger file-scope '{raw_mode}'{hint} — erwartet "
                f"{sorted(_FILE_SCOPES)}",
                code="MARKER_INVALID_FILE_SCOPE",
            )

    # The multi slot is now the mandatory extensions filter. An empty
    # slot is an error — file variables need an explicit allow-list so
    # the wizard can filter in the ``accept`` attribute and the backend
    # upload has a clear validation path.
    #
    # Regex detail: for values with ``|`` (e.g. ``pdf|docx``) the content
    # lands in the fourth slot instead of the third, because the third
    # slot does not accept a pipe. We accept that transparently — both
    # positions are checked for the extensions content.
    exts_slot: str | None = None
    if raw_multi not in (None, ""):
        exts_slot = raw_multi
        if raw_scope not in (None, ""):
            raise MarkerError(
                var_name,
                f"@openstack:file akzeptiert keinen fünften Slot "
                f"(angegeben: '{raw_scope}') — der Scope steht im "
                f"dritten Slot (z.B. ``@openstack:file:user:pdf``)",
                code="MARKER_FILE_EXTRA_SLOT",
            )
    elif raw_scope not in (None, ""):
        exts_slot = raw_scope
    if exts_slot is None:
        raise MarkerError(
            var_name,
            "``@openstack:file`` braucht einen Endungsfilter im "
            "vierten Slot, z.B. ``@openstack:file:all:pdf`` oder "
            "``@openstack:file:user:pdf|docx``",
            code="MARKER_FILE_MISSING_EXTENSIONS",
        )
    exts_raw = exts_slot.lower()
    if not _FILE_EXTENSIONS_RE.match(exts_raw):
        raise MarkerError(
            var_name,
            f"ungültiger Endungsfilter '{exts_slot}' — erlaubt sind "
            f"alphanumerische Endungen, mehrere getrennt mit '|' "
            f"(z.B. ``pdf|docx``)",
            code="MARKER_FILE_INVALID_EXTENSIONS",
        )
    file_exts = exts_raw.split("|")

    return ("file", None, None, file_scope, file_scope, file_exts)


def _parse_marker_mode(var_name: str, os_type: str, raw_mode: str | None) -> str | None:
    """Parse the mode slot (``id``/``name``) of a resource marker.

    Empty slot → ``None`` (defaults applied by the caller). Raises with a
    targeted hint when the author placed a multi-flag or a var_scope into
    the mode slot, or used an unknown token.
    """
    if raw_mode is None:
        return None
    rm = raw_mode.lower()
    if rm == "":
        # An empty slot is allowed: ``@openstack:flavor::multi`` means
        # "mode = default, multi = multi". We leave ``mode = None``; the
        # defaults are applied by the caller.
        return None
    if rm in ("id", "name"):
        return rm
    if rm in ("multi", "list", "single"):
        # Common author mistake: the user wanted to set ``:multi`` but
        # didn't leave the mode slot empty. Instead of a generic "invalid
        # mode" message, show the correct marker.
        raise MarkerError(
            var_name,
            f"'{raw_mode}' ist ein multi-Flag, nicht ein Mode — "
            f"schreibe den Marker mit leerem Mode-Slot, z.B. "
            f"``@openstack:{os_type}::{rm}``",
            code="MARKER_MULTI_IN_MODE_SLOT",
        )
    if rm in _VAR_SCOPES:
        # var-scope-in-mode-slot: same logic as multi-in-mode-slot. The
        # app author wanted to set the ``var_scope`` but didn't leave the
        # middle slots empty (``@openstack:flavor:team`` instead of
        # ``@openstack:flavor:::team``). Instead of a cryptic "invalid
        # mode" message, show the correct marker.
        raise MarkerError(
            var_name,
            f"'{raw_mode}' ist ein var_scope, nicht ein Mode — "
            f"schreibe den Marker mit leerem Mode-/Multi-Slot, z.B. "
            f"``@openstack:{os_type}:::{rm}``",
            code="MARKER_SCOPE_IN_MODE_SLOT",
        )
    mode_suggestion = _closest_match(rm, {"id", "name"})
    hint = f"; meintest du '{mode_suggestion}'?" if mode_suggestion else ""
    raise MarkerError(
        var_name,
        f"ungültiger mode '{raw_mode}'{hint} — erwartet 'id' oder 'name'",
        code="MARKER_INVALID_MODE",
    )


def _parse_marker_multi(var_name: str, raw_multi: str | None) -> bool | None:
    """Parse the multi slot (``multi``/``list``/``single``) of a marker.

    ``list`` is a synonym for ``multi``. Empty slot → ``None``.
    """
    if raw_multi is None:
        return None
    mm = raw_multi.lower()
    if mm == "":
        return None
    if mm in ("multi", "list"):
        return True
    if mm == "single":
        return False
    multi_suggestion = _closest_match(mm, {"multi", "list", "single"})
    hint = f"; meintest du '{multi_suggestion}'?" if multi_suggestion else ""
    raise MarkerError(
        var_name,
        f"ungültiger multi-Flag '{raw_multi}'{hint} — erwartet "
        "'multi', 'list' oder 'single'",
        code="MARKER_INVALID_MULTI",
    )


def _collection_check_type(type_lower: str, var_scope: str | None) -> str:
    """Return the HCL type to run the collection check against.

    For scope team/user the wizard contract requires a ``map(...)`` HCL
    type. A naive ``is_collection`` check would fail (``map(list(string))``
    starts with ``map(``) even though the inner element type is a real
    collection. For scoped markers we unwrap the outer ``map(...)`` and
    check the INNER type against the multi expectation.
    """
    if var_scope not in ("team", "user") or not type_lower.startswith("map("):
        return type_lower
    # Bracket-balance the inner part out of ``map(...)``. Naive
    # ``[4:-1]`` slicing isn't enough because nested ``map(map(...))`` is
    # legitimate — we walk the characters once and count parentheses.
    depth = 0
    start = type_lower.find("(")
    inner_end = -1
    for i in range(start, len(type_lower)):
        ch = type_lower[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner_end = i
                break
    if inner_end > start + 1:
        return type_lower[start + 1:inner_end].strip()
    return type_lower


def _check_multi_type_conflict(
    var_name: str, var_type: str, type_for_collection_check: str, multi: bool | None
) -> None:
    """Cross-check the marker's ``:multi``/``:single`` against the HCL type.

    ``list``/``set``/``tuple`` are the collection-capable picker types;
    for these ``:multi`` is natural. ``map``/``object`` are technical
    collections the picker can't drive, so they're treated like
    single-strings for conflict detection.
    """
    is_collection_type = (
        type_for_collection_check.startswith(("list(", "set(", "tuple("))
        or type_for_collection_check in ("list", "set")
    )
    if multi is True and not is_collection_type and type_for_collection_check not in ("", "string"):
        # ``string`` is let through because many apps declare
        # ``type = string`` without a multi-marker and the frontend then
        # delivers CSV anyway. But e.g. ``type = number`` or
        # ``type = map(...)`` with ``:multi`` is clearly contradictory.
        raise MarkerError(
            var_name,
            f"marker deklariert ':multi', aber HCL-Type ist '{var_type}' "
            "— erlaubt sind nur ``string``, ``list(...)``, ``set(...)`` "
            "und ``tuple(...)``",
            code="MARKER_MULTI_TYPE_CONFLICT",
        )
    if multi is False and is_collection_type:
        raise MarkerError(
            var_name,
            f"marker deklariert ':single', aber HCL-Type ist '{var_type}' "
            "(eine list/set/tuple-Kollektion) — fixe einen der beiden",
            code="MARKER_SINGLE_TYPE_CONFLICT",
        )


def _parse_resource_marker(
    var_name: str,
    var_type: str,
    source: str,
    os_type: str,
    raw_mode: str | None,
    raw_multi: str | None,
    raw_scope: str | None,
):
    """Parse a generic (non-file) resource marker: mode, multi, scope."""
    mode = _parse_marker_mode(var_name, os_type, raw_mode)
    multi = _parse_marker_multi(var_name, raw_multi)

    type_lower = (var_type or "").strip().lower()
    # Parse the scope once; reused for both the inner-type collection
    # lookup and the returned value so the same error can't fire twice.
    var_scope = _parse_var_scope(var_name, raw_scope)
    type_for_collection_check = _collection_check_type(type_lower, var_scope)
    _check_multi_type_conflict(var_name, var_type, type_for_collection_check, multi)

    _forbid_packer_team_user_scope(var_name, source, var_scope)
    return (os_type, mode, multi, None, var_scope, None)


def _parse_marker(
    var_name: str, var_type: str, description: str, source: str = "terraform"
) -> tuple[str | None, str | None, bool | None, str | None, str | None, list[str] | None]:
    """
    Parse the ``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` marker
    from the description. Returns ``(None, None, None, None, None, None)``
    when NO marker is present (not an error — the variable renders as free
    text).

    Multi-marker behavior: if several markers are found, the first with a
    known type OR an empty type slot (a pure var_scope marker) is used.
    This is intentionally tolerant. Mode/multi validation errors of the
    chosen marker remain hard failures.

    Raises ``MarkerError`` on:
      - a malformed marker (too many segments, internal whitespace,
        unknown mode/multi/scope tokens, wrong slot separators)
      - a marker contradicting the HCL type (``:single`` with
        ``type = list(...)`` or ``:multi`` with ``type = number``;
        ``:team``/``:user`` with ``type = string``)
      - file-specific: invalid scope, missing extension filter, or an
        invalid filter.
      - packer source with ``var_scope in {team, user}``.

    Returns: ``(os_type, mode, multi, file_scope, var_scope, file_exts)``.

    * ``os_type``     — None when the marker had an empty type (pure
                        var_scope marker).
    * ``mode``        — set for non-file only.
    * ``multi``       — set for non-file only.
    * ``file_scope``  — set for file only (``all``/``team``/``user``).
    * ``var_scope``   — generic scope (``all``/``team``/``user``); for file
                        variables it mirrors ``file_scope`` so the wizard
                        has one source for slot resolution.
    * ``file_exts``   — set for file only: list of allowed extensions
                        (e.g. ``["pdf", "docx"]``), order stable.

    The heavy lifting is delegated to focused helpers: this function only
    orchestrates the pipeline (reject malformed → select marker →
    dispatch to the scope-only / file / generic resource parser).
    """
    if not description:
        return (None, None, None, None, None, None)

    _reject_malformed_markers(var_name, description)

    chosen = _select_marker(var_name, description)
    if chosen is None:
        return (None, None, None, None, None, None)

    _, raw_type, raw_mode, raw_multi, raw_scope = chosen
    os_type: str | None = raw_type.lower() if raw_type else None

    if os_type is None:
        return _parse_scope_only_marker(var_name, source, raw_mode, raw_multi, raw_scope)

    if os_type == "file":
        return _parse_file_marker(var_name, source, raw_mode, raw_multi, raw_scope)

    return _parse_resource_marker(
        var_name, var_type, source, os_type, raw_mode, raw_multi, raw_scope
    )



def _apply_defaults(
    os_type: str, mode: str | None, multi: bool | None, var_type: str
) -> tuple[str, bool]:
    """
    Apply the documented defaults when the marker leaves slots empty:

    - ``mode``: 'name'. For ``_NAME_ONLY_TYPES`` (keypair, availability
      zone, floating-IP pool) 'name' is effectively the only useful
      choice; ``:id`` is respected but yields little.
    - ``multi``: derived from the HCL type — ``list``/``set``/``tuple``
      → True, else False.
    """
    if mode is None:
        mode = "name"

    if multi is None:
        type_lower = (var_type or "").strip().lower()
        # ``map(...)``/``object({...})`` are technically collections but
        # the picker can't drive them, so we treat them as "single" and
        # leave it to the author to request ``:multi`` explicitly.
        # ``list``/``set``/``tuple`` are auto-detected as multi.
        multi = (
            type_lower.startswith(("list(", "set(", "tuple("))
            or type_lower in ("list", "set")
        )

    return (mode, multi)


def _closest_match(s: str, candidates: set[str]) -> str | None:
    """
    Simple Levenshtein-1 heuristic for "did you mean …?" hints.
    ``difflib`` is imported lazily since this is the only place it's used.
    """
    if not s:
        return None
    matches = difflib.get_close_matches(s, candidates, n=1, cutoff=0.7)
    return matches[0] if matches else None


def _line_number_at(content: str, char_index: int) -> int:
    """1-based line index for a char position. Used to point
    MarkerError messages at the line in ``variables.tf`` instead of only
    naming the variable."""
    return content.count("\n", 0, char_index) + 1


def _validate_file_var_shape(var_name: str, var_type: str, scope: str) -> None:
    """Verify a ``@openstack:file:<scope>``-marked variable has the
    HCL type the wizard contract expects.

    The contract — documented in the deploy/file-uploads design — is:

    * ``scope = all``  → ``map(object({...}))``
    * ``scope = team`` → ``map(map(object({...})))``
    * ``scope = user`` → ``map(map(object({...})))``

    The outer map keys content by upload-key (today always
    ``"uploaded"``, reserved for future multi-file-per-slot). For
    ``team``/``user`` the next layer keys by team name resp.
    ``Team-User``-pair so the worker can route per-recipient bytes.

    We don't try to parse HCL — we just check the prefix shape with
    cheap string ops. False positives are unlikely (no real-world HCL
    type accidentally starts with ``map(map(`` unless it is one) and
    a strict full parse would be a big dependency for one check.
    """
    type_normalised = (var_type or "").strip().lower().replace(" ", "")
    if scope == "all" and not type_normalised.startswith("map(object("):
        raise MarkerError(
            var_name,
            f"marker ``@openstack:file:all`` erwartet HCL-Type "
            f"``map(object({{name=string, content_b64=string, "
            f"size=number, content_type=string}}))`` — angegeben: '{var_type}'",
            code="MARKER_FILE_TYPE_SHAPE",
        )
    if scope in ("team", "user") and not type_normalised.startswith("map(map(object("):
        raise MarkerError(
            var_name,
            f"marker ``@openstack:file:{scope}`` erwartet HCL-Type "
            f"``map(map(object({{name=string, content_b64=string, "
            f"size=number, content_type=string}})))`` — angegeben: '{var_type}'",
            code="MARKER_FILE_TYPE_SHAPE",
        )


def _validate_scoped_var_shape(var_name: str, var_type: str, scope: str) -> None:
    """Verify a non-file variable marked with ``var_scope = team|user``
    has a map-typed HCL declaration.

    Reasoning: bei ``team``/``user``-Scope schickt der Wizard eine Map
    (slot_key → value) an Terraform/Packer. Wenn der HCL-Type ein
    Skalar ist (``string``, ``number``, ...), würde Terraform die Map
    beim Apply ablehnen. Wir fangen das hier ab, damit der App-Autor
    den Fehler bei ``GET /apps/{id}/variables`` sieht und nicht erst
    beim ersten Deploy.

    Bei ``scope = all`` (oder fehlendem Scope) gilt das nicht — dann
    rendert der Wizard genau EIN Eingabefeld, das wie heute direkt
    als Skalar oder Liste an Terraform durchgereicht wird.
    """
    if scope not in ("team", "user"):
        return
    type_normalised = (var_type or "").strip().lower().replace(" ", "")
    if not type_normalised.startswith("map(") and type_normalised not in ("map",):
        raise MarkerError(
            var_name,
            f"marker deklariert ``var_scope = {scope}``, aber HCL-Type "
            f"ist '{var_type}'. Pro Scope-Eintrag liefert der Wizard "
            f"eine Map (slot_key → value), also muss der HCL-Type "
            f"``map(...)`` sein — z.B. ``map(string)`` oder "
            f"``map(list(string))``.",
            code="MARKER_SCOPED_REQUIRES_MAP",
        )


def _coerce_hcl_default(raw_default: str, var_type: str) -> tuple[Any, bool]:
    """Coerce an HCL default literal into its Python equivalent so the
    frontend sees ``default = 2`` as ``2`` (number) rather than ``"2"``
    (string). Returns ``(value, required)`` — an HCL ``null`` default
    yields ``None`` AND ``required = True`` (Terraform treats null as "no
    default").

    Robust against minor whitespace and trailing commas; any parse error
    falls back to the raw string.
    """
    if raw_default is None:
        return (None, True)

    stripped = raw_default.strip()
    if stripped == "":
        return (None, True)

    # Literal HCL ``null`` → Variable ist required.
    if stripped.lower() == "null":
        return (None, True)

    type_lower = (var_type or "").strip().lower()

    # Bool first, otherwise ``"true"`` as a string default is caught by
    # the string path.
    if type_lower == "bool":
        if stripped.lower() == "true":
            return (True, False)
        if stripped.lower() == "false":
            return (False, False)

    if type_lower == "number":
        try:
            if "." in stripped or "e" in stripped.lower():
                return (float(stripped), False)
            return (int(stripped), False)
        except ValueError:
            return (stripped, False)

    is_list_like = (
        type_lower.startswith(("list(", "set(", "tuple("))
        or type_lower in ("list", "set")
    )
    is_map_like = type_lower.startswith("map(") or type_lower in ("map", "object")

    if is_list_like or is_map_like or stripped.startswith(("[", "{")):
        # python-hcl2 would be the clean option, but it isn't available
        # in the backend right now and a lazy import would make the
        # import path fragile. Instead we use json.loads — HCL literals
        # for lists/maps with string/number/bool values are a true
        # subset of JSON.
        try:
            return (json.loads(stripped), False)
        except (ValueError, TypeError):
            # Fallback: HCL allows unquoted identifiers as strings
            # (``[NAT]``) and ``true``/``false``/``null`` as values. Try
            # a gentle pre-tokenize step; on further failure the string
            # passes through unchanged.
            try:
                normalised = re.sub(
                    r"\b(true|false|null)\b",
                    lambda m: m.group(0).lower(),
                    stripped,
                    flags=re.IGNORECASE,
                )
                return (json.loads(normalised), False)
            except (ValueError, TypeError):
                return (stripped, False)

    # String (or unknown type): strip outer quotes if the caller hasn't.
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return (stripped[1:-1], False)
    return (stripped, False)


def _parse_one_variable(
    *,
    var_name: str,
    var_block: str,
    var_block_offset: int,
    file_content: str,
    file_label: str,
    source: str,
) -> dict[str, Any]:
    """
    Process a single ``variable "..." { ... }`` block.

    Always returns the variable dict; marker errors are NOT raised but
    attached to the variable in the ``markerError`` field, so the frontend
    can render the variable as free text and show the error inline instead
    of breaking the whole wizard on one bad marker.
    """
    # Extract type
    type_match = re.search(r'type\s*=\s*([^\n]+)', var_block)
    var_type = type_match.group(1).strip() if type_match else "string"

    # Extract description
    desc_match = re.search(r'description\s*=\s*"([^"]*)"', var_block)
    description = desc_match.group(1) if desc_match else ""

    # Extract default value
    default_match = re.search(r'default\s*=\s*([^\n]+)', var_block)
    default_raw = default_match.group(1).strip() if default_match else None

    # Coerce HCL defaults into their Python equivalents (number→int/float,
    # bool→bool, list/map→lists/dicts). ``null`` resets ``required`` to
    # True. On parse error the value falls back to the raw string.
    try:
        default_value, required = _coerce_hcl_default(default_raw, var_type)
    except Exception:
        # Defensive: no HCL edge case should crash the wizard. Worst
        # case, keep the raw string with required=False if a default was
        # present.
        default_value = default_raw
        required = default_raw is None

    var_info: dict[str, Any] = {
        "name": var_name,
        "type": var_type,
        "description": description,
        "default": default_value,
        "required": required,
        "source": source,
    }

    # Evaluate @openstack markers. Per-variable try/except: a typo in ONE
    # variable description must not block the whole wizard; the error
    # travels in the payload alongside the variable.
    try:
        (
            os_type,
            raw_mode,
            raw_multi,
            file_scope,
            var_scope,
            file_exts,
        ) = _parse_marker(var_name, var_type, description, source=source)
        # File variables have a hard contract with cloud-init: the wizard
        # must know whether to render a single slot (scope=all), a map
        # over teams, or a map over users. The HCL type nesting must match
        # the scope or Terraform rejects the decode at apply — we catch it
        # here and give the author a clear error.
        if os_type == "file":
            _validate_file_var_shape(var_name, var_type, file_scope or "all")
        elif var_scope:
            _validate_scoped_var_shape(var_name, var_type, var_scope)
    except MarkerError as exc:
        line = _line_number_at(file_content, var_block_offset)
        var_info["markerError"] = {
            "variable": exc.var_name,
            "message": exc.message,
            "location": f"{file_label}:{line}",
            # ``code`` is the stable key for future i18n / frontend logic.
            "code": exc.code,
        }
        return var_info

    if os_type:
        if os_type == "file":
            # File variables are neither mode- nor multi-driven; the
            # wizard renders a FileDropZone, not the resource picker.
            # ``osMode`` and ``osMulti`` are deliberately left unset so
            # the frontend reads the absence as "not applicable" rather
            # than inventing a default.
            var_info["osType"] = os_type
            var_info["osScope"] = file_scope or "all"
            if file_exts:
                var_info["fileExtensions"] = file_exts
        else:
            mode, multi = _apply_defaults(os_type, raw_mode, raw_multi, var_type)
            var_info["osType"] = os_type
            var_info["osMode"] = mode
            var_info["osMulti"] = multi

    # ``varScope`` is orthogonal to the resource type — even a free-text
    # variable (no ``osType``) can be scoped. For file variables we mirror
    # ``osScope`` into ``varScope`` so the frontend reads one source.
    if var_scope:
        var_info["varScope"] = var_scope
    elif os_type == "file":
        var_info["varScope"] = file_scope or "all"

    return var_info


def _iter_variable_blocks(content: str):
    """Yield ``(var_name, var_block, block_offset)`` for each HCL
    ``variable "name" { ... }`` block, brace-balanced.

    A naive ``variable\\s+"([^"]+)"\\s*\\{([^}]+)\\}`` regex stops the
    block at the FIRST ``}`` and truncates any variable whose type or
    default literal contains braces — e.g. ``type = object({...})``,
    ``map(...)`` or ``default = {}``. Instead we match only the block
    HEAD and then walk the string counting ``{``/``}`` until depth
    returns to zero.

    ``var_block`` is the content BETWEEN the outer braces (exclusive);
    ``block_offset`` is the start index of the whole ``variable``
    declaration (used for line-number hints).
    """
    head_pattern = r'variable\s+"([^"]+)"\s*\{'
    for head in re.finditer(head_pattern, content):
        var_name = head.group(1)
        block_offset = head.start()
        open_brace = head.end() - 1  # index of the ``{`` matched above
        depth = 0
        end_index = -1
        for i in range(open_brace, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = i
                    break
        if end_index == -1:
            # Unbalanced braces — skip this malformed block rather than
            # emitting a truncated one.
            continue
        var_block = content[open_brace + 1:end_index]
        yield var_name, var_block, block_offset


def _parse_terraform_variables(file_path: str) -> list[dict[str, Any]]:
    """Parse Terraform `variables.tf` file. Per-variable marker errors
    travel in the ``markerError`` field (not raised) — see
    ``_parse_one_variable``."""
    with open(file_path) as f:
        content = f.read()

    variables = []
    for var_name, var_block, block_offset in _iter_variable_blocks(content):
        # Filter: drop ``users`` and ``image_name``
        if var_name == "users" or var_name == "image_name":
            continue
        # Multi-image apps declare ``image_name_<key>`` per template and
        # mark those declarations with ``@platform:internal`` in the
        # description. The worker fills these from the discovered Packer
        # templates; the wizard must not surface them as user-editable
        # variables. Same rationale as the ``image_name``/``users``
        # filter above — these are platform-injected, not user input.
        desc_match = re.search(r'description\s*=\s*"([^"]*)"', var_block)
        description = desc_match.group(1) if desc_match else ""
        if "@platform:internal" in description:
            continue
        variables.append(_parse_one_variable(
            var_name=var_name,
            var_block=var_block,
            var_block_offset=block_offset,
            file_content=content,
            file_label="terraform/variables.tf",
            source="terraform",
        ))

    return variables


def _parse_packer_variables(file_path: str, template_key: str = "default") -> list[dict[str, Any]]:
    """Parse Packer `variables.pkr.hcl` file. Per-variable marker errors
    travel in the ``markerError`` field; see ``_parse_one_variable``.

    ``template_key`` is recorded on each variable so the wizard can
    group Packer variables per template (and avoid name collisions
    across templates in multi-image apps). For the single-template
    layout the caller passes ``"default"``.
    """
    with open(file_path) as f:
        content = f.read()

    variables = []
    for var_name, var_block, block_offset in _iter_variable_blocks(content):
        # Filter: image_name rauslassen
        if var_name == "image_name":
            continue
        var_info = _parse_one_variable(
            var_name=var_name,
            var_block=var_block,
            var_block_offset=block_offset,
            file_content=content,
            file_label=f"packer/{template_key}/variables.pkr.hcl"
            if template_key != "default"
            else "packer/variables.pkr.hcl",
            source="packer",
        )
        var_info["template_key"] = template_key
        variables.append(var_info)

    return variables


# ----------------------------------------------------------------
# PACKER TEMPLATE DISCOVERY
# ----------------------------------------------------------------
# Apps may ship Packer templates in one of two layouts:
#
#  1. Legacy single-template layout:
#         packer/template.pkr.hcl
#         packer/variables.pkr.hcl
#     → exactly ONE image, conventionally keyed ``default``. The
#       worker injects ``image_name`` (a single Terraform variable).
#
#  2. Multi-template layout:
#         packer/<key>/template.pkr.hcl
#         packer/<key>/variables.pkr.hcl   (optional)
#     → one image per ``<key>``. The worker injects one
#       ``image_name_<key>`` Terraform variable per template, each
#       marked ``@platform:internal`` in its description so the wizard
#       skips them.
#
# Discovery rules:
#   - No ``packer/`` directory → returns ``[]`` (no Packer phase).
#   - Legacy file present       → returns ``[_PackerTemplate("default", ...)]``.
#   - Subdirectories with a
#     ``template.pkr.hcl``      → returns one entry per subdir, sorted.
#   - Both legacy AND subdirs   → ``PackerTemplateDiscoveryError`` (hard).
#   - Subdir without
#     ``template.pkr.hcl``      → ignored (e.g. ``_common/``, ``scripts/``).
#   - Subdir with a key that
#     doesn't match the pattern → ``PackerTemplateDiscoveryError``.
#
# Key pattern is intentionally narrow (``[a-z][a-z0-9_-]{0,30}``) so
# the key is safe to embed in Terraform variable names and image
# tags without quoting.
# ----------------------------------------------------------------

@dataclass
class _PackerTemplate:
    """One Packer template discovered under ``<repo>/packer``.

    ``variables_path`` may point at a non-existing file — the caller
    must check ``os.path.isfile`` before reading it. We don't filter
    here because the file is optional and a missing one is not an
    error.
    """

    key: str
    template_path: str
    variables_path: str


_TEMPLATE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


class PackerTemplateDiscoveryError(ValueError):
    """Raised when the Packer directory has a layout the platform can't
    reconcile (ambiguous, contradictory, or with an unsafe key).

    Translated to HTTP 422 at the load_variable_definitions boundary
    so the app author sees the error immediately on the first
    ``GET /apps/{id}/variables`` instead of at first deploy.
    """


def _discover_packer_templates(repo_path: str) -> list[_PackerTemplate]:
    """Walk ``<repo_path>/packer`` and return the list of templates the
    worker will build for this app.

    See the section docstring above for the layout rules. Returns
    ``[]`` for apps without any Packer at all (Terraform-only).
    """
    packer_dir = os.path.join(repo_path, "packer")
    if not os.path.isdir(packer_dir):
        return []

    legacy_template = os.path.join(packer_dir, "template.pkr.hcl")
    has_legacy = os.path.isfile(legacy_template)

    multi_templates: list[_PackerTemplate] = []
    bad_keys: list[str] = []
    for entry in sorted(os.listdir(packer_dir)):
        sub = os.path.join(packer_dir, entry)
        if not os.path.isdir(sub):
            continue
        tmpl = os.path.join(sub, "template.pkr.hcl")
        if not os.path.isfile(tmpl):
            # Subdirs without a template (``_common/``, ``scripts/``,
            # ``http/`` for boot-time HTTP servers, ...) are silently
            # ignored — they're tooling, not images to build.
            continue
        if not _TEMPLATE_KEY_RE.match(entry):
            bad_keys.append(entry)
            continue
        multi_templates.append(_PackerTemplate(
            key=entry,
            template_path=tmpl,
            variables_path=os.path.join(sub, "variables.pkr.hcl"),
        ))

    if bad_keys:
        raise PackerTemplateDiscoveryError(
            f"Packer template subdirectories with invalid keys "
            f"(must match [a-z][a-z0-9_-]{{0,30}}): {bad_keys}"
        )

    if has_legacy and multi_templates:
        raise PackerTemplateDiscoveryError(
            "App repository has BOTH packer/template.pkr.hcl (legacy "
            "layout) AND packer/<key>/template.pkr.hcl subdirectories "
            f"({[t.key for t in multi_templates]}). Choose one layout "
            "— remove the legacy file or the subdirectories."
        )

    if has_legacy:
        return [_PackerTemplate(
            key="default",
            template_path=legacy_template,
            variables_path=os.path.join(packer_dir, "variables.pkr.hcl"),
        )]

    return multi_templates


# ----------------------------------------------------------------
# GET ALL APPS
# ----------------------------------------------------------------
@router.get("/", response_model=list[AppResponse])
def list_apps(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List apps visible to the current user.

    Admins see every non-deleted app (full platform view). Everyone else,
    including teachers, sees the student-style filter: own apps + public
    apps with at least one approved version.
    """
    if current_user.role == UserRole.ADMIN:
        apps = crud_apps.get_apps(db, skip=skip, limit=limit)
    else:
        apps = crud_apps.get_visible_apps(db, current_user.userId, skip=skip, limit=limit)
    return [_serialize_app(a) for a in apps]


# ----------------------------------------------------------------
# GET APP BY ID
# ----------------------------------------------------------------
@router.get("/{app_id}", response_model=AppWithVersions)
def get_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get app by ID with available versions.

    Soft-deleted apps are still readable here so existing deployments
    that still reference them can render their app name, git link,
    etc. They just don't show up in the apps list / deploy wizard
    (those use the default-filtered ``get_apps``).
    """
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Only owner OR admin sees private/unapproved apps; everyone else
    # (incl. teachers) needs the app to be public AND have an approved
    # version. Same gate everywhere via :func:`can_view_app`.
    is_owner_or_admin = (
        str(app.userId) == str(current_user.userId)
        or current_user.role == UserRole.ADMIN
    )
    if not is_owner_or_admin and (app.is_private or not crud_approvals.has_any_approved_version(db, app.appId)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this resource"
            )

    # Fetch versions if git_link exists. Skipped for soft-deleted apps
    # — listing versions is a "what could I deploy" affordance and the
    # answer is "nothing", you already deleted this app.
    if app.git_link and app.deleted_at is None:
        try:
            all_versions = git_service.get_versions(app.git_link)
            if is_owner_or_admin:
                # Owner/Admin see all Git tags
                app.versions = all_versions
            else:
                # Everyone else only sees approved version tags
                approved_tags = {
                    a.version_tag
                    for a in crud_approvals.get_approvals_for_app(db, app.appId)
                    if a.status == "approved"
                }
                app.versions = [
                    v for v in all_versions
                    if _version_tag(v) in approved_tags
                ]
        except Exception as e:
            app.versions = []
            logger.warning(f"Could not fetch versions: {str(e)}")
    else:
        app.versions = []

    return _serialize_app(app)


# ----------------------------------------------------------------
# GET APP VARIABLES
# ----------------------------------------------------------------
def load_variable_definitions(app, version: str) -> list[dict[str, Any]]:
    """Clone the app's release-vars and parse all Terraform/Packer
    variables into the same shape ``GET /apps/{id}/variables`` returns.

    Reusable from ``POST /deployments`` so the deployment endpoint can
    enforce per-variable contracts (``varScope``, ``fileExtensions``)
    using the App-Autor's declarations as source-of-truth. Cleans up
    the temporary clone on its own — callers don't manage paths.

    Raises ``HTTPException(400)`` if the app has no Git link and
    bubbles unexpected errors as ``HTTPException(500)``.
    """
    if not app.git_link:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="App has no Git repository configured",
        )

    deployment_id = f"vars_{app.appId}_{version}".replace("/", "_")
    repo_path = None
    try:
        repo_path = git_service.clone_release_vars(app.git_link, version, deployment_id)
        variables: list[dict[str, Any]] = []
        tf_vars_path = os.path.join(repo_path, "terraform", "variables.tf")
        if os.path.exists(tf_vars_path):
            variables.extend(_parse_terraform_variables(tf_vars_path))
        # Discover all Packer templates (legacy single-file layout OR
        # per-key subdirectories) and parse each one's variables. The
        # ``template_key`` is recorded on every Packer variable so the
        # wizard can group inputs per image. Discovery raises if the
        # repo has an ambiguous or unsafe layout — surface that as
        # HTTP 422 so the app author can fix the repo before any
        # deploy attempt.
        try:
            templates = _discover_packer_templates(repo_path)
        except PackerTemplateDiscoveryError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        for tmpl in templates:
            if os.path.isfile(tmpl.variables_path):
                variables.extend(
                    _parse_packer_variables(tmpl.variables_path, template_key=tmpl.key)
                )
        return variables
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to load variable definitions for app %s version %s",
            app.appId, version,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch variables",
        )
    finally:
        if repo_path:
            try:
                git_service.cleanup_repository(repo_path)
            except Exception as cleanup_error:
                logger.error(
                    "Failed to cleanup repository: %s", str(cleanup_error)
                )


@router.get("/{app_id}/variables", response_model=list[AppVariableResponse])
def get_app_variables(
    app_id: UUID,
    version: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get dynamic app variables from app's Git repository
    Parses variables.tf file and returns all configurable variables

    Returns:
    - name: Variable name
    - type: Variable type (string, number, bool, list, map, etc.)
    - description: Variable description
    - default: Default value (if any)
    - required: Whether variable is required
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission. ``ensure_view_app`` enforces the matrix:
    # owner OR admin sees private/unapproved, others need the
    # public+approved combination.
    ensure_view_app(current_user, app, db=db)

    variables = load_variable_definitions(app, version)
    if not variables:
        logger.warning("No variables found for app %s version %s", app_id, version)

    # Marker errors travel per-variable in the ``markerError`` field; the
    # endpoint does not 400 on a single bad marker but leaves the frontend
    # to show it inline, keeping the other variables usable.
    bad = [v for v in variables if v.get("markerError")]
    if bad:
        logger.warning(
            "App %s version %s has %d variable(s) with bad @openstack markers: %s",
            app_id, version, len(bad), [v["name"] for v in bad],
        )

    return variables


# ----------------------------------------------------------------
# CREATE APP
# ----------------------------------------------------------------
@router.post("/", response_model=AppResponse, status_code=status.HTTP_201_CREATED)
def create_app(
    app: AppCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new app
    - **All authenticated users** can create apps
    - **Git repository access is verified** before creating the app
    """
    # Decode the optional image data-URL up front so a malformed
    # payload fails before we hit Keycloak / Git / DB.
    image_bytes, image_mime = parse_image_data_url(app.image)

    # Verify repository access if git_link is provided
    if app.git_link:
        access_result = git_service.verify_repository_access(app.git_link)
        if not access_result['success']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=access_result['message']
            )
        logger.info(f"Repository access verified for {app.git_link}")

    db_app = crud_apps.create_app(db, app, current_user.userId)
    if image_bytes is not None:
        db_app = crud_apps.set_app_image(db, db_app.appId, image_bytes, image_mime)

    # Auto-submit all tags for review if requested (public apps only)
    if app.submit_all_versions and not app.is_private and app.git_link:
        try:
            versions = git_service.get_versions(app.git_link)
            for v in versions:
                tag = _version_tag(v)
                if tag:
                    with contextlib.suppress(Exception):
                        crud_approvals.submit_version(db, app_id=db_app.appId, version_tag=tag)
        except Exception as e:
            logger.warning(f"Could not auto-submit versions for app {db_app.appId}: {e}")

    return _serialize_app(db_app)


# ----------------------------------------------------------------
# UPDATE APP
# ----------------------------------------------------------------
@router.put("/{app_id}", response_model=AppResponse)
def update_app(
    app_id: UUID,
    app_update: AppUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an app.

    ``git_link`` is immutable after creation — sending it in the body
    returns HTTP 400. Use ``is_private`` to toggle visibility.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission — owner-or-admin only.
    ensure_edit_app(current_user, app)

    image_was_provided = "image" in app_update.model_fields_set
    image_bytes, image_mime = (None, None)
    if image_was_provided:
        image_bytes, image_mime = parse_image_data_url(app_update.image)

    updated_app = crud_apps.update_app(db, app_id, app_update)
    if image_was_provided:
        updated_app = crud_apps.set_app_image(db, app_id, image_bytes, image_mime)
    return _serialize_app(updated_app)


# ----------------------------------------------------------------
# SUBMIT VERSION FOR REVIEW
# ----------------------------------------------------------------
@router.post(
    "/{app_id}/versions/{version_tag}/submit",
    response_model=AppVersionApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit a specific version tag for admin review.

    Owner OR admin only. A REJECTED version can be resubmitted; PENDING
    and APPROVED cannot.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_submit_app_version(current_user, app)

    if not app.git_link:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="App has no git repository configured",
        )

    # Marker validation — blocks submit on invalid @openstack markers.
    # Same logic as the approve endpoint; git errors (400/500) are
    # skipped so submit still works when the repo is unreachable.
    try:
        variables = load_variable_definitions(app, version_tag)
        marker_errors = [v.get("markerError") for v in variables if v.get("markerError")]
        if marker_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": (
                        "Version kann nicht eingereicht werden — fehlerhafte "
                        "@openstack-Marker in den Variablen-Dateien"
                    ),
                    "marker_errors": marker_errors,
                },
            )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            raise
        # 400 (no git_link, handled above) or 500 (git unreachable) —
        # allow submit anyway.

    return crud_approvals.submit_version(
        db, app_id=app_id, version_tag=version_tag, diff_url=body.diff_url, notes=body.notes
    )


# ----------------------------------------------------------------
# WITHDRAW VERSION SUBMISSION
# ----------------------------------------------------------------
@router.delete(
    "/{app_id}/versions/{version_tag}/submit",
    status_code=status.HTTP_204_NO_CONTENT,
)
def withdraw_version(
    app_id: UUID,
    version_tag: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Withdraw a PENDING version submission.

    Owner OR admin only. Deletes the approval entry so the version
    appears as unsubmitted again.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_submit_app_version(current_user, app)
    crud_approvals.withdraw(db, app_id=app_id, version_tag=version_tag)
    return None


# ----------------------------------------------------------------
# GET VERSION APPROVALS FOR APP
# ----------------------------------------------------------------
@router.get(
    "/{app_id}/versions",
    response_model=list[AppVersionApprovalResponse],
)
def list_version_approvals(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all version approval entries for an app.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_edit_app(current_user, app)

    return crud_approvals.get_approvals_for_app(db, app_id)


# ----------------------------------------------------------------
# DELETE APP
# ----------------------------------------------------------------
@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Soft-delete an app.

    Sets ``apps.deleted_at`` instead of removing the row, so any
    historical or still-running deployment that points at this app keeps
    resolving. The app stops appearing in listings and the deploy wizard;
    existing deployments live on until destroyed individually.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission — owner-or-admin only.
    ensure_delete_app(current_user, app)

    success = crud_apps.soft_delete_app(db, app_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )
    return None
