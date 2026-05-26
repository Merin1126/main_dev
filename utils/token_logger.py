"""Token 拆算与本地账单写入（适配 google-genai SDK）。

v2.6.6 起对齐新版 SDK 的 `response.usage_metadata` 字段语义：

- `prompt_token_count`：**总输入 Token（已包含缓存命中部分）**。
- `cached_content_token_count`：隐式 / 显式缓存命中 Token（缓存费率，未命中时为 0 或 None）。
- `candidates_token_count`：输出 Token（不再倒推自 total - prompt）。
- `thoughts_token_count`：思维链 Token（Gemini 3 thinking 模型；非思维模型为 0），
  按 Google 计费口径与 `candidates` 一同计入"输出费率"。
- `total_token_count`：SDK 自报的总 Token（= prompt + candidates + thoughts）。

【⚠️ 核心拆算口径（用于计费）】
    cached_tokens     = getattr(usage_metadata, 'cached_content_token_count', 0) or 0
    total_input       = getattr(usage_metadata, 'prompt_token_count', 0) or 0
    non_cached_input  = total_input - cached_tokens
    output_tokens     = getattr(usage_metadata, 'candidates_token_count', 0) or 0
    thoughts_tokens   = getattr(usage_metadata, 'thoughts_token_count', 0) or 0

计费等式（USD / 1M tokens）：
    cost = non_cached_input * input_rate
         + cached_tokens    * cache_rate
         + (output_tokens + thoughts_tokens) * output_rate

每一行 CSV 同时写入 `non_cached_input` / `cached_tokens` / `output_tokens`，
便于在 Excel 中一眼校验"隐式缓存"是否生效（看 `缓存命中Token` 列是否 > 0）。
"""

import csv
from datetime import datetime
from pathlib import Path


USD_TO_JPY = 1 / 0.0063
JPY_TO_CNY = 1 / 23.16


