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
from utils.gemini_trace_logger import append_trace_event, build_cache_event
from utils.token_logger import log_gemini_usage

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

    def _run_with_timeout(self, fn: Callable[[], Any]) -> Any:
        """阻塞执行 `fn`，超时立即放弃后台线程（避免线程清理拖延主流程）。"""
        timeout_s = max(1, int(self.timeout_sec))
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            future.cancel()
            raise RuntimeError(f"Gemini 请求超时（>{timeout_s}s）")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

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
        config = types.CreateCachedContentConfig(
            system_instruction=system_instruction,
            contents=[cache_text or ""],
            display_name=(display_name or "").strip() or None,
            ttl=self._format_ttl_seconds(ttl_seconds),
        )
        try:
            return self.client.caches.create(model=model_name, config=config)
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 创建失败: {e}")

    def get_context_cache(self, *, cache_name: str):
        """获取 Gemini explicit cache 元数据。"""
        try:
            return self.client.caches.get(name=cache_name)
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 获取失败: {e}")

    def update_context_cache_ttl(self, *, cache_name: str, ttl_seconds: int):
        """更新 Gemini explicit cache TTL。"""
        config = types.UpdateCachedContentConfig(ttl=self._format_ttl_seconds(ttl_seconds))
        try:
            return self.client.caches.update(name=cache_name, config=config)
        except Exception as e:
            raise RuntimeError(f"Gemini context cache 更新失败: {e}")

    def delete_context_cache(self, *, cache_name: str) -> None:
        """删除 Gemini explicit cache。"""
        try:
            self.client.caches.delete(name=cache_name)
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
