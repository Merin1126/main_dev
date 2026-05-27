from __future__ import annotations

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable

from google import genai
from google.genai import types

from config.api_key_store import load_trace_config
from utils.gemini_trace_logger import append_trace_event, build_cache_event, build_context_cache_event
from utils.token_logger import log_context_cache_event, log_gemini_usage

DEBUG_LOG_PATH = "/Users/merin/本地文稿/Historical Records Scraper/main_dev/.cursor/debug-b75604.log"
DEBUG_SESSION_ID = "b75604"


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # region agent log
    try:
        payload = {
            "sessionId": DEBUG_SESSION_ID,
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # endregion


class LlmService:
    """封装 google-genai SDK 调用（含 OCR 单次调用 + Analysis/Translation 有状态 Chat Session）。

    设计要点：
    - 客户端 `self.client = genai.Client(api_key=...)` 在构造或 `update_api_key` 时按需重建；
    - OCR：单次 `client.models.generate_content`，多模态 `contents=[Part.from_bytes(...), prompt]`；
    - Analysis / Translation：`client.chats.create(model=..., config=...)` 创建会话，逐页 `chat.send_message`；
    - 所有调用统一走 `_run_with_timeout`，超时立即放弃后台线程，避免主流程被额外卡死。
    """

    DEFAULT_SAFETY_SETTINGS = [
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
    ]

    def __init__(
        self,
        api_key: str,
        project_root: str,
        timeout_sec: int = 120,
        min_interval_sec: float = 0.0,
    ) -> None:
        self.api_key = api_key or ""
        self.project_root = project_root
        self.timeout_sec = timeout_sec
        self.min_interval_sec = 0.0
        self._last_request_started_at = 0.0
        self.set_min_interval(min_interval_sec)
        self._client: genai.Client | None = None
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)

    def set_min_interval(self, min_interval_sec: float) -> None:
        try:
            self.min_interval_sec = max(0.0, float(min_interval_sec or 0.0))
        except (TypeError, ValueError):
            self.min_interval_sec = 0.0

    def update_api_key(self, api_key: str) -> None:
        new_key = (api_key or "").strip()
        if new_key == self.api_key and self._client is not None:
            return
        self.api_key = new_key
        self._client = genai.Client(api_key=new_key) if new_key else None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "未检测到 GOOGLE_GEMINI_API_KEY。请先配置后再调用 Gemini。"
                )
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _load_trace_cfg(self) -> dict[str, bool]:
        try:
            cfg = load_trace_config()
        except Exception:
            cfg = {"enabled": False, "include_full_text": True}
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "include_full_text": bool(cfg.get("include_full_text", True)),
        }

    @staticmethod
    def _trace_text(text: str | None, include_full_text: bool) -> str:
        value = "" if text is None else str(text)
        return value if include_full_text else value[:1200]

    def _trace_event(
        self,
        *,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        cfg = self._load_trace_cfg()
        if not cfg["enabled"]:
            return
        base_payload = {
            "event": event,
            "screen": screen_name,
            "task": task_name,
            "selected_pdf_path": selected_pdf_path,
            "trace_include_full_text": cfg["include_full_text"],
        }
        base_payload.update(payload or {})
        append_trace_event(self.project_root, base_payload)

    def trace_cache_write(
        self,
        *,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        cache_path: str,
        cache_kind: str,
        content: str,
        page_index: int | None,
    ) -> None:
        cfg = self._load_trace_cfg()
        if not cfg["enabled"]:
            return
        event = build_cache_event(
            project_root=self.project_root,
            screen_name=screen_name,
            task_name=task_name,
            pdf_path=selected_pdf_path,
            page_index=page_index,
            cache_path=cache_path,
            cache_kind=cache_kind,
            content=content,
            include_full_text=cfg["include_full_text"],
        )
        append_trace_event(self.project_root, event)

    def trace_context_cache_lifecycle(
        self,
        *,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        event: str,
        cache_name: str,
        model_name: str | None = None,
        ttl_seconds: int | None = None,
        source_fingerprint: str | None = None,
        reason: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """记录 context cache 生命周期事件（仅观测链路，不影响业务流程）。"""
        cfg = self._load_trace_cfg()
        if not cfg["enabled"]:
            return
        payload = build_context_cache_event(
            project_root=self.project_root,
            screen_name=screen_name,
            task_name=task_name,
            pdf_path=selected_pdf_path,
            event=event,
            cache_name=cache_name,
            model_name=model_name,
            ttl_seconds=ttl_seconds,
            source_fingerprint=source_fingerprint,
            reason=reason,
            extra_payload=extra_payload,
        )
        payload["trace_include_full_text"] = cfg["include_full_text"]
        append_trace_event(self.project_root, payload)

    def _run_with_timeout(self, fn: Callable[[], Any], *, timeout_override: int | None = None) -> Any:
        """阻塞执行 `fn`，超时立即放弃后台线程（避免线程清理拖延主流程）。

        `timeout_override` 用于 cache 生命周期类操作（delete/get/update），
        这些操作不应被默认 120s 长超时拖累 worker 线程的 finally 清理路径。
        """
        timeout_s = max(1, int(timeout_override if timeout_override is not None else self.timeout_sec))
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            future.cancel()
            raise RuntimeError(f"Gemini 请求超时（>{timeout_s}s）")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # explicit cache 生命周期类操作的短超时（秒）。
    # 这些 API 在网络异常时容易挂起，单独短超时避免 finally 清理路径卡死 worker 线程。
    _CACHE_LIFECYCLE_TIMEOUT_SEC = 15

    # Gemini CreateCachedContentConfig.display_name 上限（API 校验 len($) <= 128，按 UTF-8 字节计）。
    _CACHE_DISPLAY_NAME_MAX_BYTES = 128

    @classmethod
    def clamp_context_cache_display_name(cls, display_name: str | None) -> str | None:
        """将 display_name 截断到 Gemini API 允许的 128 UTF-8 字节以内。

        API 的 `len($) <= 128` 按 UTF-8 字节计，不能仅用 Python len(text)（日文文件名常字符少、字节多）。
        完整文件名应写入成本日志的 file_name 字段，而非依赖 display_name。
        """
        text = (display_name or "").strip()
        if not text:
            return None
        max_bytes = int(cls._CACHE_DISPLAY_NAME_MAX_BYTES)
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        cut = encoded[:max_bytes]
        while cut:
            try:
                return cut.decode("utf-8")
            except UnicodeDecodeError:
                cut = cut[:-1]
        return "analysis:cache"

    def _apply_request_rate_gate(self) -> None:
        """软限流：控制相邻请求最小间隔，降低突发频率导致的 429/503。"""
        min_gap = max(0.0, float(self.min_interval_sec or 0.0))
        if min_gap <= 0:
            self._last_request_started_at = time.monotonic()
            return
        now = time.monotonic()
        elapsed = now - self._last_request_started_at
        if elapsed < min_gap:
            wait = min_gap - elapsed
            _debug_log(
                "run1",
                "H8",
                "services/llm_service.py:_apply_request_rate_gate",
                "request delayed by rate gate",
                {"minGap": round(min_gap, 3), "elapsed": round(elapsed, 3), "wait": round(wait, 3)},
            )
            time.sleep(wait)
        self._last_request_started_at = time.monotonic()

    @staticmethod
    def _classify_error(err: Exception) -> str:
        msg = str(err).lower()
        if "timed out" in msg or "请求超时" in msg:
            return "timeout"
        if "503" in msg or "unavailable" in msg:
            return "service_unavailable"
        if "429" in msg or "resource_exhausted" in msg or "rate limit" in msg:
            return "rate_limited"
        if "server disconnected without sending a response" in msg:
            return "server_disconnected"
        if "connection reset" in msg or "connection aborted" in msg:
            return "connection_reset"
        if "cachedcontent" in msg or "cached content" in msg:
            return "cached_content_error"
        return "unknown"

    @staticmethod
    def _format_ttl_seconds(ttl_seconds: int | None) -> str | None:
        if ttl_seconds is None:
            return None
        ttl = max(1, int(ttl_seconds))
        return f"{ttl}s"

    @staticmethod
    def _safe_chat_history(chat) -> list[Any]:
        """尽力读取 chat history；若 SDK 版本差异导致失败则返回空列表。"""
        getter = getattr(chat, "get_history", None)
        if not callable(getter):
            return []
        try:
            hist = getter()
        except TypeError:
            try:
                hist = getter(False)
            except Exception:
                return []
        except Exception:
            return []
        return hist if isinstance(hist, list) else []

    def get_chat_history_count(self, chat) -> int:
        try:
            return len(self._safe_chat_history(chat))
        except Exception:
            return 0

    @staticmethod
    def _extract_model_text_from_history(history: list[Any]) -> str:
        """从 history 逆序提取最近一条 model 文本（若无则空字符串）。"""
        for item in reversed(history or []):
            role = getattr(item, "role", None)
            if role != "model":
                continue
            parts = getattr(item, "parts", None) or []
            chunks: list[str] = []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    chunks.append(str(text))
            if chunks:
                return "\n".join(chunks).strip()
        return ""

    @staticmethod
    def extract_context_cache_token_count(cache_obj: Any) -> int:
        """从 CachedContent.usage_metadata 读取 token 数（官方 caches.create 响应自带）。"""
        if cache_obj is None:
            return 0
        usage = getattr(cache_obj, "usage_metadata", None)
        if usage is None:
            return 0
        for key in ("total_token_count", "total_tokens"):
            try:
                value = (
                    usage.get(key, 0)
                    if isinstance(usage, dict)
                    else getattr(usage, key, 0)
                )
                count = int(value or 0)
                if count > 0:
                    return count
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def extract_context_cache_create_time_iso(cache_obj: Any) -> str:
        """从 CachedContent.create_time 提取 ISO 时间（用于 storage 存活时长锚点）。"""
        if cache_obj is None:
            return ""
        create_time = getattr(cache_obj, "create_time", None)
        if create_time is None:
            return ""
        if isinstance(create_time, str):
            return create_time.strip()
        iso = getattr(create_time, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                return str(create_time)
        return str(create_time)

    def _count_context_cache_tokens_fallback(
        self,
        *,
        model_name: str,
        system_instruction: str,
        cache_text: str,
    ) -> int:
        """count_tokens 兜底：官方文档注明 caching 暂不支持统一 count_tokens，失败时返回 0。"""
        try:
            count_resp = self.client.models.count_tokens(
                model=model_name,
                contents=[cache_text or ""],
                config=types.GenerateContentConfig(system_instruction=system_instruction),
            )
            return int(getattr(count_resp, "total_tokens", 0) or 0)
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # Chat Session：Analysis / Translation 的有状态多轮通道
    # ------------------------------------------------------------------ #

    def create_context_cache(
        self,
        *,
        model_name: str,
        system_instruction: str,
        cache_text: str,
        ttl_seconds: int | None = None,
        display_name: str | None = None,
    ):
        """创建 Gemini explicit context cache。"""
        raw_display_name = (display_name or "").strip()
        safe_display_name = self.clamp_context_cache_display_name(raw_display_name)
        config = types.CreateCachedContentConfig(
            system_instruction=system_instruction,
            contents=[cache_text or ""],
            display_name=safe_display_name,
            ttl=self._format_ttl_seconds(ttl_seconds),
        )
        try:
            created = self.client.caches.create(model=model_name, config=config)
            cache_token_count = self.extract_context_cache_token_count(created)
            if cache_token_count <= 0:
                cache_token_count = self._count_context_cache_tokens_fallback(
                    model_name=model_name,
                    system_instruction=system_instruction,
                    cache_text=cache_text,
                )
            try:
                log_context_cache_event(
                    event="create",
                    model_name=model_name,
                    cache_name=str(getattr(created, "name", "") or ""),
                    cache_tokens=cache_token_count,
                    storage_hours=0.0,
                    ttl_seconds=ttl_seconds,
                    file_name=raw_display_name or (safe_display_name or ""),
                    reused=False,
                    bill_write=True,
                )
            except Exception:
                pass
            return created
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 创建失败: {e}")

    def get_context_cache(self, *, cache_name: str):
        """获取 Gemini explicit cache 元数据（带短超时保护）。"""
        try:
            return self._run_with_timeout(
                lambda: self.client.caches.get(name=cache_name),
                timeout_override=self._CACHE_LIFECYCLE_TIMEOUT_SEC,
            )
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 获取失败: {e}")

    def list_context_caches(self) -> list[Any]:
        """列出账号下所有 context cache（带短超时保护）。

        失败/超时时返回空列表，调用方需自行处理"无法判断"的语义。
        主要用于启动巡检反向比对：找远端有但本地 sidecar 已丢的孤儿。
        """
        try:
            result = self._run_with_timeout(
                lambda: list(self.client.caches.list()),
                timeout_override=self._CACHE_LIFECYCLE_TIMEOUT_SEC,
            )
            return list(result or [])
        except Exception:
            return []

    def update_context_cache_ttl(
        self,
        *,
        cache_name: str,
        ttl_seconds: int,
        screen_name: str | None = None,
        task_name: str | None = None,
        selected_pdf_path: str | None = None,
    ):
        """更新 Gemini explicit cache TTL（带短超时保护 + 续期 trace）。"""
        config = types.UpdateCachedContentConfig(ttl=self._format_ttl_seconds(ttl_seconds))
        try:
            result = self._run_with_timeout(
                lambda: self.client.caches.update(name=cache_name, config=config),
                timeout_override=self._CACHE_LIFECYCLE_TIMEOUT_SEC,
            )
            if screen_name and task_name:
                self._trace_event(
                    screen_name=screen_name,
                    task_name=task_name,
                    selected_pdf_path=selected_pdf_path,
                    event="context_cache_refreshed",
                    payload={
                        "cache_name": cache_name,
                        "ttl_seconds": int(ttl_seconds or 0),
                    },
                )
                self.trace_context_cache_lifecycle(
                    screen_name=screen_name,
                    task_name=task_name,
                    selected_pdf_path=selected_pdf_path,
                    event="context_cache_refreshed",
                    cache_name=cache_name,
                    ttl_seconds=int(ttl_seconds or 0),
                    reason="update_context_cache_ttl",
                )
            return result
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 更新失败: {e}")

    def delete_context_cache(
        self,
        *,
        cache_name: str,
        screen_name: str | None = None,
        task_name: str | None = None,
        selected_pdf_path: str | None = None,
        cost_log: dict[str, Any] | None = None,
    ) -> None:
        """删除 Gemini explicit cache（带短超时保护 + 删除 trace）。

        【⚠️ Keyword-Only 约束】底层 SDK 要求 `name=` 必须显式具名传参，
        位置参数会触发 TypeError。

        `cost_log`（可选）由调用方传入已算好的 storage 结算字段，用于写入
        `api_cache_cost_log.csv` 的 delete 行。
        """
        try:
            self._run_with_timeout(
                lambda: self.client.caches.delete(name=cache_name),
                timeout_override=self._CACHE_LIFECYCLE_TIMEOUT_SEC,
            )
            if isinstance(cost_log, dict) and cost_log:
                try:
                    log_context_cache_event(
                        event="delete",
                        model_name=str(cost_log.get("model_name", "") or ""),
                        cache_name=cache_name,
                        cache_tokens=int(cost_log.get("cache_tokens", 0) or 0),
                        storage_hours=float(cost_log.get("storage_hours", 0) or 0),
                        file_name=str(cost_log.get("file_name", "") or ""),
                        reused=False,
                        bill_write=False,
                    )
                except Exception:
                    pass
            if screen_name and task_name:
                self._trace_event(
                    screen_name=screen_name,
                    task_name=task_name,
                    selected_pdf_path=selected_pdf_path,
                    event="context_cache_deleted",
                    payload={"cache_name": cache_name},
                )
                self.trace_context_cache_lifecycle(
                    screen_name=screen_name,
                    task_name=task_name,
                    selected_pdf_path=selected_pdf_path,
                    event="context_cache_deleted",
                    cache_name=cache_name,
                    reason="delete_context_cache",
                )
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 删除失败: {e}")

    def start_chat_session(
        self,
        model_name: str,
        system_instruction: str,
        response_mime_type: str = "text/plain",
        temperature: float = 0.3,
        cached_content: str | None = None,
        *,
        screen_name: str | None = None,
        task_name: str | None = None,
        selected_pdf_path: str | None = None,
    ):
        """创建一个全新的多轮 Chat 会话。

        【⚠️ Keyword-Only 约束】底层 SDK 要求 `model=`、`config=` 必须显式具名传参。
        """
        config_kwargs = {
            "response_mime_type": response_mime_type,
            "temperature": temperature,
            "safety_settings": self.DEFAULT_SAFETY_SETTINGS,
            "cached_content": (cached_content or None),
        }
        if not cached_content:
            config_kwargs["system_instruction"] = system_instruction
        config = types.GenerateContentConfig(**config_kwargs)
        try:
            chat = self.client.chats.create(model=model_name, config=config)
        except Exception as e:
            if cached_content:
                raise RuntimeError(f"CACHED_CONTENT_INVALID: {e}")
            raise RuntimeError(f"Gemini Chat 会话创建失败: {e}")
        session_id = uuid.uuid4().hex[:12]
        try:
            setattr(chat, "_hrs_session_id", session_id)
        except Exception:
            pass

        # Chat Session 级别追踪：补齐 system prompt 与配置记录，便于排查“为什么 turn 看起来没上下文”。
        if screen_name and task_name:
            cfg = self._load_trace_cfg()
            include_full_text = cfg["include_full_text"]
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="chat_session_started",
                payload={
                    "chat_session_id": session_id,
                    "model_name": model_name,
                    "response_mime_type": response_mime_type,
                    "temperature": temperature,
                    "cached_content": cached_content or "",
                    "history_turn_count": 0,
                    "system_instruction": self._trace_text(system_instruction, include_full_text),
                },
            )
            # 尝试观测 SDK 在 create 后的可见 history（不同版本 SDK 可能为空）。
            observed_history = self._safe_chat_history(chat)
            observed_model_text = self._extract_model_text_from_history(observed_history)
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="chat_session_observed",
                payload={
                    "chat_session_id": session_id,
                    "model_name": model_name,
                    "observed_history_count": len(observed_history),
                    "has_model_reaction": bool(observed_model_text),
                    "model_reaction_text": self._trace_text(observed_model_text, include_full_text),
                },
            )
        return chat

    def send_chat_message(
        self,
        chat,
        *,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        file_name: str,
        model_name: str,
        turn_prompt: str,
        behavior_name: str,
        page_index: int | None = None,
        enrich_json_data: Callable[[dict, str], dict] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """在已有 Chat Session 上发送一轮消息并返回 (文本结果, usage 摘要)。

        - 自动复用 `chat` 的 history（SDK 内部托管）；
        - 命中 JSON 时按 Analysis 的双轨存储约定写入 `Database_JSON/`；
        - Trace 与 Token 统计与 `detect_text` 保持一致。
        """
        cfg = self._load_trace_cfg()
        include_full_text = cfg["include_full_text"]
        request_started_at = time.perf_counter()
        session_id = getattr(chat, "_hrs_session_id", None)
        history_before = self._safe_chat_history(chat)

        self._trace_event(
            screen_name=screen_name,
            task_name=task_name,
            selected_pdf_path=selected_pdf_path,
            event="chat_turn_prepared",
            payload={
                "file_name": file_name,
                "page_index": page_index,
                "model_name": model_name,
                "chat_session_id": session_id,
                "history_count_before": len(history_before),
                "input_kind": "chat_text",
                "turn_prompt": self._trace_text(turn_prompt, include_full_text),
            },
        )
        _debug_log(
            "run2",
            "H11",
            "services/llm_service.py:send_chat_message:prepared",
            "chat turn prepared metrics",
            {
                "model": model_name,
                "pageIndex": page_index,
                "historyCountBefore": len(history_before),
                "turnPromptLen": len(turn_prompt or ""),
                "minIntervalSec": round(float(self.min_interval_sec or 0.0), 3),
            },
        )

        try:
            self._apply_request_rate_gate()
            response = self._run_with_timeout(lambda: chat.send_message(turn_prompt))
        except Exception as e:
            _debug_log(
                "run1",
                "H9",
                "services/llm_service.py:send_chat_message:exception",
                "chat request failed before retry layer",
                {"model": model_name, "pageIndex": page_index, "errorCategory": self._classify_error(e), "error": str(e)[:260]},
            )
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="chat_turn_error",
                payload={
                    "file_name": file_name,
                    "page_index": page_index,
                    "model_name": model_name,
                    "chat_session_id": session_id,
                    "history_count_before": len(history_before),
                    "error": str(e),
                    "error_category": self._classify_error(e),
                    "elapsed_ms": int((time.perf_counter() - request_started_at) * 1000),
                },
            )
            raise RuntimeError(f"Gemini Chat 调用失败: {e}")

        usage_summary = log_gemini_usage(
            getattr(response, "usage_metadata", None),
            file_name,
            behavior_name,
            model_name,
        )
        raw_text = (response.text or "").strip()
        elapsed_ms = int((time.perf_counter() - request_started_at) * 1000)
        history_after = self._safe_chat_history(chat)
        self._trace_event(
            screen_name=screen_name,
            task_name=task_name,
            selected_pdf_path=selected_pdf_path,
            event="chat_turn_response",
            payload={
                "file_name": file_name,
                "page_index": page_index,
                "model_name": model_name,
                "chat_session_id": session_id,
                "history_count_before": len(history_before),
                "history_count_after": len(history_after),
                "elapsed_ms": elapsed_ms,
                "usage_summary": usage_summary,
                "response_text": self._trace_text(raw_text, include_full_text),
                "response_len": len(raw_text),
            },
        )

        return self._postprocess_text_result(
            raw_text=raw_text,
            screen_name=screen_name,
            task_name=task_name,
            selected_pdf_path=selected_pdf_path,
            file_name=file_name,
            page_index=page_index,
            enrich_json_data=enrich_json_data,
        ), usage_summary

    def generate_text_once(
        self,
        *,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        file_name: str,
        model_name: str,
        prompt_text: str,
        behavior_name: str,
        page_index: int | None = None,
        enrich_json_data: Callable[[dict, str], dict] | None = None,
        response_mime_type: str = "text/plain",
        temperature: float = 0.3,
        cached_content: str | None = None,
        system_instruction: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """单次文本生成通道：支持 cached_content（无状态 Analysis 主通道）。

        【cached_content vs system_instruction 互斥】
        Gemini API 在 `cached_content` 与 `system_instruction` 同时存在时会报错；
        本函数自动处理互斥：若传入 `cached_content`，则忽略 `system_instruction`；
        若 `cached_content` 为空，则使用 `system_instruction`（用于 cache fallback 降级路径）。
        """
        if not self.api_key:
            raise RuntimeError("未检测到 GOOGLE_GEMINI_API_KEY。请先配置后再调用 Gemini。")

        cfg = self._load_trace_cfg()
        include_full_text = cfg["include_full_text"]
        request_started_at = time.perf_counter()
        clean_prompt = (prompt_text or "").strip()
        effective_system_instruction = (system_instruction or "").strip() if not cached_content else ""

        self._trace_event(
            screen_name=screen_name,
            task_name=task_name,
            selected_pdf_path=selected_pdf_path,
            event="request_prepared",
            payload={
                "file_name": file_name,
                "page_index": page_index,
                "model_name": model_name,
                "input_kind": "stateless_text",
                "cached_content": cached_content or "",
                "system_instruction_used": bool(effective_system_instruction),
                "response_mime_type": response_mime_type,
                "temperature": temperature,
                "prompt_text": self._trace_text(clean_prompt, include_full_text),
            },
        )

        config_kwargs: dict[str, Any] = {
            "safety_settings": self.DEFAULT_SAFETY_SETTINGS,
            "response_mime_type": response_mime_type,
            "temperature": temperature,
            "cached_content": (cached_content or None),
        }
        if effective_system_instruction:
            config_kwargs["system_instruction"] = effective_system_instruction
        config = types.GenerateContentConfig(**config_kwargs)
        try:
            self._apply_request_rate_gate()
            response = self._run_with_timeout(
                lambda: self.client.models.generate_content(
                    model=model_name,
                    contents=[clean_prompt],
                    config=config,
                )
            )
            usage_summary = log_gemini_usage(
                getattr(response, "usage_metadata", None),
                file_name,
                behavior_name,
                model_name,
            )
            raw_text = (response.text or "").strip()
            elapsed_ms = int((time.perf_counter() - request_started_at) * 1000)
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="response_received",
                payload={
                    "file_name": file_name,
                    "page_index": page_index,
                    "model_name": model_name,
                    "elapsed_ms": elapsed_ms,
                    "usage_summary": usage_summary,
                    "response_text": self._trace_text(raw_text, include_full_text),
                    "response_len": len(raw_text),
                },
            )
            return self._postprocess_text_result(
                raw_text=raw_text,
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                file_name=file_name,
                page_index=page_index,
                enrich_json_data=enrich_json_data,
            ), usage_summary
        except Exception as e:
            category = self._classify_error(e)
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="request_error",
                payload={
                    "file_name": file_name,
                    "page_index": page_index,
                    "model_name": model_name,
                    "cached_content": cached_content or "",
                    "error": str(e),
                    "error_category": category,
                    "elapsed_ms": int((time.perf_counter() - request_started_at) * 1000),
                },
            )
            if cached_content and category == "cached_content_error":
                raise RuntimeError(f"CACHED_CONTENT_INVALID: {e}")
            raise RuntimeError(f"Gemini API 调用失败: {e}")

    # ------------------------------------------------------------------ #
    # 单次调用：OCR
    # ------------------------------------------------------------------ #

    def detect_text(
        self,
        *,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        file_name: str,
        model_name: str,
        academic_prompt: str,
        behavior_name: str,
        page_index: int | None = None,
        image_bytes: bytes | None = None,
        source_text: str | None = None,
        enrich_json_data: Callable[[dict, str], dict] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """OCR 主通道：图片输入走多模态单次调用；纯文本兜底走单次调用。

        【⚠️ 多模态约束】图片输入必须封装为 `types.Part.from_bytes(data=..., mime_type=...)`，
        且 `contents` 必须是列表。控制参数通过 `types.GenerateContentConfig` 传入。
        """
        if (image_bytes is None) == (source_text is None):
            raise ValueError("必须且仅能指定 image_bytes 与 source_text 其中之一")
        if not self.api_key:
            raise RuntimeError("未检测到 GOOGLE_GEMINI_API_KEY。请先配置后再调用 Gemini。")

        cfg = self._load_trace_cfg()
        include_full_text = cfg["include_full_text"]
        request_started_at = time.perf_counter()

        if image_bytes is not None:
            contents = [
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                academic_prompt,
            ]
        else:
            contents = [f"{academic_prompt}\n\n【待处理的 OCR 史料底稿】：\n{source_text}"]

        self._trace_event(
            screen_name=screen_name,
            task_name=task_name,
            selected_pdf_path=selected_pdf_path,
            event="request_prepared",
            payload={
                "file_name": file_name,
                "page_index": page_index,
                "model_name": model_name,
                "input_kind": "image" if image_bytes is not None else "text",
                "prompt_text": self._trace_text(academic_prompt, include_full_text),
                "source_text": self._trace_text(source_text, include_full_text) if source_text is not None else None,
                "image_bytes_len": len(image_bytes) if image_bytes is not None else 0,
            },
        )

        config = types.GenerateContentConfig(
            safety_settings=self.DEFAULT_SAFETY_SETTINGS,
        )

        try:
            self._apply_request_rate_gate()
            response = self._run_with_timeout(
                lambda: self.client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
            )
            usage_summary = log_gemini_usage(
                getattr(response, "usage_metadata", None),
                file_name,
                behavior_name,
                model_name,
            )
            raw_text = (response.text or "").strip()
            elapsed_ms = int((time.perf_counter() - request_started_at) * 1000)
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="response_received",
                payload={
                    "file_name": file_name,
                    "page_index": page_index,
                    "model_name": model_name,
                    "elapsed_ms": elapsed_ms,
                    "usage_summary": usage_summary,
                    "response_text": self._trace_text(raw_text, include_full_text),
                    "response_len": len(raw_text),
                },
            )
            return self._postprocess_text_result(
                raw_text=raw_text,
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                file_name=file_name,
                page_index=page_index,
                enrich_json_data=enrich_json_data,
            ), usage_summary
        except Exception as e:
            self._trace_event(
                screen_name=screen_name,
                task_name=task_name,
                selected_pdf_path=selected_pdf_path,
                event="request_error",
                payload={
                    "file_name": file_name,
                    "page_index": page_index,
                    "model_name": model_name,
                    "error": str(e),
                    "error_category": self._classify_error(e),
                    "elapsed_ms": int((time.perf_counter() - request_started_at) * 1000),
                },
            )
            raise RuntimeError(f"Gemini API 调用失败: {e}")

    # ------------------------------------------------------------------ #
    # Shared post-processing：JSON 拦截 + Database_JSON 落盘
    # ------------------------------------------------------------------ #

    def _postprocess_text_result(
        self,
        *,
        raw_text: str,
        screen_name: str,
        task_name: str,
        selected_pdf_path: str | None,
        file_name: str,
        page_index: int | None,
        enrich_json_data: Callable[[dict, str], dict] | None,
    ) -> str:
        """JSON 路径：解析 → 元数据注入 → 写库；失败/非 JSON：原样返回。"""
        clean_text = raw_text
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:-3].strip()
        elif clean_text.startswith("```"):
            clean_text = clean_text[3:-3].strip()

        try:
            data = json.loads(clean_text)
        except json.JSONDecodeError:
            return raw_text

        if enrich_json_data and selected_pdf_path:
            data = enrich_json_data(data, selected_pdf_path)

        db_dir = os.path.join(self.project_root, "Database_JSON")
        os.makedirs(db_dir, exist_ok=True)
        doc_id = data.get("Document_ID", file_name.replace(".pdf", ""))
        safe_doc_id = re.sub(r'[\\/:*?"<>|]+', "_", str(doc_id)).strip("_") or "unknown_document"
        page_match = re.search(r"第(\d+)页", file_name)
        if page_match:
            page_suffix = f"_p{int(page_match.group(1)):04d}"
        else:
            page_suffix = f"_seg_{abs(hash(file_name)) % 100000:05d}"
        json_save_path = os.path.join(db_dir, f"{safe_doc_id}{page_suffix}.json")
        with open(json_save_path, "w", encoding="utf-8") as jf:
            json.dump(data, jf, ensure_ascii=False, indent=2)
        self.trace_cache_write(
            screen_name=screen_name,
            task_name=task_name,
            selected_pdf_path=selected_pdf_path,
            cache_path=json_save_path,
            cache_kind="Database_JSON",
            content=json.dumps(data, ensure_ascii=False, indent=2),
            page_index=page_index,
        )
        return json.dumps(data, ensure_ascii=False, indent=2)
