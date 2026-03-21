from collections import namedtuple
from pathlib import Path

# 定义项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

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

class ScreenName:
    """HRS V2.2 全局视图路由名称"""
    SCRAPER = "scraper"
    SCRAPER_TITLE = "史料高并发抓取"
    OCR = "ocr"
    OCR_TITLE = "史料 OCR 校对"
    SETTING = "setting"
    SETTING_TITLE = "系统与环境设置"

# 注册所有有效的屏幕路由
LIST_SCREEN = [
    ScreenName.SCRAPER,
    ScreenName.OCR,
    ScreenName.SETTING,
]