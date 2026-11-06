"""Load ``duplex.yaml`` into frozen dataclasses, failing closed on anything unknown or unsafe.

``load_config`` is the only entry point. It rejects unknown keys, unknown provider and router
backend names, references to undefined profiles, literal secrets, and env var names that are unset,
with a ``ConfigError`` that names the offending key. Secrets never live in the file: every secret is
read from the env var a ``*_env`` key names, at load time, into a ``Secrets`` object that hides its
values from ``repr()``.
"""

import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import yaml

from .prompts.base import BACKCHANNELS, Backchannel
from .providers.base import ModelChoice
from .switching import NOT_A_TARGET, spoken_form

PROVIDER_NAMES = frozenset({"gpt-live", "openai-realtime", "xai-realtime", "fake"})
ROUTER_BACKENDS = frozenset({"decisions", "jev", "knn"})
FAST_PATH_BACKENDS = frozenset({"knn"})
TURN_DETECTION_TYPES = frozenset({"server_vad", "semantic_vad"})
DELEGATION_STYLES = frozenset({"live", "tool"})
EFFORTS = ("low", "medium", "high")

ALL_PROFILES = "*"
"""In a person's ``profiles`` list, grants every profile defined in config."""

CONTROL_PROFILE = "_control"
"""The router's reserved choice for status and cancel requests; no profile may use this name."""

_RESERVED_PROFILE_NAMES = {
    CONTROL_PROFILE: "is reserved for the router",
    "ambiguous": "is reserved: the switch-request parser returns it when a name fits two profiles",
}
"""Names no profile may use, each with why."""

VOICE_CONTEXT_MAX_CHARS = 2000
"""Longest ``profiles.<p>.voice_context`` or ``people.<p>.voice_context`` allowed; each goes into the
voice prompt while its profile is active, or while the conversation is with its person."""

type ContextKind = Literal["soul", "user", "briefing"]
"""A Hermes context file: a profile's ``SOUL.md`` (``soul``), its notes on its owner,
``memories/USER.md`` (``user``), or the briefing on the owner's current projects and priorities that
Hermes writes from its memory (``briefing``)."""

HERMES_CONTEXT_FILES: tuple[ContextKind, ...] = ("soul", "user", "briefing")
"""What ``hermes.voice_context_files`` may list."""

OWNER_CONTEXT_FILES: frozenset[ContextKind] = frozenset({"user", "briefing"})
"""The context files about the Hermes owner, which go into the voice prompt only while the owner is
alone in the conversation."""

HERMES_CONTEXT_MAX_CHARS_LIMIT = 8000
"""Largest ``hermes.voice_context_max_chars`` allowed, so every context file with every voice context
still fits GPT-Live's 16,384-token instructions."""

CONFIG_ENV = "HERMES_DUPLEX_CONFIG"
HERMES_HOME_ENV = "HERMES_HOME"
CONFIG_FILE_NAME = "duplex.yaml"

# Keys that would hold a secret literally; config names the env var instead (``<key>_env``).
_SECRET_KEYS = frozenset({"api_key", "token", "bot_token", "key", "web_token", "secret"})
_LITERAL_SECRET = "secrets must not be written in config; name the env var that holds it with a '_env' key"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DISCORD_ID = re.compile(r"^[0-9]+$")

HERMES_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""A Hermes profile id, which names its directory under ``$HERMES_HOME/profiles/``; Hermes matches
names lowercased."""


class ConfigError(Exception):
    """The config is missing, unreadable, or invalid. ``key`` is the dotted path of the offending key.

    Messages never contain secret values.
    """

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key


class MissingEnvError(ConfigError):
    """The config is valid except that env vars it names are unset or empty.

    The message and ``key`` name the first such variable, in file order. ``missing`` lists every one
    as (config key, env var name), in file order; ``config`` is the loaded config, whose ``secrets``
    hold only the variables that are set.
    """

    def __init__(self, missing: Sequence[tuple[str, str]], config: Config) -> None:
        key, env_name = missing[0]
        super().__init__(key, f"environment variable {env_name} is not set or empty")
        self.missing = tuple(missing)
        self.config = config


class Secrets:
    """Secret values read from the env vars that config ``*_env`` keys name.

    Look a value up by its env var name, e.g. ``secrets.get(config.provider.api_key_env)``. The values
    never appear in ``repr()`` or ``str()``.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, env_name: str) -> str:
        """Return the value of ``env_name``; raises ``KeyError`` if config names no such env var."""
        return self._values[env_name]

    def names(self) -> tuple[str, ...]:
        """The env var names it holds values for, in the order config first names them."""
        return tuple(self._values)

    def __repr__(self) -> str:
        return f"Secrets(<{len(self._values)} values hidden>)"


