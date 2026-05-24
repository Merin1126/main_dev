Download Event Code Spec
========================

Overview
--------

从当前版本起，下载任务在 ``download_events.message`` 中统一写入以下格式：

``CODE | 中文说明 | detail=...``

- ``CODE``: 机器可读错误码/中止码（便于聚合和统计）。
- ``中文说明``: 人类可读摘要。
- ``detail=...``: 可选细节（如异常文本、URL、页数比例）。

该格式由 ``core_scraper.py`` 中的 ``_event_message()`` 统一生成。


Status Lifecycle
----------------

标准事件流：

1. ``queued``（主线程入队）
2. ``downloading``（worker 接单）
3. 终态之一：

   - ``succeeded``
   - ``failed``（附带错误码 message）
   - ``aborted``（手动停止）

对应 ``documents.status``：

- 成功：``downloaded``（或 Hoover 的 ``pending_hoover`` 语义成功）
- 失败：``failed``
- 手动停止：``discovered``（保留可重试）


Event Codes
-----------

JACAR 分支
~~~~~~~~~~

- ``E_JACAR_CONTENT_LIST_MISSING``: 页面缺少 ``najContentList``。
- ``E_JACAR_CONTENT_LIST_PARSE``: ``najContentList`` JSON 解析失败。
- ``E_JACAR_CONTENT_LIST_EMPTY``: ``najContentList`` 为空。
- ``E_JACAR_REL_PATH_MISSING``: 首条记录缺少 ``path/source``。

Toyo Bunko / OPAC / IIIF 分支
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- ``E_TOYO_OPAC_RESOLVE_FAILED``: OPAC 跳板未解析到阅读器地址。
- ``E_TOYO_MANIFEST_MISSING``: 页面中未找到 IIIF manifest。
- ``E_TOYO_CANVASES_EMPTY``: manifest canvases 为空。
- ``E_TOYO_IIIF_INCOMPLETE``: IIIF 组装未完成（会带 ``detail=done/total``）。
- ``E_TOYO_IIIF_ZERO_PAGES``: IIIF 未提取到有效页。

通用分支
~~~~~~~~

- ``E_UNSUPPORTED_DOMAIN``: 未支持的来源域名（带 URL detail）。
- ``E_TASK_EXCEPTION``: worker 主流程捕获到未预期异常（带异常 detail）。

手动中止
~~~~~~~~

- ``A_MANUAL_STOP_DIRECT``: 直链下载阶段手动停止。
- ``A_MANUAL_STOP_IIIF``: IIIF 组装阶段手动停止。


GUI Display Rule
----------------

``screens/scraper_screen.py`` 已按如下规则展示消息：

- 若命中 ``CODE | 中文说明 | detail``，监控窗口会优先展示 ``CODE 中文说明``，
  并在有 detail 时追加 ``(detail=...)``。
- 否则按原始 message 兜底显示。


Operational Guidance
--------------------

- 若要统计失败分布，建议按 ``message`` 的 ``CODE`` 前缀做分组。
- 若出现高频 ``E_TASK_EXCEPTION``，优先查看 ``detail`` 并结合
  ``Scraper_Logs/scraper_run_*.log`` 排查。
- 若出现 ``aborted``，属用户中止，不计入系统性失败。
