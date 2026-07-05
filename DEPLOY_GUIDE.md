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
5. 保存后，回到 `Deployments`。
6. 点最新一次部署右侧的三个点，选择 `Redeploy`。
7. Redeploy 时如果出现选项，选择使用最新源码重新部署。

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