@dataclass(frozen=True)
class HermesConfig:
    """How to reach the Hermes API server."""

    api_base: str
    """Base URL of the API server, without a trailing slash, e.g. ``http://127.0.0.1:8642``."""

    profile_keys_env: Mapping[str, str]
    """Profile name → env var holding that profile's ``API_SERVER_KEY``. A profile missing here
    cannot run tasks ("not configured")."""

    voice_context_files: frozenset[ContextKind] = frozenset()
    """Which of each profile's Hermes files go into the voice prompt, from ``HERMES_CONTEXT_FILES``;
    empty for none."""

    voice_context_max_chars: int = 4000
    """Most characters of ``SOUL.md`` and of ``USER.md`` the voice prompt carries; a longer one is cut
    short. The briefing has its own fixed cap."""

    briefing_refresh_h: float = 6.0
    """Hours after which a briefing is old enough for Hermes to write it again, in the background."""


@dataclass(frozen=True)
class ProviderConfig:
    """The speech-to-speech provider. ``name`` is always explicit, never inferred from other keys."""

    name: str
    """One of ``PROVIDER_NAMES``."""

    model: str | None
    """Provider model id; None means the adapter's default."""

    voice: str | None
    """Voice name; None means the provider's default."""

    api_key_env: str | None
    """Env var holding the provider API key; required for every provider except ``fake``."""

    turn_detection: Mapping[str, Any] | None
    """Realtime-family turn detection settings passed to the provider (``type`` is ``server_vad`` or
    ``semantic_vad``; ``xai-realtime`` allows only ``server_vad``). None means the provider default.
    GPT-Live ignores it."""

    script: Path | None = None
    """``fake`` only: the JSONL script to replay. Relative paths resolve against the config file."""

    record_path: Path | None = None
    """``fake`` only: where to append what the session sent, as JSONL. Relative like ``script``."""

    capabilities: Mapping[str, int | bool | str] | None = None
    """``fake`` only: ``ProviderCapabilities`` keys that override the fake's defaults; None keeps them."""


@dataclass(frozen=True)
class ControlExamples:
    """Extra phrases for the router's ``_control`` choice, added to its built-in examples."""

    status: tuple[str, ...] = ()
    cancel: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionsConfig:
    """How to reach the OpenAI Decisions API for the ``decisions`` router backend."""

    api_key_env: str
    """Env var holding the OpenAI API key."""

    model: str | None = None
    """Decisions model id; None means the backend's default."""

    base_url: str | None = None
    """API base URL without a trailing slash, e.g. ``https://api.openai.com/v1``; None means the
    backend's default."""


@dataclass(frozen=True)
class JevConfig:
    """How to reach a TypeSafe System One API (Jev, or a compatible service) for the ``jev`` backend."""

    api_key_env: str
    """Env var holding the API key."""

    model: str | None = None
    """System One model id or alias; None means the backend's default."""

    base_url: str | None = None
    """API root without ``/v1`` or a trailing slash, e.g. ``https://api.typesafe.ai``; None means the
    backend's default."""


@dataclass(frozen=True)
class RouterConfig:
    """Which backends judge each task's effort and suggest switches, and the composite policy's knobs."""

    backend: str
    """Main-path backend, one of ``ROUTER_BACKENDS``."""

    fast_path: str | None = None
    """Fast-path backend (``knn``) run alongside the main path, or None for no fast path."""

    deadline_ms: int = 300
    """How long the main path may take before the composite falls back, in ms."""

    default_profile: str = "general"
    """The profile people start on when their own ``default_profile`` is unset and they may use it;
    must be defined under ``profiles``."""

    suggest_switch: bool = True
    """Whether the router may suggest switching to another profile the speaker may use."""

    suggest_threshold: float = 0.85
    """Least probability, in (0, 1], another profile needs before the router suggests it."""

    control_examples: ControlExamples = ControlExamples()

    decisions: DecisionsConfig | None = None
    """Settings of the ``decisions`` backend; required when ``backend`` is ``decisions``."""

    jev: JevConfig | None = None
    """Settings of the ``jev`` backend; required when ``backend`` is ``jev``."""