def _safe_int(usage_metadata, key: str) -> int:
    """从 SDK 的 usage_metadata 安全获取整数字段：None / 缺失 / 非数字一律回退为 0。

    兼容三种返回形态：
    - Pydantic-like model（新版 google-genai SDK 的默认形态，属性访问）
    - dict（手工 mock / 兼容旧版本）
    - None（极端异常分支）
    """
    if usage_metadata is None:
        return 0
    if isinstance(usage_metadata, dict):
        value = usage_metadata.get(key, 0)
    else:
        value = getattr(usage_metadata, key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_model_pricing(model_name: str, total_input: int) -> dict[str, float]:
    """返回模型价格（USD / 1M tokens）。

    字段定义：
    - input_price_per_m: 非缓存输入单价
    - cache_read_price_per_m: cached_content 读命中单价
    - output_price_per_m: 输出 + thoughts 单价
    - cache_write_price_per_m: explicit cache 创建写入单价（通常与 input 同价）
    - cache_storage_price_per_m_per_hour: explicit cache 存储小时单价
    """
    if model_name == "gemini-3-flash-preview":
        return {
            "input_price_per_m": 0.50,
            "cache_read_price_per_m": 0.05,
            "output_price_per_m": 3.00,
            "cache_write_price_per_m": 0.50,
            "cache_storage_price_per_m_per_hour": 1.00,
        }

    # Pro 预览档沿用现有阶梯价；context cache 存储按官方价 $4.50 / 1M / 小时。
    if total_input <= 200_000:
        input_price_per_m = 2.00
        cache_read_price_per_m = 0.20
        output_price_per_m = 12.00
    else:
        input_price_per_m = 4.00
        cache_read_price_per_m = 0.40
        output_price_per_m = 18.00
    return {
        "input_price_per_m": input_price_per_m,
        "cache_read_price_per_m": cache_read_price_per_m,
        "output_price_per_m": output_price_per_m,
        "cache_write_price_per_m": input_price_per_m,
        "cache_storage_price_per_m_per_hour": 4.50,
    }


def log_gemini_usage(
    usage_metadata,
    file_name,
    behavior_name="未知",
    model_name="gemini-3.1-pro-preview",
):
    # --- 1) 严格按"4 步拆算口径"提取，并做 None 容错 ---
    cached_tokens = _safe_int(usage_metadata, "cached_content_token_count")
    total_input = _safe_int(usage_metadata, "prompt_token_count")
    non_cached_input = max(0, total_input - cached_tokens)
    output_tokens = _safe_int(usage_metadata, "candidates_token_count")
    thoughts_tokens = _safe_int(usage_metadata, "thoughts_token_count")

    # SDK 自报 total 兜底：若缺失则按官方等式重组。
    total_token_count = _safe_int(usage_metadata, "total_token_count") or (
        total_input + output_tokens + thoughts_tokens
    )

    # --- 2) 按模型/档位选择 USD/1M 价格 ---
    pricing = _resolve_model_pricing(model_name, total_input)
    input_price_per_m = pricing["input_price_per_m"]
    cache_read_price_per_m = pricing["cache_read_price_per_m"]
    output_price_per_m = pricing["output_price_per_m"]

    # --- 3) 三段式计费：非缓存输入 + 缓存输入 + （输出 + 思维） ---
    billable_output = output_tokens + thoughts_tokens
    input_non_cache_cost_usd = (non_cached_input / 1_000_000) * input_price_per_m
    cache_read_cost_usd = (cached_tokens / 1_000_000) * cache_read_price_per_m
    output_cost_usd = (billable_output / 1_000_000) * output_price_per_m
    cost_usd = (
        input_non_cache_cost_usd
        + cache_read_cost_usd
        + output_cost_usd
    )
    cost_jpy = cost_usd * USD_TO_JPY
    cost_cny = cost_jpy * JPY_TO_CNY

    cache_hit_ratio = (cached_tokens / total_input) if total_input > 0 else 0.0
    # 统一使用 billable 口径，避免 total_token_count 语义变化影响审计。
    billable_total_tokens = non_cached_input + cached_tokens + billable_output

    # --- 4) 写入 api_cost_log.csv ---
    project_root = Path(__file__).resolve().parent.parent
    log_path = project_root / "api_cost_log.csv"
    file_exists = log_path.exists()

    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        file_name,
        behavior_name,
        model_name,
        non_cached_input,           # 输入Token(非缓存)
        cached_tokens,              # 缓存命中Token
        output_tokens,              # 输出Token（严格等于 SDK candidates_token_count）
        thoughts_tokens,            # 思维Token(Thoughts) — Gemini 3 thinking 模型才会 > 0
        total_token_count,          # SDK 原始总Token（保留原值）
        billable_total_tokens,      # 计费核算总Token（非缓存输入 + 缓存输入 + 输出含thoughts）
        f"{cache_hit_ratio * 100:.2f}",  # 缓存命中率(%)，便于人工核对隐式缓存是否生效
        f"{input_non_cache_cost_usd:.8f}",
        f"{cache_read_cost_usd:.8f}",
        f"{output_cost_usd:.8f}",
        f"{cost_usd:.8f}",
        f"{cost_jpy:.8f}",
        f"{cost_cny:.8f}",
    ]

    with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "时间",
                    "文件名",
                    "行为类型",
                    "模型名",
                    "输入Token(非缓存)",
                    "缓存命中Token",
                    "输出Token",
                    "思维Token(Thoughts)",
                    "总Token(SDK)",
                    "总Token(计费核算)",
                    "缓存命中率(%)",
                    "费用-非缓存输入(USD)",
                    "费用-缓存读取(USD)",
                    "费用-输出(USD)",
                    "预估美元(USD)",
                    "预估日元(JPY)",
                    "预估人民币(CNY)",
                ]
            )
        writer.writerow(row)

    print(
        f"[TokenLogger] action={behavior_name} | total={total_token_count} "
        f"(in={total_input} [cache={cached_tokens}/{cache_hit_ratio * 100:.1f}%] "
        f"out={output_tokens} thoughts={thoughts_tokens}) | "
        f"CNY={cost_cny:.4f} | JPY={cost_jpy:.4f}"
    )

    return {
        # 既有键保持向后兼容，base_screen 的累计器无需改动即可继续工作。
        "prompt_non_cached": non_cached_input,
        "cached_content_token_count": cached_tokens,
        "candidates_token_count": output_tokens,
        "total_token_count": total_token_count,
        "cost_usd": cost_usd,
        "cost_jpy": cost_jpy,
        "cost_cny": cost_cny,
        "input_non_cache_cost_usd": input_non_cache_cost_usd,
        "cache_read_cost_usd": cache_read_cost_usd,
        "output_cost_usd": output_cost_usd,
        # v2.6.6 新增：思维链 Token 与缓存命中率，便于下游 UI / 审计扩展。
        "thoughts_token_count": thoughts_tokens,
        "billable_output_tokens": billable_output,
        "billable_total_tokens": billable_total_tokens,
        "cache_hit_ratio": cache_hit_ratio,
    }


