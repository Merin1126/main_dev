from __future__ import annotations

from config.academic_prompts import render_analysis_system, render_analysis_turn
from config.translation_prompts import (
    TRANSLATION_PLUGINS,
    render_translation_system,
    render_translation_turn,
)


def _render_analysis_system_with_plugins() -> str:
    plugin_enum = "』、『".join(TRANSLATION_PLUGINS.keys())
    return render_analysis_system(translation_plugin_enum=f"『{plugin_enum}』")


def _check_analysis_system_prompt() -> None:
    rendered = _render_analysis_system_with_plugins()

    # 1) 所有插件名必须出现在系统前缀里（使用『插件名』格式）
    missing = [name for name in TRANSLATION_PLUGINS.keys() if f"『{name}』" not in rendered]
    assert not missing, f"以下插件未出现在 Analysis 系统前缀中: {missing}"

    # 2) JSON Schema 关键字段必须存在
    for marker in ('"Historical_Context"', '"Discourse_Analysis"', "Translation_Plugins"):
        assert marker in rendered, f"Analysis 系统前缀缺少关键字段: {marker}"

    # 3) v2.6.6：跨页一致性约束需要直接写入 system，而非动态注入
    assert "跨页一致性强制约束" in rendered, "Analysis 系统前缀缺少跨页一致性强制约束段落"

    print("[Analysis] 系统前缀渲染 OK；插件枚举全部命中。")


def _check_analysis_turn_prompt() -> None:
    rendered = render_analysis_turn(page_number=3, page_text="第三页测试 OCR 文本片段")
    assert "<SOURCE_TEXT" in rendered and "page=\"3\"" in rendered, "Analysis turn 模板缺少 <SOURCE_TEXT page=...>"
    assert "第三页测试 OCR 文本片段" in rendered, "Analysis turn 模板未注入页面文本"
    print("[Analysis] turn 模板渲染 OK。")


def _check_translation_system_prompt() -> None:
    rendered = render_translation_system(
        active_plugins=["外交密电", "内阁阁议"],
        context_summary="第1页：日方判断国共必将分裂\n第2页：建议加强情报渗透",
    )
    assert "全局翻译底座法则" in rendered, "Translation 系统前缀缺少底座法则"
    assert "外交密电与照会专用" in rendered, "Translation 系统前缀未注入外交密电插件"
    assert "内阁阁议与政党辩论专用" in rendered, "Translation 系统前缀未注入内阁阁议插件"
    assert "全书剧情大纲" in rendered, "Translation 系统前缀缺少全书剧情大纲段落"

    # 不应再出现旧版滑动窗口/上一页译文拼接残留
    forbidden = ("prev_page_raw", "[上一页结尾]", "【上一页译文接续参考】")
    for token in forbidden:
        assert token not in rendered, f"Translation 系统前缀仍包含已废弃片段: {token}"
    print("[Translation] 系统前缀渲染 OK；废弃片段已彻底清理。")


def _check_translation_turn_prompt() -> None:
    rendered = render_translation_turn(
        page_number=5,
        page_text="第五页の原文サンプル",
        context_info="【翻译背景参数注入】\n本页文书类型为：情报报告。",
    )
    assert "第 5 页" in rendered, "Translation turn 模板未注入页码"
    assert "第五页の原文サンプル" in rendered, "Translation turn 模板未注入原文"
    assert "自然衔接" in rendered, "Translation turn 模板缺少跨轮衔接指令"
    forbidden = ("prev_page_raw", "上一页结尾", "上一页译文接续参考")
    for token in forbidden:
        assert token not in rendered, f"Translation turn 模板仍包含已废弃片段: {token}"
    print("[Translation] turn 模板渲染 OK。")


def main() -> int:
    _check_analysis_system_prompt()
    _check_analysis_turn_prompt()
    _check_translation_system_prompt()
    _check_translation_turn_prompt()
    print(f"检查通过：Analysis/Translation 的 system + turn 模板共 4 项渲染均通过，"
          f"插件总数 = {len(TRANSLATION_PLUGINS)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
