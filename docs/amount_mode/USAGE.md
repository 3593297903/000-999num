# 按套金额随机小组 1.1 使用说明

本程序与旧NumberTool独立。默认数据为`E:\number\data_amount`，逐批备份为`E:\number\data_amount_backups`，不会转换旧号码历史。

保留程序完整文件夹，双击AmountTool.exe打开桌面界面；AmountVerify.exe双击打开复核页面。不要单独移动exe而丢掉`_internal`。

填写1～300套，每套填写1～100位的ASCII正整数金额。每套完整包含000～999。未填完时显示有效小计，不能开始。金额大于浮点精确范围也按整数计算，不截断派生金额。

点击“开始生成并核查15次”会先提交正在编辑的最后一格，再冻结本批。例：三套1、5、2元，合计8元，3000条，全部号码8000元。每个号码在全部结果中累计8元。

每组2～5条按合并前原始记录计数。同号金额相加，再将同金额号码合成一行；只剩一个号码也合法。例如两条007分别为1元和5元，输出`007直选6元`，仍保留两条来源记录。组间不合并。

只有实际五个固定文件经过A/B/C各五遍、全部15个新进程通过并安全提交后，才开放正式结果。重复五遍不能消除算法本身的固定缺陷，程序不承诺任何硬件情况下绝对无故障。

“分组结果”按组号取用，复制只复制本组正文；“逐号统计”列出全部000～999。“历史”每页30批，显示上次核查状态；打开时按原可信指纹重新检查。历史查看、复制、导出均不重新随机。

取消在进入COMMITTING之前生效；进入提交后等待安全提交完成。强退后先查看历史中的未完成批次：完整可信快照必须重新15次，未形成可信快照的现场保留，另建新批。损坏结果暂停取用，不能重新计算指纹冒充修复。

命令行示例（从完整程序目录运行）：

```powershell
.\AmountTool.exe gui --data-dir E:\number\data_amount
.\AmountTool.exe generate --config E:\number\input.json --data-dir E:\number\data_amount --result-json E:\number\generation.json
.\AmountVerify.exe --batch-dir E:\number\data_amount\batches\实际批次编号 --data-dir E:\number\data_amount --result-json E:\number\reverify.json
.\AmountTool.exe history --data-dir E:\number\data_amount --offset 0 --limit 30
.\AmountTool.exe group --data-dir E:\number\data_amount --batch-id 实际批次编号 --group 1 --result-json E:\number\group.json
```

输入配置：`{"set_count":3,"amounts":["1","5","2"]}`。金额必须是规范字符串，不能写小数、科学计数法、空白、布尔或全角数字。实际收据中取batch_id/batch_dir，不把上述“实际批次编号”当真实路径执行。

退出码0成功，2输入/路径拒绝，3数据或审查失败，4环境/I/O/内部错误，5取消或中断。结果JSON与诊断日志分开；如果批次已完成而收据写入失败，批次仍留在历史，不要盲目重复生成。窗口版CLI会附着调用它的控制台，支持Ctrl+C/Ctrl+Break请求取消。

数据、备份、导出及结果JSON等路径不能位于任何旧、新或外部批次内部。路径拒绝不会创建目标。缓存最多3批/128MiB，诊断日志最多10份×10MiB；原始批次、核查报告和备份包不自动清理。新任务会检查内存及数据、备份卷空间，不足时明确拒绝。

开发及验收数据应使用项目下独立的`qa`目录，不能作为日常数据目录。实际运行收据保留在各设备本地；源码发布不表示全部长期验收已经完成。
