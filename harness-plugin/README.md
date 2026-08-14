# ds-agents-lota-data

DSH harness 插件：**ds_agents 的本地缓存只读工具**。开源部分，不含私有 Python 引擎与数据。

## 这是什么

一个 Cordis 插件（`index.js`），往 harness 的 `ctx.tools` 注册三个只读工具，直接按缓存格式规范读本地 JSON：

- `lota_matches(date)` — 某足球日比赛列表
- `lota_match(lota_id)` — 单场全貌（基础信息/比分/赔率/段落/预测/订单计数）
- `lota_sections(lota_id, slugs)` — 按 slug 取 prompt 段落

**只读、不触网、不含密钥**。数据从哪来（Lota API / 自建源）由用户按 [`docs/fetcher_protocol.md`](docs/fetcher_protocol.md) 的 Fetcher 协议自行提供。

## 目录

```
harness-plugin/
├─ index.js                # 插件本体（JS）
├─ cordis.yml              # 挂载清单（YAML）
├─ package.json
├─ SKILL.md                # 说明书（给模型的提示词）
└─ docs/
   ├─ cache_format_spec.md # 缓存文件格式规范 v1（盘上格式）
   └─ fetcher_protocol.md  # 数据获取层（Fetcher）接入协议 v1（网络侧）
```

## 挂载

把 `cordis.yml` 里的插件行并入你的 profile / agent preset：

```yaml
- id: lota-data
  name: 'ds-agents-lota-data'
  config:
    cacheDir: ./python-engine/data
```

## 缓存格式

详见 [`docs/cache_format_spec.md`](docs/cache_format_spec.md)。要点：
`matches/<date>.json`、`features/<id>.json`（三种历史形状归一化）、`tags/<id>.json`、`predicts/<id>.json`、`orders/<id>.json`。

## 数据专有部分

`lota_match` 的赔率提取（`odds.js`）依赖作者私有数据源的 compact-fet 文本排版，**用户自定义数据源时需修改/替换 `odds.js`**（详见文件头部注释与 `SKILL.md`）。

## 数据获取层接入（私有）

分析/下单/结算/因子全部在本插件内（纯 JS）。唯一私有的是**数据获取层**（抓 Lota → 写缓存）：
- 缓存格式见 [`docs/cache_format_spec.md`](docs/cache_format_spec.md)
- 数据怎么进缓存、私有 `lota_fetcher.js` 放哪怎么用，见 [`docs/fetcher_protocol.md`](docs/fetcher_protocol.md)

拿到私有 `lota_fetcher.js` 后，放到 `cacheDir` 父目录，跑 `refresh-range` + `prefetch` 写缓存即可。

## License

MIT
