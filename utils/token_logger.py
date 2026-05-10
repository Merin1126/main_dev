import csv
from datetime import datetime
from pathlib import Path


USD_TO_JPY = 1 / 0.0063
JPY_TO_CNY = 1 / 23.16


def _get_usage_value(usage_metadata, key, default=0):
    if usage_metadata is None:
        return default
    if isinstance(usage_metadata, dict):
        return int(usage_metadata.get(key, default) or 0)
    return int(getattr(usage_metadata, key, default) or 0)


def log_gemini_usage(
    usage_metadata,
    file_name,
    behavior_name="未知",
    model_name="gemini-3.1-pro-preview",
):
    prompt_token_count = _get_usage_value(usage_metadata, "prompt_token_count", 0)
    cached_content_token_count = _get_usage_value(usage_metadata, "cached_content_token_count", 0)

    # 1. 优先获取谷歌底层最准确的全局总数（若缺失则兜底为输入量）
    total_token_count = _get_usage_value(
        usage_metadata,
        "total_token_count",
        prompt_token_count
    )

    # 2. 利用倒推法获取精准的输出 Token（解决 SDK candidates_token_count 字段漏单问题）
    candidates_token_count = max(0, total_token_count - prompt_token_count)

    prompt_non_cached = max(prompt_token_count - cached_content_token_count, 0)

    if model_name == "gemini-3-flash-preview":
        # Flash 预览版：无阶梯价
        input_price_per_m = 0.50
        cache_price_per_m = 0.05
        output_price_per_m = 3.00
    else:
        # Pro 预览版（以及未知模型的默认回退）：按提示词 token 阶梯计费
        if prompt_token_count <= 200_000:
            input_price_per_m = 2.00
            cache_price_per_m = 0.20
            output_price_per_m = 12.00
        else:
            input_price_per_m = 4.00
            cache_price_per_m = 0.40
            output_price_per_m = 18.00

    cost_usd = (
        (prompt_non_cached / 1_000_000) * input_price_per_m
        + (cached_content_token_count / 1_000_000) * cache_price_per_m
        + (candidates_token_count / 1_000_000) * output_price_per_m
    )
    cost_jpy = cost_usd * USD_TO_JPY
    cost_cny = cost_jpy * JPY_TO_CNY

    project_root = Path(__file__).resolve().parent.parent
    log_path = project_root / "api_cost_log.csv"
    file_exists = log_path.exists()

    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        file_name,
        behavior_name,
        model_name,
        prompt_non_cached,
        cached_content_token_count,
        candidates_token_count,
        total_token_count,
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
                    "总Token",
                    "预估美元(USD)",
                    "预估日元(JPY)",
                    "预估人民币(CNY)",
                ]
            )
        writer.writerow(row)

    print(
        f"[TokenLogger] 已记录: action={behavior_name}, total={total_token_count}, "
        f"CNY={cost_cny:.4f}, JPY={cost_jpy:.4f}"
    )

    return {
        "prompt_non_cached": prompt_non_cached,
        "cached_content_token_count": cached_content_token_count,
        "candidates_token_count": candidates_token_count,
        "total_token_count": total_token_count,
        "cost_usd": cost_usd,
        "cost_jpy": cost_jpy,
        "cost_cny": cost_cny,
    }
