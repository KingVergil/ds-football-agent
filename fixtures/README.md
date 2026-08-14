# 测试数据（fixtures）

`testdata-14d.tar.gz`：过去 14 个足球日（2026-08-01 ~ 2026-08-14）的**比赛列表 + compact-fet 特征缓存**，供开源用户开箱即玩。

## 内容

解压后：

```
testdata-14d/
├── matches/   14 天比赛列表（matches/<足球日>.json）
└── features/  672 场 compact-fet（features/<lota_id>.json）
```

- 共 1519 个 lota_id（14 天比赛），其中 672 场有 compact-fet 特征数据。
- **不含** tags（段落）、roles（7 狗角色）、predicts/orders——这些要么可由 features 即时切分，要么属私有运行时数据。

## 用法

```bash
tar -xzf testdata-14d.tar.gz
```

然后把插件的 `cacheDir` 指向解压目录（或把 `matches/`、`features/` 拷进你的缓存根目录）：

```yaml
- id: lota-data
  name: 'ds-agents-lota-data'
  config:
    cacheDir: ./testdata-14d
```

之后在 `ds-agents` 预设下开 session 即可分析。

## 说明

- `tags/<id>.json`（段落）未打包，因为可由 `features/<id>.json` 里的 `compact_fet` 文本按 [`cache_format_spec.md`](../harness-plugin/docs/cache_format_spec.md) 的段落规则即时切分。
- 要真实/最新数据，用私有 `lota_fetcher.js` 抓取（见 [`fetcher_protocol.md`](../harness-plugin/docs/fetcher_protocol.md)）。
