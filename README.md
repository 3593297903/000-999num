# 000–999 按套金额随机分组工具

Windows 本地桌面程序，使用 Python 3.13、PySide6 和 SQLite。当前主要入口是 **按套金额随机小组**，包含整批结果导出 Word。

源码仓库：[3593297903/000-999num](https://github.com/3593297903/000-999num)。仓库只保存源码、测试、固定格式样例、构建配置和使用文档。实际号码批次、金额输入记录、历史库、备份、Word、QA 收据、虚拟环境和 exe 构建产物留在运行设备本地。

## 业务规则

- 选择 **1～300 套**，每套完整包含 `000–999`，每套填写一个大于零的整数金额。
- 合并所有套的原始号码记录后随机打乱，每组随机分配 **2～5 条原始记录**，直到全部分完。
- 同组同号的金额相加；金额相同的号码放在一行，金额不同的分行；组间独立。合并后只剩一个号码也保留。
- 保留三位号码的前导零，使用 `007.128直选5元` 格式。
- 原始每套每个号码恰好一次；所有结果中，每个号码的累计金额都等于所有套的输入金额之和。
- A、B、C 三种不同方法各执行五遍，15 次实际独立进程核查全部通过后才提交正式结果。

例如三套分别填写 1、5、2 元：共 3000 条原始记录，每个号码累计 8 元，总金额 8000 元。若某组中两份 `007` 分别来自 1 元和 5 元的套，合并为 `007直选6元`。

当前上限为 300 套；500 套扩展尚未实现。本仓库不把历史的两小时运行及更大规模长期验收标为已完成。

## 在新电脑安装和启动

需要 Windows 10/11 x64、Python 3.13 x64，以及 Python launcher。首次安装依赖需要网络。

```powershell
git clone https://github.com/3593297903/000-999num.git
cd 000-999num
.\setup.cmd
.\start-amount.cmd
```

也可以从 GitHub 下载源码 ZIP，解压后依次双击 `setup.cmd` 和 `start-amount.cmd`。

`setup.cmd` 创建项目自己的 `.venv` 并安装锁定依赖。`start-amount.cmd` 优先使用已有的完整打包目录，否则使用已安装的源码环境；数据写入项目的 `data_amount`，默认备份写入旁边的 `data_amount_backups`。启动脚本显式传入本机目录，新电脑不要求存在 E 盘。

安装和启动不会带入另一台电脑的历史记录。已有设备的真实数据和备份应单独保存。

## 全部分组导出 Word

生成完成后进入“分组结果”，或从“历史”打开已完成且核查通过的批次，点击 **“导出本批全部分组为Word”**，选择保存位置和新文件名。

导出所选批次的全部组，按结果顺序排列；组间空一行，标题后紧接原有直选正文，保留号码和金额：

```text
第一组
007.128直选1元
905直选5元

第二组
156.482直选2元
640直选10元
```

保存后实际回读 Word 的全部段落，逐行比对完整原结果，并核对组数及尾组。发布前后重新检查原数据和原核查证据。取消或核对失败不发布完成文件，已有文件拒绝覆盖。生成 Word 本身不要求安装 Word 或 WPS。

详见 [Word 导出说明](docs/amount_mode/Word导出说明.md) 和 [金额模式使用说明](docs/amount_mode/USAGE.md)。

## 开发、测试与打包

精确依赖见 `requirements-dev.lock`，项目配置见 `pyproject.toml`。手动启动金额模式：

```powershell
.\.venv\Scripts\python.exe -m number_app.amount_mode gui --data-dir .\data_amount
```

Word 导出定向测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/amount_mode/test_word_export.py -q -p no:cacheprovider
```

Windows 打包：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath dist-amount-word --workpath build-amount-word packaging/amount_tool.spec
```

产物为 `dist-amount-word/AmountTool` 完整目录，其中包含 `AmountTool.exe`、`AmountVerify.exe` 和 `_internal`，需要一起保留。

测试和故障注入使用本地 `qa` 目录，不使用日常批次。部分历史探针带有开发设备路径，使用前需要指定自己的 QA 根目录；公开仓库不包含历史执行收据。重复五次核对不能消除算法自身的固定缺陷，测试通过也不代表任何环境下绝对无故障。

## 旧分组模式

仓库保留原有“套数＋组数＋百分比配额”的实现和测试，通过 `start.cmd` 启动，数据目录为 `data`，与金额模式分开。旧模式资料见 [使用说明](docs/USAGE.md)。
