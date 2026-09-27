# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检、恢复状态机和区域封控汇总。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限、版本冲突和区域封控。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。

区域封控由所有关联事件共同决定：新建事件后其 `zone_ids` 对应区域即进入封控；恢复或取消事件时，若关联区域仍有其他未结束（非 `restored`/`cancelled`）的事件，接口返回 409 `zone_events_open` 并在 `open_events` 中列出仍未结的事件，区域保持封控；只有关联事件全部结束，区域才解除封控。`GET /api/state` 新增 `zones` 字段（`zone_id`、`locked`、`open_events`），首页按区域展示封控状态和未结数量并定时刷新。