@dataclass(frozen=True)
class ProfileConfig:
    """A Hermes profile the router can send tasks to."""

    description: str = ""
    """What the profile handles, in a phrase; the router matches requests against it."""

    examples: tuple[str, ...] = ()
    """Example requests for the profile."""

    models: Mapping[str, ModelChoice] = field(default_factory=lambda: MappingProxyType({}))
    """Effort (``low``, ``medium``, ``high``) → model for runs at that effort. A missing effort uses
    ``default_model``."""

    default_model: ModelChoice | None = None
    """Model for efforts missing from ``models``; None means the Hermes profile's own default."""

    aliases: tuple[str, ...] = ()
    """Other names people say for the profile, e.g. "the stock picker". Each is compared as it is said
    (``switching.spoken_form``: case, punctuation, hyphens, and a leading "the" ignored); no two
    profiles share one, none is or starts with another profile's name ("general news" while "general"
    is a profile), and none starts with "me", "us", or "you"."""

    voice_context: str | None = None
    """Text, at most ``VOICE_CONTEXT_MAX_CHARS`` long, that the voice prompt carries while this is the
    active profile: what it covers, what to always hand off, how to say its words. None for none."""

    def model_for(self, effort: str) -> ModelChoice | None:
        """The model for a run at ``effort`` (``low``, ``medium``, ``high``) on this profile: its
        ``models`` entry, else ``default_model``; None means the Hermes profile's own default."""
        return self.models.get(effort) or self.default_model


@dataclass(frozen=True)
class PersonConfig:
    """One person who may talk to the service."""

    discord_id: int | None
    """Discord user id, or None if the person does not use Discord."""

    web_token_env: str | None
    """Env var holding the person's web sign-in token, or None if the person does not use the web."""

    profiles: tuple[str, ...]
    """Profiles the person may use, never empty: names defined under ``profiles``, or ``"*"`` for all
    of them."""

    default_profile: str | None = None
    """The profile the person starts on, one they may use; None leaves it to the router's
    ``default_profile`` or their first permitted profile."""

    hermes_owner: bool = False
    """Whether the person is Hermes's owner, whom each profile's ``memories/USER.md`` describes; at
    most one person is."""

    voice_context: str | None = None
    """Text, at most ``VOICE_CONTEXT_MAX_CHARS`` long, that the voice prompt carries about the person
    while the conversation is with them. None for none."""


@dataclass(frozen=True)
class DiscordConfig:
    """The Discord bot transport."""

    enabled: bool = False
    bot_token_env: str | None = None
    """Env var holding the bot token; required when ``enabled``."""


@dataclass(frozen=True)
class WebConfig:
    """The web page and WebSocket transport."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    """TCP port; 0 lets the OS pick a free one."""


@dataclass(frozen=True)
class TransportsConfig:
    """The transports people connect through; each is off unless enabled."""

    discord: DiscordConfig = DiscordConfig()
    web: WebConfig = WebConfig()


@dataclass(frozen=True)
class AudioConfig:
    """Audio pacing, barge-in detection, and playback buffering."""

    pacer_lead_ms: int = 200
    """Most unplayed output audio the pacer lets reach a transport, in ms."""

    gate_threshold_dbfs: float = -35.0
    """Input RMS level, in dBFS, above which the loudness gate treats a speaker as talking."""

    playback_queue_s: float = 5.0
    """Most audio a playback queue holds before it drops the oldest, in seconds."""


@dataclass(frozen=True)
class TasksConfig:
    """How the service treats tasks a previous run of it left open."""

    restore_max_age_h: float = 24.0
    """An open task created more than this many hours ago is not followed again after a restart; it is
    marked unknown without telling anyone."""


@dataclass(frozen=True)
class AssistantConfig:
    """Who the voice assistant is on the call; every provider's prompt is written from it."""

    name: str = "Hermes"
    """The name the assistant goes by; every prompt's identity line says it."""

    persona: str | None = None
    """Text, possibly several lines, that replaces the prompt's own style and tone lines; None keeps
    them. It never replaces the identity line, which always comes from ``name``."""

    backchannel: Backchannel = "moderate"
    """How often to make brief listening sounds while someone speaks: ``none``, ``moderate``, or
    ``frequent``. Only GPT-Live uses it."""


@dataclass(frozen=True)
class Config:
    """The whole validated ``duplex.yaml``, plus the paths and secrets resolved while loading it."""

    path: Path
    """The file this config was loaded from."""

    hermes_home: Path
    """Resolved ``$HERMES_HOME`` (default ``~/.hermes``)."""

    hermes: HermesConfig
    provider: ProviderConfig
    router: RouterConfig
    profiles: Mapping[str, ProfileConfig]
    people: Mapping[str, PersonConfig]
    transports: TransportsConfig
    audio: AudioConfig
    tasks: TasksConfig
    assistant: AssistantConfig
    secrets: Secrets = field(repr=False, compare=False)


def resolve_hermes_home(env: Mapping[str, str] | None = None) -> Path:
    """Return ``$HERMES_HOME`` from ``env`` (default ``os.environ``), or ``~/.hermes`` when unset."""
    env = os.environ if env is None else env
    value = env.get(HERMES_HOME_ENV)
    return Path(value).expanduser() if value else Path.home() / ".hermes"


