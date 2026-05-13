from __future__ import annotations

import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable

from google import genai
from google.genai import types
from PIL import Image

from config.api_key_store import load_trace_config
from utils.gemini_trace_logger import append_trace_event, build_cache_event
from utils.token_logger import log_gemini_usage


class LlmService:
    """封装 Gemini 调用、超时、Token 记录与 Trace 记录。"""

    def __init__(self, api_key: str, project_root: str, timeout_sec: int = 120) -> None:
        self.api_key = api_key
        self.project_root = project_root
        self.timeout_sec = timeout_sec

    def update_api_key(self, api_key: str) -> None:
        self.api_key = api_key

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

    def _call_gemini_with_timeout(self, client, *, model_name: str, contents, config):
        timeout_s = max(1, int(self.timeout_sec))

        def _do_call():
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_do_call)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            future.cancel()
            raise RuntimeError(f"Gemini 请求超时（>{timeout_s}s）")
        finally:
            # 关键：不要在超时后等待后台线程结束，避免主流程被额外阻塞数分钟。
            executor.shutdown(wait=False, cancel_futures=True)

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
        if (image_bytes is None) == (source_text is None):
            raise ValueError("必须且仅能指定 image_bytes 与 source_text 其中之一")
        if not self.api_key:
            raise RuntimeError("未检测到 GOOGLE_GEMINI_API_KEY。请先配置后再调用 Gemini。")

        client = genai.Client(api_key=self.api_key)
        cfg = self._load_trace_cfg()
        include_full_text = cfg["include_full_text"]
        request_started_at = time.perf_counter()

        if image_bytes is not None:
            image = Image.open(io.BytesIO(image_bytes))
            contents = [academic_prompt, image]
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

        try:
            response = self._call_gemini_with_timeout(
                client,
                model_name=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    safety_settings=[
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
                ),
            )
            usage_summary = log_gemini_usage(
                getattr(response, "usage_metadata", None),
                file_name,
                behavior_name,
                model_name,
            )
            raw_text = response.text.strip()
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

            clean_text = raw_text
            if clean_text.startswith("```json"):
                clean_text = clean_text[7:-3].strip()
            elif clean_text.startswith("```"):
                clean_text = clean_text[3:-3].strip()

            try:
                data = json.loads(clean_text)
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
                return json.dumps(data, ensure_ascii=False, indent=2), usage_summary
            except json.JSONDecodeError:
                return raw_text, usage_summary
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
                    "elapsed_ms": int((time.perf_counter() - request_started_at) * 1000),
                },
            )
            raise RuntimeError(f"Gemini API 调用失败: {e}")
