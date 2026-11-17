# 版本、迁移与回滚

首版没有真实上一版AmountTool。已经发布的旧NumberTool属于独立模式，继续使用其原程序和原数据；不迁移它的数据库。所有“1000→1001”迁移实验均是明确未发布的QA测试库，不能称为真实前版升级。

当前支持schema2、set_amount_small_v1、对应rules/partition/shuffle/format版本、checker amount_abc_1.1.0、备份1、历史库1001/AMTL。未知协议或索引版本停止写入，不猜测金额含义，也不重写原始结果和旧报告。依赖锁不自动升级。

```powershell
.\AmountTool.exe version-info --data-dir E:\number\data_amount --result-json E:\number\version.json
.\AmountTool.exe migrate-index --data-dir E:\number\qa_migration_fixture --backup-dir E:\number\qa_migration_backups --result-json E:\number\migration.json
.\AmountTool.exe rollback-check --data-dir E:\number\data_amount --snapshot-file F:\AmountBackups\catalog\snapshots\实际索引快照.sqlite --result-json E:\number\rollback_check.json
```

`migrate-index`只接受当前1001或精确识别的未发布1000夹具。迁移前先通过SQLite backup保存并验证原索引，再事务迁移；异常回滚，保留原库。不把任意旧库标成1000后强行尝试。

升级时先为每批确认自包含包，再做索引快照；候选程序放全新目录验证，不覆盖正在运行的exe。程序目录与数据目录分离，目录切换不删除数据。

回滚前先确认升级后新增的批次和包都保留。`rollback-check`发现旧快照缺少当前批次时拒绝破坏性覆盖；这个命令不会替换数据库。可继续使用兼容程序读取现有索引，或将全部可信包（包括升级后新增批次）恢复到新根，每批重新15次，再切换程序/数据根。不能用旧索引覆盖新库使新增历史消失。

本轮实际exe目录切换、独立包恢复及回滚演练的完成状态和路径见最终验收报告；源码夹具结果不等于实际成品升级通过。未来如有真实前版发布，每次升级必须使用该真实版本生成的QA批次回归。