def log_context_cache_event(
    *,
    event: str,
    model_name: str,
    cache_name: str,
    cache_tokens: int,
    ttl_seconds: int | None = None,
    file_name: str = "",
    behavior_name: str = "analysis_context_cache",
    storage_hours: float | None = None,
    reused: bool = False,
) -> dict:
    """记录 explicit context cache 的创建/复用/删除/续期成本估算。

    - event: create / reuse / delete / refresh
    - cache_tokens: 缓存文本对应 token 数（建议来自 count_tokens）
    - reused: 是否命中已有 cache（True 表示节省了 cache write 费用）。
      reuse 路径下 `write_cost_usd` 强制写 0，仅记录存储费估算便于审计。
    """
    cached_tokens = max(0, int(cache_tokens or 0))
    pricing = _resolve_model_pricing(model_name, cached_tokens)
    write_price_per_m = pricing["cache_write_price_per_m"]
    storage_price_per_m_per_hour = pricing["cache_storage_price_per_m_per_hour"]

    if storage_hours is None:
        storage_hours = max(0.0, (_safe_float(ttl_seconds) / 3600.0) if ttl_seconds else 0.0)
    else:
        storage_hours = max(0.0, _safe_float(storage_hours))

    if reused:
        write_cost_usd = 0.0
    else:
        write_cost_usd = (cached_tokens / 1_000_000) * write_price_per_m
    storage_cost_usd = (
        (cached_tokens / 1_000_000) * storage_price_per_m_per_hour * storage_hours
    )
    total_cost_usd = write_cost_usd + storage_cost_usd
    total_cost_jpy = total_cost_usd * USD_TO_JPY
    total_cost_cny = total_cost_jpy * JPY_TO_CNY

    project_root = Path(__file__).resolve().parent.parent
    log_path = project_root / "api_cache_cost_log.csv"
    file_exists = log_path.exists()
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        event,
        "1" if reused else "0",
        file_name,
        behavior_name,
        model_name,
        cache_name,
        cached_tokens,
        f"{storage_hours:.6f}",
        f"{write_cost_usd:.8f}",
        f"{storage_cost_usd:.8f}",
        f"{total_cost_usd:.8f}",
        f"{total_cost_jpy:.8f}",
        f"{total_cost_cny:.8f}",
    ]
    with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "时间",
                    "事件",
                    "是否复用",
                    "文件名",
                    "行为类型",
                    "模型名",
                    "缓存名",
                    "缓存Token",
                    "存储小时",
                    "写入费用(USD)",
                    "存储费用(USD)",
                    "总费用(USD)",
                    "总费用(JPY)",
                    "总费用(CNY)",
                ]
            )
        writer.writerow(row)

    return {
        "event": event,
        "reused": reused,
        "cache_name": cache_name,
        "cache_tokens": cached_tokens,
        "storage_hours": storage_hours,
        "write_cost_usd": write_cost_usd,
        "storage_cost_usd": storage_cost_usd,
        "total_cost_usd": total_cost_usd,
        "total_cost_jpy": total_cost_jpy,
        "total_cost_cny": total_cost_cny,
    }
