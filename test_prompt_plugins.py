from __future__ import annotations

from config.academic_prompts import render_analysis_prompt
from config.translation_prompts import TRANSLATION_PLUGINS


PLACEHOLDER = "__TRANSLATION_PLUGIN_ENUM__"


def render_analysis_prompt_with_plugins() -> str:
    plugin_enum = "』、『".join(TRANSLATION_PLUGINS.keys())
    return render_analysis_prompt(f"『{plugin_enum}』")


def main() -> int:
    rendered = render_analysis_prompt_with_plugins()

    # 1) 占位符必须被替换掉
    assert PLACEHOLDER not in rendered, "占位符未被替换，仍存在 __TRANSLATION_PLUGIN_ENUM__"

    # 2) 所有插件名必须出现在提示词里（使用『插件名』格式）
    missing = [name for name in TRANSLATION_PLUGINS.keys() if f"『{name}』" not in rendered]
    assert not missing, f"以下插件未出现在渲染后的提示词中: {missing}"

    # 3) 输出关键片段方便人工核对（不调用任何 API）
    marker = '"Translation_Plugins"'
    idx = rendered.find(marker)
    if idx == -1:
        print("未找到 Translation_Plugins 段落，请检查 academic_prompts 模板。")
    else:
        start = max(0, idx - 40)
        end = min(len(rendered), idx + 260)
        print("=== 渲染片段（人工核对）===")
        print(rendered[start:end])
        print("========================")

    print(f"检查通过：共检测到 {len(TRANSLATION_PLUGINS)} 个插件，均成功注入 Analysis Prompt。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

