from __future__ import annotations

import os
import re

from screens.base_screen import BaseDocumentScreen


class AnalysisScreen(BaseDocumentScreen):
    requires_image_input = False
    show_single_page_actions = False

    screen_title = "史料分析"
    cache_dir_name = "Analysis_Cache"
    right_panel_title = "\uf080 史料价值甄别区"
    primary_action_label = "开始智能分析"
    task_short_name = "分析"
    progress_verb = "分析"
    force_full_label = "强制重新分析"
    export_dialog_title = "保存分析报告"
    empty_page_marker = "（本页暂无分析结果）"
    idle_editor_hint = (
        "👈 请在左侧选择一份已下载的史料 PDF。\n\n"
        "此处将按「背景—主体—态度—价值」四维度输出结构化甄别意见，便于复核与写作引用。"
    )

    def get_academic_prompt(self) -> str:
          return """你是一位专攻日本近代政治与军事档案的顶尖研究者。
  
  请仔细阅读以下由 OCR 提取的 1921-1927 年间日本军政档案原文文本。你需要对日方对中国共产党、国民革命及反帝运动的「观察、认识、判断与因应」进行深度解构。
  
  【强制执行规则】：
  必须严格以 JSON 格式输出，绝对不要包含任何 Markdown 标记（如 ```json），直接输出纯 JSON 字符串。请严格遵循以下 JSON 结构与字段定义：
  
  {
    "Historical_Context": {
      "Date_Written": "提取文件的撰写或发布时间（请转换为 YYYY-MM-DD 格式，若仅有年月则填 YYYY-MM，未知填 null）",
      "Author_Sender": "提取发文者或报告人的职衔与姓名（如：驻广州总领事、特务机关长）",
      "Recipient": "提取收文者或呈报对象（如：外务大臣、参谋本部）",
      "Document_Type": "判断文书类型（如：情报报告、训令、电报、决议草案等）"
    },
    "Entities_and_Concepts": {
      "Organizations": ["提取文中出现的所有相关组织与机构（如：中国共产党、广州国民政府、省港罢工委员会等）"],
      "Key_Figures": ["提取文中出现的关键人物（如：陈独秀、鲍罗廷、孙中山等）"],
      "All_Figures": ["提取文中出现的所有人物姓名"],
      "Locations": ["提取事件发生的核心地理位置（如：广州、沙面、武汉等）"],
      "Discourse_Keywords": ["提取日方在公文中使用的具有『强烈主观色彩』或『话语权力』的历史专有名词（如：赤化、排外、暴支、容共、过激派等）"]
    },
    "Discourse_Analysis": {
      "Observation_Info": "（观察与认知）用一句话概括日方通过何种渠道获取了什么具体情报或事实？",
      "Core_Judgment": "（认识与判断）用一句话精准概括日方对该事件的定性或战略研判（如：认为受赤化思想主导、判断国共必将分裂等）。",
      "Response_Action": "（因应）用一句话概括日方已经采取或建议采取的具体对策（如无，填写『未提及』）。",
      "Relevance_Score": 1到5的整数（评估该史料对研究日本军政界应对中国共产革命与反帝运动的价值，1为无关流水账，5为具有极高战略研究价值的核心机密报告）
    }
  }"""

    def export_document(self) -> None:
        self._export_text_pages_default()

    def enrich_json_data(self, data: dict, pdf_path: str) -> dict:
        """在 JSON 写入硬盘前，利用正则解析文件名，自动注入元数据"""
        filename = os.path.basename(pdf_path)
        if filename.lower().endswith(".pdf"):
            filename = filename[:-4]

        # 匹配 core_scraper.py 中定义的标准文件名格式
        # 示例：戦前期外務省記録：「当地反帝国主義運動状況報告ノ件」、JACAR Ref. B03030289700（第（）—（）画像目）、『支那ニ於ケル利権回収問題一件/利権回収運動』（日本外交史料館）
        pattern = r"^(.*?)：「(.*?)」、JACAR Ref\.\s*(.*?)（.*?）、『(.*?)』[（\(](.*?)[）\)]$"
        match = re.match(pattern, filename)

        if match:
            level2, title, ref, parent, repo = match.groups()
            auto_cite = (
                f"{level2}：「{title}」、JACAR Ref. {ref}"
                f"（第（）—（）画像目）、『{parent}』（{repo}）"
            )

            # 将解析出的元数据强行插入大模型生成的 JSON 字典的最前方
            new_data = {
                "Document_ID": f"JACAR_{ref}",
                "Citation_Metadata": {
                    "Level2_Name": level2,
                    "Doc_Title": title,
                    "JACAR_Ref": ref,
                    "Image_Range": "（）—（）",  # 留空给后续大模型或人工确认
                    "Parent_Volume": parent,
                    "Repository": repo,
                    "Auto_Citation": auto_cite,
                },
            }
            # 合并大模型生成的其他部分（Historical_Context等）
            new_data.update(data)
            return new_data
        else:
            # 如果用户手动改坏了文件名导致正则失败，保留一个容错提示
            data["Citation_Metadata"] = {"Error": "文件名格式已被破坏或不标准，无法自动提取出处。"}
            return data
