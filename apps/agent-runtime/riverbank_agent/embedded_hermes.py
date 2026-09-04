"""Low-latency embedded Hermes adapter for latency-sensitive voice turns.

This module is the only RiverBank product layer allowed to import Hermes Python
internals.  It preserves a warm agent while presenting the small interface used
by the voice service, so another backend can replace it without rewriting ASR,
TTS, wake-word, or expression orchestration.
"""

from __future__ import annotations

import contextlib
import io
import logging
import threading
import time
from collections.abc import Callable


class EmbeddedHermesRuntime:
    """Warm, interruptible Hermes adapter for RiverBank voice interactions."""

    name = "hermes-embedded"

    def __init__(
        self,
        *,
        toolsets: set[str] | frozenset[str],
        mcp_discovery: bool,
        max_turns: int,
        timeout_seconds: float,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.toolsets = frozenset(toolsets)
        self.mcp_discovery = bool(mcp_discovery)
        self.max_turns = max(1, int(max_turns))
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.log = log or (lambda message: logging.getLogger(__name__).info(message))
        self.agent = None
        self.session_db = None
        self.turn_count = 0
        self.lock = threading.RLock()
        self.clarify_handler = None
        self.request_active = threading.Event()
        self.interrupt_requested = threading.Event()

    def available(self) -> bool:
        try:
            __import__("run_agent")
            return True
        except (ImportError, OSError):
            return False

    def handle_clarification(
        self,
        question: str,
        choices: list | None = None,
        multi_select: bool = False,
    ) -> str:
        if callable(self.clarify_handler):
            return str(self.clarify_handler(question, choices, multi_select))
        return "[语音界面当前无法取得补充信息，请简短说明需要用户稍后补充什么。]"

    def _build(self) -> None:
        started = time.monotonic()
        from gateway.session_context import declare_stateless_channel
        from hermes_cli.config import load_config, split_model_config_default
        from hermes_cli.env_loader import load_hermes_dotenv
        from hermes_cli.fallback_config import get_fallback_chain
        from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.tools_config import _get_platform_tools
        from hermes_state import SessionDB
        from run_agent import AIAgent

        load_hermes_dotenv()
        declare_stateless_channel()
        config = load_config()
        model_config = config.get("model") or {}
        if isinstance(model_config, str):
            model = model_config
            provider = None
        else:
            raw_model = model_config.get("default") or model_config.get("model") or ""
            if isinstance(raw_model, dict):
                model, _ = split_model_config_default(raw_model)
            else:
                model = str(raw_model or "")
            provider = str(model_config.get("provider") or "").strip() or None
        runtime = resolve_runtime_provider(requested=provider, target_model=model or None)
        available_toolsets = set(_get_platform_tools(config, "cli"))
        enabled_toolsets = sorted(available_toolsets.intersection(self.toolsets))
        if not enabled_toolsets:
            raise RuntimeError("RiverBank voice toolset filter matched no available tools")
        missing_toolsets = sorted(self.toolsets.difference(available_toolsets))
        if missing_toolsets:
            self.log(f"Voice Agent unavailable toolsets ignored: {missing_toolsets}")
        if self.mcp_discovery:
            ensure_mcp_discovery_before_agent_build(
                logger=logging.getLogger(__name__),
                single_query=True,
            )
        self.session_db = SessionDB()
        self.agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            model=model,
            enabled_toolsets=enabled_toolsets,
            quiet_mode=True,
            platform="cli",
            session_db=self.session_db,
            credential_pool=runtime.get("credential_pool"),
            fallback_model=get_fallback_chain(config) or None,
            clarify_callback=self.handle_clarification,
        )
        self.agent.suppress_status_output = True
        self.agent.stream_delta_callback = None
        self.agent.tool_gen_callback = None
        self.turn_count = 0
        self.log(
            "RiverBank voice Agent ready "
            f"in {time.monotonic() - started:.2f}s toolsets={enabled_toolsets}"
        )

    def warmup(self) -> None:
        with self.lock:
            if self.agent is None:
                try:
                    self._build()
                except Exception as exc:
                    self.log(f"RiverBank voice Agent warmup warning: {exc}")
                    self.close()

    def ask(self, prompt: str, stream_callback=None) -> str:
        with self.lock:
            if self.agent is None or self.turn_count >= self.max_turns:
                self.close()
                self._build()
            captured = io.StringIO()
            previous_stream_callback = self.agent.stream_delta_callback
            self.agent.stream_delta_callback = stream_callback
            self.request_active.set()
            timed_out = threading.Event()

            def abort_timed_out_request() -> None:
                timed_out.set()
                self.interrupt()

            watchdog = threading.Timer(self.timeout_seconds, abort_timed_out_request)
            watchdog.daemon = True
            watchdog.start()
            try:
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    result = self.agent.run_conversation(prompt)
            except Exception:
                self.close()
                raise
            finally:
                watchdog.cancel()
                self.request_active.clear()
                if self.agent is not None:
                    self.agent.stream_delta_callback = previous_stream_callback
                    if self.interrupt_requested.is_set():
                        clear_interrupt = getattr(self.agent, "clear_interrupt", None)
                        if callable(clear_interrupt):
                            try:
                                clear_interrupt()
                            except Exception as exc:
                                self.log(f"Voice Agent interrupt reset warning: {exc}")
                self.interrupt_requested.clear()
            if timed_out.is_set():
                self.close()
                raise TimeoutError(
                    f"RiverBank voice Agent request exceeded {self.timeout_seconds:.0f}s"
                )
            self.turn_count += 1
            response = str(result.get("final_response") or "").strip()
            if not response:
                raise RuntimeError("RiverBank voice Agent returned an empty response")
            return response

    def interrupt(self) -> bool:
        if not self.request_active.is_set():
            return False
        agent = self.agent
        if agent is None:
            return False
        self.interrupt_requested.set()
        hard_interrupt = getattr(agent, "hard_interrupt", None)
        try:
            if callable(hard_interrupt):
                hard_interrupt()
            else:
                agent.interrupt(hard_cancel=True)
            return True
        except Exception as exc:
            self.log(f"Voice Agent hard interrupt warning: {exc}")
            return False

    def close(self) -> None:
        self.request_active.clear()
        self.interrupt_requested.clear()
        if self.agent is not None:
            try:
                self.agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                self.agent.close()
            except Exception:
                pass
            self.agent = None
        if self.session_db is not None:
            try:
                self.session_db.close()
            except Exception:
                pass
            self.session_db = None
