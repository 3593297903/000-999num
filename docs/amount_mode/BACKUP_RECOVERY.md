# 逐批备份与独立恢复

每个完成批次自动触发自己的自包含包，备份状态独立显示。生成正确而备份失败时，号码结果保留，历史和提示显示备份失败；重试备份不会重新生成号码。同批同指纹的有效包可以复用，覆盖会话列表保留原值，不把后来的复核说成已备份。

包位于`备份根\batches\batch_id\backup_id`，包含原可信输入锚点、必要历史记录、本批固定五文件及已提交核查会话。最后写入`BACKUP_COMPLETE.json`；恢复仍检查整份清单、身份、原指纹和每个文件，不能仅看完成标记。

```powershell
.\AmountTool.exe backup --data-dir E:\number\data_amount --batch-id 实际批次编号 --destination F:\AmountBackups --result-json E:\number\backup.json
.\AmountTool.exe restore --package F:\AmountBackups\batches\实际批次编号\实际备份编号 --data-dir E:\number\restored_amount --result-json E:\number\restore.json
.\AmountTool.exe restore-many --package F:\完整包A F:\完整包B --data-dir E:\number\restored_amount_all --result-json E:\number\restore_all.json
.\AmountTool.exe recover --data-dir E:\number\data_amount --batch-id 实际中断批次编号 --result-json E:\number\recovery.json
```

恢复目标必须全新或明确为空。复制阶段不暴露有效完成；只对目标副本重新执行完整15次，通过后发布。原目录不可用也必须能恢复；同身份不同原指纹、坏包、未来版本、缺文件均拒绝。批量结果逐批列出，部分失败不会返回全部完成。

普通导出不携带本地可信输入锚点，独立复核只能标为`imported_declaration_only`，不能冒充原生恢复包，也不能覆盖同batch_id已有的本地可信登记。

```powershell
.\AmountTool.exe export --data-dir E:\number\data_amount --batch-id 实际批次编号 --destination F:\AmountExports --result-json E:\number\export.json
.\AmountTool.exe snapshot --data-dir E:\number\data_amount --backup-dir F:\AmountBackups --result-json E:\number\index_snapshot.json
```

导出先复制到目标卷的独立partial目录，按原hash读回，再发布最终目录。失败partial是未完成证据，不能作为正式输出。

全局索引快照通过SQLite backup API另行生成，保留A/B两个已验证槽位。索引快照只备份历史登记，不代替完整号码包；号码包可独立重建登记并新15次恢复。坏库不会被程序删除并重建空历史。

默认备份与数据在同盘，只能帮助处理文件损坏、误操作等，不能抵御整盘损坏。请另将自包含包保存到独立磁盘或离线介质。程序不上传号码或金额。
