"""In-app help. The text lives in this module, so the EXE build picks it up."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout, QWidget

from xcp_pcan.ui.window_util import configure_tool_window

HELP_HTML = """
<h2>CANkit</h2>
<p>Windows 上的 CAN / XCP 工具集：经 <b>PEAK PCAN</b> 或 <b>Vector XL</b>
做总线观测、UDS 诊断，以及基于 A2L / ELF 的在线测量与标定。
工程文件为 <b>.xcpp</b>，退出时自动保存；下次启动会恢复连接参数、观测列表和符号名单。</p>

<h3>1. 工具栏</h3>
<ul>
<li><b>Project</b>：Open / Save / Save as。</li>
<li><b>Data Editor</b>：Load A2L/ELF、Resolve addresses from ELF、Update A2L from ELF、Create A2L from ELF。</li>
<li><b>Trace Viewer</b>：离线日志与在线 Monitor / 发报文。</li>
<li><b>Diagnostic Console</b>：UDS over ISO-TP（会话、DID、安全访问、例程、刷写模板等）。</li>
</ul>

<h3>2. 连接（主窗口）</h3>
<ul>
<li><b>Hardware</b>：PCAN 或 Vector。Vector 用 ↻ 刷新设备；未检测到时显示 Not detected，通道回退为 Channel 0–3。</li>
<li><b>Nom bitrate / CAN FD / Data</b>：须与总线一致。FD 总线上开 Classic 只会看到错误帧。</li>
<li><b>Master TX ID / Slave RX ID</b>：仅用于 XCP，始终手工填写，不会被 A2L 覆盖。</li>
<li><b>Connect</b> 只打开 CAN 总线（即可用 Trace Monitor、Transmit、Diagnostic Console）。
<b>Start poll / Start DAQ</b> 时才做 XCP 握手。同一通道上不要再开第二个 XCP 主站。</li>
<li>Trace / Diag 窗口里也可点 Connect；进度与提示会留在该窗口，不会抢主窗口焦点。</li>
<li><b>Advanced</b>（默认收起）：
<ul>
<li><b>Bus / timing</b>：Vector app name（默认 CANkit，需在 Vector Hardware Config 分配通道）、PCAN FD f_clock、CTO timeout。</li>
<li><b>Session resilience</b>：CTO 重试、自动重连、DAQ DTO 静默超时后重建列表。</li>
<li><b>Security &amp; Cal page</b>：Seed&amp;Key（.py 或 DLL）、Unlock resources、自动 SET_CAL_PAGE、Unlock now / SET / GET_CAL_PAGE。</li>
</ul>
</li>
</ul>

<h3>3. Data Editor 与符号区</h3>
<ul>
<li><b>Load A2L/ELF</b>：加载 MEASUREMENT / CHARACTERISTIC（或 ELF 符号）。大文件后台加载，可继续改连接参数。</li>
<li><b>Resolve addresses from ELF</b>：仅在内存里给地址仍为 0 的符号补地址，不改 A2L 文件。</li>
<li><b>Update A2L from ELF</b>：按 ELF 回写文件中的地址（含前导下划线等别名），另存新 A2L；后台运行可取消。</li>
<li><b>Create A2L from ELF</b>：由 ELF 全局符号生成 A2L。可选内置 <b>Templete_FR</b> / <b>Templete_FL</b> 或自选模板；可选 DWARF 展开结构体成员。</li>
<li>符号过滤：单关键词为子串；一次粘贴多个名字则按全名匹配。<b>Add selected</b> / <b>Add all filtered</b> 加入当前 Polling 或 DAQ 页。</li>
</ul>

<h3>4. 符号名单（Load lists）</h3>
<p>把要观测的名字放进文件夹里的 txt：一个名字一行；文件名作为分组标签（如 <b>batt.txt</b> → <b>Batt</b>）。</p>
<ul>
<li>能对上的符号进入当前 Polling / DAQ 并按标签分组；仅差前导下划线也会匹配（<code>Foo</code> ↔ <code>_Foo</code>）。</li>
<li>含中文、路径或空格的行会使整份 txt 被跳过（说明文件不会当名单）。未匹配名写在 Log 里。</li>
<li>观测树支持 <b>Fold all / Expand all</b>；选中标签后 <b>Remove</b> 可去掉该组。</li>
</ul>

<h3>5. Polling / DAQ 与标定</h3>
<ul>
<li><b>Polling</b>：设 Interval 后 <b>Start poll</b>，周期 SHORT_UPLOAD / UPLOAD。日常读 MEASUREMENT 优先用它。</li>
<li><b>DAQ</b>：设 Event、Prescaler 后 <b>Start DAQ</b>。同一会话不要与 Polling 同时跑。</li>
<li><b>Write value</b>：仅标量 <b>VALUE</b> 型 CHARACTERISTIC。MEASUREMENT / CURVE / MAP / VAL_BLK 只读。DAQ 运行中禁止写；Polling 写时会短暂停轮询。</li>
<li><b>Curves</b>：当前页变量曲线。<b>Export CSV</b>：导出缓冲采样（time_s / timestamp / name / value / unit）。</li>
</ul>

<h3>6. Trace Viewer</h3>
<ul>
<li><b>Open</b>：离线 ASC / BLF / CSV。<b>Start Monitor</b>：在已 Connect 的总线上实时收发（不需要 XCP）。</li>
<li>搜索框按 ID / 数据 / 名称 / 通道 / rx|tx|err 过滤；表头 <b>ID▾</b> / <b>Dir▾</b> 可再筛；<b>Reset filters</b> 清空。</li>
<li><b>Collapse by ID</b>：按 ID 折叠时间线。<b>Load DBC</b> 后选中帧可看信号解码；下方详情可开关。</li>
<li><b>Transmit</b>：最多两行发送；支持 FD / Ext、单发或按 Period 循环发送（需先 Connect）。</li>
</ul>

<h3>7. Diagnostic Console</h3>
<ul>
<li>在已打开的 CAN 总线上发 UDS（ISO-TP）。默认 Req/Resp 示例为 0x73D / 0x7BD；可改 Ext、FD、超时与 Pad（常用 0xCC）。</li>
<li>左侧服务树：Sessions（10）、Identification / Write DID（22 / 2E）、Security Access（27）、
RoutineControl（31）、Programming（34/36/37）、ECU Reset（11）、TesterPresent（3E）、Raw。</li>
<li>点选填充参数与 on-wire Raw；改参数会同步 Raw。双击或 <b>Execute</b> 发送。右侧显示 Diagnostic Trace 与 Response 解析。</li>
<li>右键可 <b>Add DID (Read + Write)</b>、只加 22 / 2E、加 Routine；对 0x22 项可 <b>Remove DID</b>（内置项可隐藏，自定义项删除）。</li>
<li><b>TesterPresent</b>：周期性发 3E 00 保活。未开总线时会提示 Connect，完成后可继续操作。</li>
</ul>

<h3>8. 工程与日志</h3>
<ul>
<li>默认工程在用户目录 <b>.xcp_pcan\\default.xcpp</b>（连接、安全、韧性、符号路径、观测列表等）。</li>
<li>底部 <b>Log</b> 可折叠；Filter：All / App / XCP / CAN / Errors。<b>Verbose</b> 打开 CAN/XCP DEBUG。</li>
</ul>

<h3>9. 运行环境</h3>
<p>运行机需已安装 PCAN 驱动或 Vector XL Driver；使用独立 EXE 时不需要 Python。</p>
"""


class HelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Help")
        self.resize(640, 720)
        configure_tool_window(self)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(HELP_HTML)
        layout.addWidget(browser)
