"""The discord.py private internals the voice receiver hooks still exist on the pinned discord.py.

discord.py cannot receive DAVE audio, so ``hermes_duplex.transports.discord.receiver`` reaches into its
voice connection state and voice websocket. A discord.py upgrade that renames any of these breaks
voice receive silently at runtime; this test makes it fail here instead. Checked statically, with
no live connection.
"""

import ast
import inspect
import textwrap
from importlib.metadata import version

import davey
import discord
import discord.gateway
import discord.opus
import discord.voice_state

# Attributes VoiceConnectionState.__init__ assigns on the instance, which the receiver reads or wraps.
CONNECTION_STATE_ATTRIBUTES = frozenset(
    {"hook", "secret_key", "dave_session", "dave_protocol_version", "ssrc", "ws"}
)

# Methods the receiver calls on the connection state.
CONNECTION_STATE_METHODS = ("add_socket_listener", "remove_socket_listener")


def _instance_attributes(cls: type) -> set[str]:
    """Names ``cls.__init__`` assigns as ``self.<name> = ...`` or ``self.<name>: T = ...``."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls.__init__)))
    targets: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
    return {
        target.attr
        for target in targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }


def test_discord_py_is_the_pinned_version() -> None:
    assert version("discord.py") == "2.7.1"


def test_voice_client_connection_is_a_voice_connection_state() -> None:
    hints = inspect.get_annotations(discord.VoiceClient.create_connection_state, eval_str=True)

    assert hints["return"] is discord.voice_state.VoiceConnectionState
    assert "self._connection: VoiceConnectionState = self.create_connection_state()" in inspect.getsource(
        discord.VoiceClient.__init__
    )


def test_connection_state_has_the_hooked_attributes() -> None:
    cls = discord.voice_state.VoiceConnectionState

    assert CONNECTION_STATE_ATTRIBUTES <= _instance_attributes(cls)
    for name in CONNECTION_STATE_METHODS:
        assert callable(getattr(cls, name)), name
    assert list(inspect.signature(cls.add_socket_listener).parameters) == ["self", "callback"]
    assert list(inspect.signature(cls.remove_socket_listener).parameters) == ["self", "callback"]


def test_voice_websocket_calls_an_overridable_hook() -> None:
    cls = discord.gateway.DiscordVoiceWebSocket

    assert inspect.iscoroutinefunction(cls._hook)
    assert "_hook" in _instance_attributes(cls)
    assert "await self._hook(self, msg)" in inspect.getsource(cls.received_message)


def test_dave_and_opus_entry_points_exist() -> None:
    assert davey.MediaType.audio
    assert callable(davey.DaveSession.decrypt)
    assert discord.opus.Decoder.SAMPLING_RATE == 48000
    assert discord.opus.Decoder.CHANNELS == 2
    assert callable(discord.opus.Decoder.decode)
