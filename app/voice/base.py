"""Provider-neutral voice connection.

CallSession talks only to this interface. OpenAISipConnection implements it today; a
media-stream bridge (Telnyx WebSocket <-> Gemini Live / others) will implement it later.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class CallerTranscript:
    text: str


@dataclass
class AgentTranscript:
    text: str


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ResponseDone:
    # True when the finished response asked for tools; we must answer with a new response.
    had_tool_calls: bool = False


@dataclass
class CallerSpeechStarted:
    pass


@dataclass
class PlaybackFinished:
    """The caller has heard everything the agent generated so far."""


@dataclass
class ProviderError:
    message: str


@dataclass
class Closed:
    reason: str = ""


VoiceEvent = (
    CallerTranscript | AgentTranscript | ToolCall | ResponseDone | CallerSpeechStarted
    | PlaybackFinished | ProviderError | Closed
)


class VoiceConnection(ABC):
    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[VoiceEvent]: ...

    @abstractmethod
    async def send_tool_result(self, call_id: str, output: dict) -> None: ...

    @abstractmethod
    async def request_response(self, instructions: str | None = None) -> None:
        """Ask the model to speak now (optionally with one-off instructions)."""

    @abstractmethod
    async def inject_context(self, text: str) -> None:
        """Quietly add information the model should know (not spoken on its own)."""

    @abstractmethod
    async def update_instructions(self, instructions: str) -> None: ...

    @abstractmethod
    async def transfer(self, target_uri: str) -> None: ...

    @abstractmethod
    async def hangup(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...
