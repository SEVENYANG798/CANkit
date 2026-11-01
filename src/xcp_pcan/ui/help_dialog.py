"""In-app help. The text lives in this module, so the EXE build picks it up."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout, QWidget

from xcp_pcan.ui.window_util import configure_tool_window

HELP_HTML = """
<h2>XcpVariableReader</h2>
<p>在 Windows 上通过 PEAK PCAN 或 Vector 硬件，用 XCP on CAN / CAN FD
读取和标定 ECU 变量。符号来自 A2L 或 ELF，不限于某一种 CAN 卡。
工程文件为 <b>.xcpp</b>，退出时自动保存，下次启动会恢复连接参数、观测列表和上次使用的符号名单。</p>

<h3>1. 连接</h3>
<ul>
<li><b>Hardware / Channel</b>：PCAN 选通道；Vector 会枚举设备（如 VN1630）和 CAN 通道。列表为空时显示 Not detected，通道回退为 Channel 0–3。</li>
<li><b>Master TX ID / Slave RX ID</b> 始终手工填写，不会被 A2L 覆盖。</li>
<li><b>高级配置</b>：波特率、CAN FD、会话重连，以及 Security &amp; Cal page（Seed&amp;Key、标定页）。默认收起。</li>
<li>点 <b>Connect</b> 只打开 CAN 硬件（即可用 Trace Viewer Monitor / 发报文）。点 <b>Start poll</b> / <b>Start DAQ</b> 时才做 XCP 握手。同一通道上不要再开第二个 XCP 主站。</li>
</ul>

<h3>2. 符号</h3>
<ul>
<li><b>Load A2L/ELF</b>：加载测量量和标定量。大文件在后台读，界面可以继续操作。</li>
<li><b>Resolve addresses from ELF</b>：只在内存里补上地址仍为 0 的符号，不改 A2L 文件。许多 ECU 工程 A2L（如 VIU 标定导出）里 MEASUREMENT 地址是占位 0，读值前必须先做这一步或改用已回写地址的 A2L。</li>
<li><b>Update A2L from ELF</b>：按 ELF 回写文件里的地址（含前导下划线这类名字差异），另存一份 A2L。</li>
<li><b>Create A2L from ELF</b>：用 ELF 全局符号新建一份 A2L。可选内置 <b>Templete_FR</b>（右区 0x6A6/0x400006A7）或 <b>Templete_FL</b>（左区 0x6A8/0x400006A9），也可自选模板。</li>
<li><b>Trace Viewer</b>：离线 ASC / BLF / CSV，或 <b>Start Monitor</b> 在线观测。主窗口 <b>Connect</b> 打开总线即可（不必 XCP）。表头 <b>ID▾</b> / <b>Dir▾</b> 过滤；底部 <b>Transmit</b> 可发报文。</li>
<li>过滤框：一个词是子串匹配；一次粘贴多个名字则按全名匹配。</li>
</ul>

<h3>3. 符号名单</h3>
<p>把要观测的符号按文本放好，一个名字一行，空行可以忽略。用文件夹里每个 txt 的文件名当标签，例如 <b>batt.txt</b> 显示为 <b>Batt</b>。</p>
<ul>
<li><b>Load lists</b>：选这个文件夹。能对上的符号进入当前的 Polling 或 DAQ，并按标签分组。只差前导下划线也会对上（<code>Foo</code> 与 <code>_Foo</code>）。</li>
<li>说明文件不会当名单。某一行出现中文、路径或空格时，整份 txt 会被跳过（例如 QUICKSTART.txt）。</li>
<li>没对上的名字写在下方 Log 里，红色显示，不占用符号区。</li>
<li>上次选过的文件夹会记住。下次打开工程时，观测列表会自动带上这些标签。</li>
<li><b>Fold all / Expand all</b>：一次收起或展开全部分组。</li>
</ul>

<h3>4. 观测与标定</h3>
<ul>
<li><b>Polling</b>：按间隔周期读取。先设 Interval，再 <b>Start poll</b>。日常开发优先用它读 MEASUREMENT。</li>
<li><b>DAQ</b>：按事件通道采集。设 Event、Prescaler 后 <b>Start DAQ</b>。同一会话里 Polling 和 DAQ 不要同时跑。</li>
<li><b>Write value</b>：只允许标量 <b>VALUE</b> 型 CHARACTERISTIC。CURVE / MAP / VAL_BLK 和 MEASUREMENT 只读。DAQ 运行中禁止写入；Polling 写时会短暂停轮询。</li>
<li><b>Curves</b>：看当前页变量的曲线。</li>
<li><b>Export CSV</b>：导出缓冲中的采样（time_s / timestamp / name / value / unit）。</li>
<li>选中一个标签再点 <b>Remove</b>，会去掉这一组。</li>
</ul>

<h3>5. 工程与日志</h3>
<ul>
<li><b>Project</b>：打开、保存、另存为。默认工程在用户目录下的 <b>.xcp_pcan\\default.xcpp</b>。</li>
<li>Log 可按 All / App / XCP / CAN / Errors 过滤。Verbose 会多打 CAN 与 XCP 调试信息。</li>
</ul>

<h3>6. 运行环境</h3>
<p>目标电脑不需要安装 Python，但需要已安装的 PCAN 驱动或 Vector XL Driver。
打包结果是 <b>output\\XcpVariableReader.exe</b>。</p>
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