def resolve_config_path(explicit: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Return the config file path: ``explicit`` (a ``--config`` argument) if given, else
    ``$HERMES_DUPLEX_CONFIG``, else ``$HERMES_HOME/duplex.yaml``. The file need not exist."""
    env = os.environ if env is None else env
    if explicit is not None:
        return Path(explicit).expanduser()
    if value := env.get(CONFIG_ENV):
        return Path(value).expanduser()
    return resolve_hermes_home(env) / CONFIG_FILE_NAME


def load_config(path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate the config at ``path`` (resolved by ``resolve_config_path`` when None).

    ``env`` (default ``os.environ``) supplies ``HERMES_HOME``, ``HERMES_DUPLEX_CONFIG``, and every
    secret. Raises ``ConfigError`` naming the offending key when the file is missing or unreadable,
    a key is unknown or has the wrong type, a name is not one of the allowed values, a person or the
    router names an undefined profile, a person may use no profile or starts on one they may not use,
    two profiles share an alias, an alias is or starts with another profile's name or starts with
    "me", "us", or "you", two people are the Hermes owner, ``hermes.voice_context_files`` names an
    unknown file, lists ``user`` or ``briefing`` with no owner, or is set while a profile name is no
    Hermes profile id, a secret is written literally, or a named env var is unset or empty.
    Unset env vars are checked last: when only they are wrong, the error is a ``MissingEnvError`` that
    reports the first one, in file order, and lists them all.
    """
    env = os.environ if env is None else env
    config_path = resolve_config_path(path, env)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(str(config_path), "config file not found") from None
    except yaml.MarkedYAMLError as error:
        # Report only the position: the error's own text quotes the line, which may hold a secret.
        mark = error.problem_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigError(str(config_path), f"invalid YAML{where}: {error.problem}") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigError(str(config_path), f"cannot read config: {type(error).__name__}") from None

    env_refs: list[tuple[str, str]] = []
    root = _Section(raw if raw is not None else {}, "", env_refs)
    profiles = _profiles(root.section("profiles", required=True, by_name=True))
    config_dir = config_path.parent
    hermes_section = root.section("hermes", required=True)
    hermes = _hermes(hermes_section, profiles)
    provider = _provider(root.section("provider", required=True), config_dir)
    router = _router(root.section("router", required=True), profiles)
    people = _people(root.section("people", by_name=True), profiles)
    owner_files = sorted(hermes.voice_context_files & OWNER_CONTEXT_FILES)
    if owner_files and not any(p.hermes_owner for p in people.values()):
        raise ConfigError(
            hermes_section.child("voice_context_files"),
            f"lists {' and '.join(owner_files)}, but no person has hermes_owner: true, "
            "so it would never be used",
        )
    transports = _transports(root.section("transports"))
    audio = _audio(root.section("audio"))
    tasks = _tasks(root.section("tasks"))
    assistant = _assistant(root.section("assistant"))
    root.done()

    secrets, missing = _resolve_secrets(env_refs, env)
    config = Config(
        path=config_path,
        hermes_home=resolve_hermes_home(env),
        hermes=hermes,
        provider=provider,
        router=router,
        profiles=profiles,
        people=people,
        transports=transports,
        audio=audio,
        tasks=tasks,
        assistant=assistant,
        secrets=secrets,
    )
    if missing:
        raise MissingEnvError(missing, config)
    return config


def _profiles(section: _Section) -> Mapping[str, ProfileConfig]:
    profiles: dict[str, ProfileConfig] = {}
    # The spoken form of each alias -> the profile that claimed it first.
    alias_owners: dict[tuple[str, ...], str] = {}
    # Only a profile's exact name switches mid-sentence, so an alias that is, or starts with, another
    # profile's name would switch to that profile there ("talk to general news" to general).
    spoken_names = {spoken_form(name): name for name in section.names()}
    for name in section.names():
        if name in _RESERVED_PROFILE_NAMES:
            raise ConfigError(section.child(name), f"'{name}' {_RESERVED_PROFILE_NAMES[name]}")
        profile = section.section(name, required=True)
        models_section = profile.section("models", by_name=True)
        models: dict[str, ModelChoice] = {}
        for effort in models_section.names():
            if effort not in EFFORTS:
                raise ConfigError(
                    models_section.child(effort), f"unknown effort (allowed: {', '.join(EFFORTS)})"
                )
            models[effort] = _model_choice(models_section.section(effort, required=True))
        models_section.done()
        default_model = profile.optional_section("default_model")
        aliases = profile.texts("aliases")
        for alias in aliases:
            spoken = spoken_form(alias)
            if not spoken:
                raise ConfigError(profile.child("aliases"), f"alias '{alias}' must contain a word")
            if spoken[0] in NOT_A_TARGET:
                raise ConfigError(
                    profile.child("aliases"),
                    f"alias '{alias}' can't start with 'me', 'us', or 'you': \"talk to me\" is no switch",
                )
            other = next(
                (
                    other
                    for spoken_name, other in spoken_names.items()
                    if other != name and spoken[: len(spoken_name)] == spoken_name
                ),
                None,
            )
            if other is not None:
                raise ConfigError(
                    profile.child("aliases"),
                    f"alias '{alias}' starts with the name of the profile '{other}'",
                )
            owner = alias_owners.setdefault(spoken, name)
            if owner != name:
                raise ConfigError(
                    profile.child("aliases"), f"alias '{alias}' is used by both '{owner}' and '{name}'"
                )
        profiles[name] = ProfileConfig(
            description=profile.text("description") or "",
            examples=profile.texts("examples"),
            models=MappingProxyType(models),
            default_model=_model_choice(default_model) if default_model is not None else None,
            aliases=aliases,
            voice_context=_voice_context(profile),
        )
        profile.done()
    section.done()
    if not profiles:
        raise ConfigError(section.key, "at least one profile is required")
    return MappingProxyType(profiles)


def _voice_context(section: _Section) -> str | None:
    """The ``voice_context`` of a profile or person ``section``, checked; None when absent."""
    value = section.text("voice_context")
    if value is None:
        return None
    if not value.strip():
        raise ConfigError(section.child("voice_context"), "must contain more than whitespace")
    if len(value) > VOICE_CONTEXT_MAX_CHARS:
        raise ConfigError(
            section.child("voice_context"),
            f"is {len(value)} characters; at most {VOICE_CONTEXT_MAX_CHARS} are allowed",
        )
    return value


def _model_choice(section: _Section) -> ModelChoice:
    choice = ModelChoice(provider=section.required_text("provider"), model=section.required_text("model"))
    section.done()
    return choice


def _hermes(section: _Section, profiles: Mapping[str, ProfileConfig]) -> HermesConfig:
    api_base = section.required_text("api_base")
    if not api_base.startswith(("http://", "https://")):
        raise ConfigError(section.child("api_base"), "must be an http:// or https:// URL")
    keys = section.section("profile_keys_env", by_name=True)
    profile_keys_env: dict[str, str] = {}
    for profile in keys.names():
        if profile not in profiles:
            raise ConfigError(keys.child(profile), f"profile '{profile}' is not defined under profiles")
        profile_keys_env[profile] = keys.required_env_name(profile)
    keys.done()
    files = section.texts("voice_context_files")
    for kind in files:
        if kind not in HERMES_CONTEXT_FILES:
            raise ConfigError(
                section.child("voice_context_files"),
                _not_allowed("Hermes context file", kind, frozenset(HERMES_CONTEXT_FILES)),
            )
    if files:
        for name in profiles:
            if not HERMES_PROFILE_NAME.match(name.lower()):
                raise ConfigError(
                    section.child("voice_context_files"),
                    f"profile '{name}' is not a Hermes profile name (letters, digits, '-', '_'), "
                    "so its files cannot be found",
                )
    briefing_refresh_h = section.number("briefing_refresh_h", HermesConfig.briefing_refresh_h)
    if not math.isfinite(briefing_refresh_h) or briefing_refresh_h <= 0:
        raise ConfigError(section.child("briefing_refresh_h"), "must be a finite number greater than 0")
    config = HermesConfig(
        api_base=api_base.rstrip("/"),
        profile_keys_env=MappingProxyType(profile_keys_env),
        voice_context_files=frozenset(cast(tuple[ContextKind, ...], files)),
        voice_context_max_chars=section.integer(
            "voice_context_max_chars",
            HermesConfig.voice_context_max_chars,
            minimum=1,
            maximum=HERMES_CONTEXT_MAX_CHARS_LIMIT,
        ),
        briefing_refresh_h=briefing_refresh_h,
    )
    section.done()
    return config


def _provider(section: _Section, config_dir: Path) -> ProviderConfig:
    name = section.required_text("name")
    if name not in PROVIDER_NAMES:
        raise ConfigError(section.child("name"), _not_allowed("provider", name, PROVIDER_NAMES))

    is_fake = name == "fake"
    api_key_env = section.env_name("api_key_env")
    if api_key_env is None and not is_fake:
        raise ConfigError(section.child("api_key_env"), f"is required for provider '{name}'")

    turn_detection = section.free_mapping("turn_detection")
    if turn_detection is not None:
        kind = turn_detection.get("type")
        if kind not in TURN_DETECTION_TYPES:
            raise ConfigError(
                section.child("turn_detection.type"),
                f"must be one of {', '.join(sorted(TURN_DETECTION_TYPES))}",
            )
        if name == "xai-realtime" and kind != "server_vad":
            raise ConfigError(section.child("turn_detection.type"), "xai-realtime supports only server_vad")

    paths: dict[str, Path | None] = {}
    for key in ("script", "record_path"):
        value = section.text(key)
        if value is not None and not is_fake:
            raise ConfigError(section.child(key), "is only allowed for provider 'fake'")
        paths[key] = config_dir / Path(value).expanduser() if value is not None else None

    capabilities_section = section.optional_section("capabilities")
    if capabilities_section is not None and not is_fake:
        raise ConfigError(section.child("capabilities"), "is only allowed for provider 'fake'")
    capabilities = _capabilities(capabilities_section)

    config = ProviderConfig(
        name=name,
        model=section.text("model"),
        voice=section.text("voice"),
        api_key_env=api_key_env,
        turn_detection=turn_detection,
        script=paths["script"],
        record_path=paths["record_path"],
        capabilities=capabilities,
    )
    section.done()
    return config


def _capabilities(section: _Section | None) -> Mapping[str, int | bool | str] | None:
    """The capability overrides in ``section``, holding only the keys it sets; None when absent."""
    if section is None:
        return None
    overrides: dict[str, int | bool | str] = {}
    for name in ("input_rate", "output_rate", "max_session_s"):
        if section.has(name):
            overrides[name] = section.positive_int(name, 0)
    for name in (
        "supports_truncate",
        "supports_out_of_band_response",
        "emits_speech_events",
        "replaces_instructions",
    ):
        if section.has(name):
            overrides[name] = section.flag(name, False)
    style = section.text("delegation_style")
    if style is not None:
        if style not in DELEGATION_STYLES:
            raise ConfigError(
                section.child("delegation_style"), _not_allowed("delegation style", style, DELEGATION_STYLES)
            )
        overrides["delegation_style"] = style
    section.done()
    return MappingProxyType(overrides)


def _router(section: _Section, profiles: Mapping[str, ProfileConfig]) -> RouterConfig:
    backend = section.required_text("backend")
    if backend not in ROUTER_BACKENDS:
        raise ConfigError(section.child("backend"), _not_allowed("router backend", backend, ROUTER_BACKENDS))
    fast_path = section.text("fast_path")
    if fast_path is not None and fast_path not in FAST_PATH_BACKENDS:
        raise ConfigError(
            section.child("fast_path"), _not_allowed("fast-path backend", fast_path, FAST_PATH_BACKENDS)
        )
    if section.has("sticky_idle_s"):
        raise ConfigError(
            section.child("sticky_idle_s"),
            "was removed: each person now keeps an active profile until they switch; delete this key",
        )
    default_profile = section.text("default_profile") or RouterConfig.default_profile
    if default_profile not in profiles:
        raise ConfigError(
            section.child("default_profile"), f"profile '{default_profile}' is not defined under profiles"
        )
    suggest_threshold = section.number("suggest_threshold", RouterConfig.suggest_threshold)
    if not 0 < suggest_threshold <= 1:
        raise ConfigError(section.child("suggest_threshold"), "must be greater than 0 and at most 1")
    control = section.section("control_examples")
    control_examples = ControlExamples(status=control.texts("status"), cancel=control.texts("cancel"))
    control.done()
    decisions = _api_backend(section, "decisions", backend, DecisionsConfig)
    jev = _api_backend(section, "jev", backend, JevConfig)

    config = RouterConfig(
        backend=backend,
        fast_path=fast_path,
        deadline_ms=section.positive_int("deadline_ms", RouterConfig.deadline_ms),
        default_profile=default_profile,
        suggest_switch=section.flag("suggest_switch", RouterConfig.suggest_switch),
        suggest_threshold=suggest_threshold,
        control_examples=control_examples,
        decisions=decisions,
        jev=jev,
    )
    section.done()
    return config


def _api_backend[C](router: _Section, name: str, backend: str, config_type: Callable[..., C]) -> C | None:
    """The settings of the HTTP API backend ``name`` (``api_key_env``, optional ``model`` and
    ``base_url``) as ``config_type``, or None when absent; they are required when ``backend`` is
    ``name``."""
    section = router.optional_section(name)
    if section is None:
        if backend == name:
            raise ConfigError(router.child(f"{name}.api_key_env"), f"is required for router backend '{name}'")
        return None
    base_url = section.text("base_url")
    if base_url is not None and not base_url.startswith(("http://", "https://")):
        raise ConfigError(section.child("base_url"), "must be an http:// or https:// URL")
    config = config_type(
        api_key_env=section.required_env_name("api_key_env"),
        model=section.text("model"),
        base_url=base_url.rstrip("/") if base_url is not None else None,
    )
    section.done()
    return config


def _people(section: _Section, profiles: Mapping[str, ProfileConfig]) -> Mapping[str, PersonConfig]:
    people: dict[str, PersonConfig] = {}
    owner: str | None = None
    for name in section.names():
        person = section.section(name, required=True)
        allowed = person.texts("profiles")
        if not allowed:
            raise ConfigError(person.child("profiles"), 'must list at least one profile (or "*" for all)')
        for profile in allowed:
            if profile != ALL_PROFILES and profile not in profiles:
                raise ConfigError(
                    person.child("profiles"), f"profile '{profile}' is not defined under profiles"
                )
        default_profile = person.text("default_profile")
        if default_profile is not None:
            if default_profile not in profiles:
                raise ConfigError(
                    person.child("default_profile"),
                    f"profile '{default_profile}' is not defined under profiles",
                )
            if ALL_PROFILES not in allowed and default_profile not in allowed:
                raise ConfigError(
                    person.child("default_profile"),
                    f"profile '{default_profile}' is not one of the person's profiles",
                )
        hermes_owner = person.flag("hermes_owner", False)
        if hermes_owner:
            if owner is not None:
                raise ConfigError(
                    person.child("hermes_owner"), f"only one person may be the Hermes owner, and '{owner}' is"
                )
            owner = name
        people[name] = PersonConfig(
            discord_id=person.discord_id("discord_id"),
            web_token_env=person.env_name("web_token_env"),
            profiles=allowed,
            default_profile=default_profile,
            hermes_owner=hermes_owner,
            voice_context=_voice_context(person),
        )
        person.done()
    section.done()
    return MappingProxyType(people)


def _transports(section: _Section) -> TransportsConfig:
    discord_section = section.section("discord")
    discord_enabled = discord_section.flag("enabled", DiscordConfig.enabled)
    bot_token_env = discord_section.env_name("bot_token_env")
    if discord_enabled and bot_token_env is None:
        raise ConfigError(discord_section.child("bot_token_env"), "is required when Discord is enabled")
    discord_section.done()

    web_section = section.section("web")
    web = WebConfig(
        enabled=web_section.flag("enabled", WebConfig.enabled),
        host=web_section.text("host") or WebConfig.host,
        port=web_section.integer("port", WebConfig.port, minimum=0, maximum=65535),
    )
    web_section.done()
    section.done()
    return TransportsConfig(
        discord=DiscordConfig(enabled=discord_enabled, bot_token_env=bot_token_env), web=web
    )


def _audio(section: _Section) -> AudioConfig:
    gate_threshold_dbfs = section.number("gate_threshold_dbfs", AudioConfig.gate_threshold_dbfs)
    if gate_threshold_dbfs > 0:
        raise ConfigError(section.child("gate_threshold_dbfs"), "must be at most 0 dBFS")
    playback_queue_s = section.number("playback_queue_s", AudioConfig.playback_queue_s)
    if playback_queue_s <= 0:
        raise ConfigError(section.child("playback_queue_s"), "must be greater than 0")
    config = AudioConfig(
        pacer_lead_ms=section.positive_int("pacer_lead_ms", AudioConfig.pacer_lead_ms),
        gate_threshold_dbfs=gate_threshold_dbfs,
        playback_queue_s=playback_queue_s,
    )
    section.done()
    return config


def _tasks(section: _Section) -> TasksConfig:
    restore_max_age_h = section.number("restore_max_age_h", TasksConfig.restore_max_age_h)
    if restore_max_age_h <= 0:
        raise ConfigError(section.child("restore_max_age_h"), "must be greater than 0")
    section.done()
    return TasksConfig(restore_max_age_h=restore_max_age_h)


def _assistant(section: _Section) -> AssistantConfig:
    backchannel = section.text("backchannel") or AssistantConfig.backchannel
    if backchannel not in BACKCHANNELS:
        raise ConfigError(
            section.child("backchannel"),
            _not_allowed("backchannel level", backchannel, frozenset(BACKCHANNELS)),
        )
    name, persona = section.text("name"), section.text("persona")
    for key, value in (("name", name), ("persona", persona)):
        if value is not None and not value.strip():
            raise ConfigError(section.child(key), "must contain more than whitespace")
    config = AssistantConfig(name=name or AssistantConfig.name, persona=persona, backchannel=backchannel)
    section.done()
    return config


def _resolve_secrets(
    env_refs: list[tuple[str, str]], env: Mapping[str, str]
) -> tuple[Secrets, list[tuple[str, str]]]:
    """The secrets of the env vars in ``env_refs`` that are set, and the (key, env var name) of each
    reference to one that is unset or empty."""
    values: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for key, env_name in env_refs:
        if value := env.get(env_name):
            values[env_name] = value
        else:
            missing.append((key, env_name))
    return Secrets(values), missing


def _not_allowed(what: str, value: str, allowed: frozenset[str]) -> str:
    return f"unknown {what} '{value}' (allowed: {', '.join(sorted(allowed))})"


class _Section:
    """Reads one YAML mapping, tracking which keys were read so ``done()`` can reject the rest.

    A section of settings rejects a secret-looking key (``api_key``, ``token``, …) as soon as it is
    created, before any other check; a section keyed ``by_name`` (profiles, people) holds names, not
    settings, so it does not. A key whose value is null counts as absent. Every ``*_env`` name read
    through ``env_name`` is validated and appended, with its dotted key, to ``env_refs``, which child
    sections share.
    """

    def __init__(
        self, raw: object, key: str, env_refs: list[tuple[str, str]], *, by_name: bool = False
    ) -> None:
        if not isinstance(raw, dict):
            raise ConfigError(key or "<root>", "must be a mapping")
        self._raw: dict[Any, Any] = raw
        self._read: set[str] = set()
        self._env_refs = env_refs
        self.key = key
        if not by_name:
            for name in self.names():
                if name in _SECRET_KEYS:
                    raise ConfigError(self.child(name), _LITERAL_SECRET)

    def child(self, name: str) -> str:
        return f"{self.key}.{name}" if self.key else name

    def names(self) -> list[str]:
        """Every key of this mapping, for sections keyed by name (profiles, people)."""
        names = []
        for name in self._raw:
            if not isinstance(name, str):
                raise ConfigError(self.child(str(name)), "key must be a string")
            names.append(name)
        return names

    def optional_section(self, name: str, *, by_name: bool = False) -> _Section | None:
        """The mapping under ``name``, or None when absent."""
        value = self._value(name)
        if value is None:
            return None
        return _Section(value, self.child(name), self._env_refs, by_name=by_name)

    def section(self, name: str, *, required: bool = False, by_name: bool = False) -> _Section:
        """The mapping under ``name``; an empty section when absent and not ``required``."""
        section = self.optional_section(name, by_name=by_name)
        if section is not None:
            return section
        if required:
            raise ConfigError(self.child(name), "is required")
        return _Section({}, self.child(name), self._env_refs)

    def has(self, name: str) -> bool:
        """Whether ``name`` holds a non-null value; counts as reading it."""
        return self._value(name) is not None

    def text(self, name: str) -> str | None:
        value = self._value(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigError(self.child(name), "must be a non-empty string")
        return value

    def required_text(self, name: str) -> str:
        value = self.text(name)
        if value is None:
            raise ConfigError(self.child(name), "is required")
        return value

    def texts(self, name: str) -> tuple[str, ...]:
        value = self._value(name)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ConfigError(self.child(name), "must be a list of non-empty strings")
        return tuple(value)

    def flag(self, name: str, default: bool) -> bool:
        value = self._value(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ConfigError(self.child(name), "must be true or false")
        return value

    def integer(self, name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
        value = self._value(name)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(self.child(name), "must be an integer")
        if value < minimum or (maximum is not None and value > maximum):
            bounds = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
            raise ConfigError(self.child(name), f"must be {bounds}")
        return value

    def positive_int(self, name: str, default: int) -> int:
        return self.integer(name, default, minimum=1)

    def number(self, name: str, default: float) -> float:
        value = self._value(name)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(self.child(name), "must be a number")
        return float(value)

    def discord_id(self, name: str) -> int | None:
        value = self._value(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | str) or not _DISCORD_ID.match(str(value)):
            raise ConfigError(self.child(name), "must be a Discord user id (digits)")
        return int(value)

    def env_name(self, name: str) -> str | None:
        """The env var name under ``name``, recorded for secret resolution; None when absent.

        The value is not echoed in errors: a malformed name may be a pasted secret.
        """
        value = self._value(name)
        if value is None:
            return None
        if not isinstance(value, str) or not _ENV_NAME.match(value):
            raise ConfigError(
                self.child(name), "must name an environment variable (A-Z, 0-9, _), not hold a value"
            )
        self._env_refs.append((self.child(name), value))
        return value

    def required_env_name(self, name: str) -> str:
        value = self.env_name(name)
        if value is None:
            raise ConfigError(self.child(name), "is required")
        return value

    def free_mapping(self, name: str) -> Mapping[str, Any] | None:
        """A mapping passed through as is (provider settings), still checked for literal secrets."""
        value = self._value(name)
        if value is None:
            return None
        _Section(value, self.child(name), self._env_refs)
        return MappingProxyType(dict(value))

    def done(self) -> None:
        """Reject every key that was not read."""
        for name in self.names():
            if name not in self._read:
                raise ConfigError(self.child(name), "unknown key")

    def _value(self, name: str) -> Any:
        self._read.add(name)
        return self._raw.get(name)
