from collections import namedtuple
from pathlib import Path

# 定义项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

#全局版本号配置
APP_VERSION = "V3.1.1"

# OCR 预处理总开关与分档模式（off / mild / strong）
OCR_PREPROCESS_ENABLED = True
OCR_PREPROCESS_MODE = "mild"

# 各档图像增强参数：对比度 + 锐度
OCR_PREPROCESS_PROFILES = {
    "off": {"contrast": 1.0, "sharpness": 1.0},
    "mild": {"contrast": 1.3, "sharpness": 1.1},
    "strong": {"contrast": 1.5, "sharpness": 1.2},
}

# Analysis explicit context cache 开关（Gemini cachedContents）
ANALYSIS_EXPLICIT_CACHE_ENABLED = True
# 显式缓存默认 TTL（秒）
ANALYSIS_CACHE_TTL_SECONDS = 7200
# 命中已有缓存时是否自动续期 TTL。
# 默认 False：Analysis 任务结束（成功/取消/异常）会在 finally 中主动删除远端 cache，
# 自动续期意义不大；若未来希望保留 cache 让其自然过期再做二次分析，可改为 True
# 并将清理时机改成"按页清理或不清理"。
ANALYSIS_CACHE_AUTO_REFRESH_TTL = False

# 保留给未来可能需要的图片组件结构
imagesTupple = namedtuple("images", ["light", "dark"])

class Color:
    """全局 UI 调色盘 (Theme Colors)"""
    TRANSPARENT = "transparent" #   
    WHITE = "#FFFFFF" #
    BLACK = "#000000" #
    GRAY = "#CCCCCC" #
    RED = "#c60101" #
    ORANGE = "#d97706" #
    GREEN = "#017a01" #
    BLUE = "#035fa1" #
    
    # CustomTkinter 专属的明暗双色支持: (Light Mode, Dark Mode)
    TEXT = ("#000000", "#FFFFFF") #
    TEXT_GRAY = ("#535050", "#9e9a9a") #
    BG_CONTENT = ("#e0e0ff", "#2b2b31") #
    BG_BUTON_DND = ("#d7d7fe", "#393941") #
    BG_CONTENT_SECONDARY = ("#c2c2ff", "#393941") #
    BG_NAVIGATION = ("#d2d2fe", "#333338") #
    BG_BUTTON_NAVIGATION = "transparent" #
    BG_ACTIVE_BUTTON_NAVIGATION = ("#5d5d98", "#545473") #
    BG_HOVER_BUTTON_NAVIGATION = ("#6e6eaa", "#65658b") #
    BG_CARD = ("#ccccff", "#31313a") #
    BG_SPLASH = "#e0e0ff" #
    BG_ALT_TREEVIEW = ("#ccccff", "#afafd5") #

    # 基础背景类
    BG_MAIN_DARK = "#1a1a1a"
    BG_PANEL = "#2b2b2b"
    BG_NAV_DARK = "#212121"
    BG_HOVER = "#2c456c"
    BG_LIST_ITEM_HOVER = "#2f4b75"
    BG_LIST_ITEM_ACTIVE = "#3a5f96"
    BG_LIST_ITEM_ACTIVE_HOVER = "#476ea8"
    BG_BUTTON_MUTED = "#315180"
    BG_BUTTON_MUTED_HOVER = "#27436b"
    BG_BUTTON_NEUTRAL_HOVER = "#213a5c"

    # 文本颜色类
    TEXT_WHITE = "white"
    TEXT_MUTED = "gray"
    TEXT_HINT = "#d7dbe1"
    TEXT_HINT_SOFT = "#aeb7c2"
    TEXT_HINT_TUPLE = ("#444444", "#b0b0b0")
    TEXT_SUCCESS = "green"
    TEXT_WARNING = "orange"

    # 边框颜色类
    BORDER_LIST_ITEM = "#3c5678"
    BORDER_LIST_ITEM_ACTIVE = "#7ca4d7"

    # 按钮功能类
    BTN_SUCCESS = "#2563eb"
    BTN_SUCCESS_HOVER = "#1d4ed8"
    BTN_SUCCESS_ALT = "#3b82f6"
    BTN_SUCCESS_ALT_HOVER = "#2563eb"
    BTN_DANGER = "#2563eb"
    BTN_DANGER_HOVER = "#1d4ed8"
    BTN_WARNING = "#2563eb"
    BTN_WARNING_HOVER = "#1d4ed8"
    BTN_PRIMARY_ALT = "#1d4ed8"
    BTN_PRIMARY_ALT_HOVER = "#1e40af"

    # 主题色类
    PRIMARY = "#2563eb"
    PRIMARY_HOVER = "#1d4ed8"

    # 导航动画色类
    NAV_TEXT_INACTIVE_START = "#525252"
    NAV_TEXT_INACTIVE_END = "#9a9a9a"
    NAV_TEXT_ACTIVE_START = "#8db7d8"
    NAV_TEXT_ACTIVE_END = "#ffffff"

class ScreenName:
    """HRS V2.2 全局视图路由名称"""
    SCRAPER = "scraper"
    SCRAPER_TITLE = "史料下载"
    OCR = "ocr"
    OCR_TITLE = "史料校对"
    TRANSLATION = "translation"
    TRANSLATION_TITLE = "史料翻译"
    ANALYSIS = "analysis"
    ANALYSIS_TITLE = "史料分析"
    SETTING = "setting"
    SETTING_TITLE = "系统与环境设置"

# 注册所有有效的屏幕路由
LIST_SCREEN = [
    ScreenName.SCRAPER,
    ScreenName.OCR,
    ScreenName.TRANSLATION,
    ScreenName.ANALYSIS,
    ScreenName.SETTING,
]