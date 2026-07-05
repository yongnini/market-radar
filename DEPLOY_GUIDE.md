# Market Radar 部署指南

这份文件夹就是可以推到 GitHub / Vercel 的版本。

## 这次修复了什么

1. 修复 `/api/radar` 404 的核心问题：Vercel Python Serverless Function 需要 `class handler(BaseHTTPRequestHandler)`，不能使用普通 `def handler(request)` 返回字典。
2. 简化 `vercel.json`：去掉容易干扰自动识别的 `builds` 和 `routes`，保留函数运行时间配置。
3. 优化超时风险：Google Trends、NewsAPI、Reddit 改为并行请求；外部接口超时缩短；Google Trends 有时间预算，失败时会使用已有模拟数据降级。
4. 移除代码里的 NewsAPI 明文 Key：改为在 Vercel 后台配置 `NEWS_API_KEY` 环境变量。

## 需要上传到 GitHub 的文件

只上传这个文件夹里的内容：

```text
market-radar-deploy/
├── api/
│   └── radar.py
├── index.html
├── requirements.txt
├── vercel.json
└── DEPLOY_GUIDE.md
```

不要上传外层的 `server.py`、`server_index.html`、`requirements_original.txt`。它们是本地 FastAPI 旧版本，不是 Vercel 部署版本。

## Vercel 设置

1. 打开 Vercel 项目 `market-radar`。
2. 进入 `Settings`。
3. 进入 `Environment Variables`。
4. 新增变量：
   - Name: `NEWS_API_KEY`
   - Value: 你的 NewsAPI Key
   - Environments: Production、Preview、Development 都勾选
5. 如果要启用稳定 Reddit 数据，再新增：
   - Name: `REDDIT_CLIENT_ID`
   - Value: Reddit app 的 client id
   - Environments: Production、Preview、Development 都勾选
6. 再新增：
   - Name: `REDDIT_CLIENT_SECRET`
   - Value: Reddit app 的 client secret
   - Environments: Production、Preview、Development 都勾选
7. 保存后，回到 `Deployments`。
8. 点最新一次部署右侧的三个点，选择 `Redeploy`。
9. Redeploy 时如果出现选项，选择使用最新源码重新部署。

## 数据真实性说明

页面会显示每个数据源的可信状态：

- `Live data`：来自真实外部 API。
- `Partial live data`：拿到部分真实数据，但不是完整响应。
- `Fallback estimate`：外部服务不可用时生成的演示级估算数据。
- `Unavailable`：该数据源当前不可用，页面不会把它伪装成真实数据。

Google Trends 使用 `pytrends`，不是 Google 官方 API，所以偶尔失败是正常现象。失败时趋势模块会显示 fallback estimate。

NewsAPI 需要 `NEWS_API_KEY`。没有配置时新闻模块会显示不可用。

Reddit 优先使用 OAuth。没有 `REDDIT_CLIENT_ID` 和 `REDDIT_CLIENT_SECRET` 时会尝试公开 JSON 接口；如果 Reddit 返回 403，页面会显示 Reddit unavailable。

## GitHub Actions

仓库包含 `.github/workflows/market-radar-smoke.yml`。每次推送到 `main` 后会自动：

1. 安装 Python 依赖。
2. 检查 `api/radar.py` 语法。
3. 调用本地 response builder，确认返回结构。
4. 重试检查线上 `/api/radar` 是否返回可解析 JSON。

## 部署后检查

部署完成后，先访问：

```text
https://你的域名.vercel.app/api/radar?keyword=portable+blender
```

如果正常，会看到一大段 JSON，开头类似：

```json
{
  "keyword": "portable blender",
  "generated_at": "...",
  "markets": [...]
}
```

然后再打开首页：

```text
https://你的域名.vercel.app/
```

输入 `portable blender` 测试 Dashboard。

## 如果仍然 404

重点检查 GitHub 里的文件路径必须是：

```text
api/radar.py
```

不是：

```text
api.radar.py
```

也不是：

```text
market-radar-deploy/api/radar.py
```

如果你的 GitHub 仓库根目录已经是部署目录，就应该直接看到 `api` 文件夹、`index.html`、`vercel.json`。

## 如果接口返回 JSON 但新闻为空

检查 Vercel 的 `NEWS_API_KEY` 环境变量是否保存成功。保存后一定要重新部署一次。

## 如果 Google Trends 不稳定

这是正常现象。`pytrends` 不是 Google 官方 API，偶尔会被限流或请求失败。当前版本会自动用模拟趋势数据降级，保证作品集页面不会坏掉。
