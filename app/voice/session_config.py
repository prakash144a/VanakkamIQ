"""Build the session config sent when accepting an OpenAI Realtime SIP call."""

from app.agent.tools import Tool


def build_realtime_session(
    *,
    model: str,
    instructions: str,
    voice: str,
    tools: list[Tool],
    transcription_model: str,
) -> dict:
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": {
                # Caller-side transcripts for the dashboard and conversation history.
                "transcription": {"model": transcription_model},
                # Semantic VAD waits for the caller to finish a thought; callers can interrupt.
                "turn_detection": {"type": "semantic_vad", "create_response": True, "interrupt_response": True},
            },
            "output": {"voice": voice},
        },
        "tools": [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools
        ],
        "tool_choice": "auto",
    }
