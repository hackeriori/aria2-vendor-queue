# aria2-vendor-queue

为 aria2 提供按 **网盘厂商（Alist 挂载点）** 划分的独立下载队列。

## 解决什么问题

Alist 挂载了多个网盘（夸克、百度网盘、迅雷云盘、阿里云、天翼云盘），各厂商独立限速：

- aria2 全局"最大同时下载数 = 1"：同一时刻只有一个厂商在下载，其他厂商的限速额度被浪费。
- 调大到 2：aria2 的全局队列不区分任务来自哪个厂商，可能同时下载两个夸克任务，速度被厂商限速瓜分（每个 ~50KB/s），也可能出现一夸克一百度（速度叠加）——完全看运气。
- aria2 **没有**"标签/分类 + 按分类限流"的原生能力（实测 1.36.0 `tellStatus` 也读不回任务级 `header` 选项，无法把它当分类元数据用），所以无论怎么调 aria2 或 AriaNg 都解决不了。

**本调度器**利用一个天然事实：AriaNg 经 Alist 添加的任务，URL 形如

```
http://<你的 alist 地址>/d/<挂载点名>/<文件路径>?sign=...
```

`/d/` 后第一段就是 Alist 挂载点名，即厂商标识。于是可以：

1. 按挂载名把任务自动分类，每个分类独立 FIFO 队列（按添加顺序）；
2. 每个分类限制并发数（默认 1）：同一厂商永远只下载一个任务，下完当前这一集才轮到下一集；
3. 不同厂商互不干扰、真正并行——总下载速度 ≈ 各厂商限速之和；
4. 分类规则/并发数可配置（比如某个厂商不限速，就把它的 `maxConcurrent` 调大）。

**无需修改 AriaNg 或 aria2 一行代码**，AriaNg 里正常"新建任务"即可，分类全自动。

## 工作机制

```
                         ┌────────────────────────────┐
   AriaNg 添加任务        │       aria2 (全局上限=6)     │
   (URL 含挂载名)  ─────► │                            │
                         │  active: 06.mkv(夸克)       │
        每轮轮询 RPC       │  …                         │
   ┌──────────────────┐  └────────────▲───────────────┘
   │  aria2-vendor-queue│   pause超发的 │ unpause轮到的
   │  (本调度器, 3s/轮)  │──────────────┘
   └──────────────────┘
        │ 分类: URL /d/夸克/ → 夸克队列 (并发1, FIFO: 07,08,…26 已暂停排队)
        │       URL /d/百度网盘/ → 百度队列 (并发1)
        │       …
        └ 06.mkv 下载完 → 恢复 07.mkv；百度队列此时可并行下载自己的队首任务
```

- 调度器每轮只做一件事：让每个分类"正在下载 + 待启动"的任务数**恰好等于**该分类的并发上限；
  - 超出的任务（无论正在下载还是还没开始的等待任务）→ `aria2.pause` 暂停排队，**保留已下载进度**；
  - 有空位时（上一个任务完成/被你手动暂停）→ 按添加顺序 `aria2.unpause` 该分类队列里最早的任务；
  - 不超不欠时 → 一个动作都不做（稳态零开销）。
- 由于把"待启动的等待任务"也计入占用，启动后一轮内就能收敛，不会出现"aria2 先把等待任务全启动、调度器再一个个暂停"的抖动。
- 启动时若 aria2 全局"最大同时下载数"小于各分类并发之和，自动调大（可在配置中关闭）。

## 快速开始

需要 Python 3.7+，无第三方依赖。

```bash
# 1. 准备配置（参考 config.example.json，填入你的 RPC 地址和密钥）
cp config.example.json config.json

# 2. 验证：只读模式跑一轮，看分类是否正确，不会动你的任务
python3 aria2_vendor_queue.py --dry-run --once --verbose

# 3. 正式运行
python3 aria2_vendor_queue.py --verbose
```

## 配置说明（config.json）

| 字段 | 说明 |
|---|---|
| `rpc` | aria2 JSON-RPC 地址，如 `https://aria2.xxx.com/jsonrpc` |
| `secret` | aria2 RPC 密钥（即 aria2 的 `--rpc-secret`；AriaNg 设置里存的是 base64，需用解码后的原文） |
| `secretEnv` | `secret` 为空时从此环境变量读取，默认 `ARIA2_RPC_SECRET` |
| `pollIntervalSec` | 轮询间隔，默认 3 秒（越小响应越快，开销可忽略） |
| `logFile` | 日志文件路径（相对配置目录），默认 `logs/vendor-queue.log`；日志同时输出到 stdout |
| `logMaxBytes` | 单个日志文件最大字节数，达到后自动轮转（备份带时间戳），默认 10M（10485760） |
| `logRetentionDays` | 历史日志保留天数，启动及每次轮转时自动清理超期备份，默认 30 天 |
| `autoAdjustGlobalLimit` | 启动时自动把 aria2 全局最大下载数调到"各分类并发之和+默认值"，默认 true |
| `defaultCategory` / `defaultMaxConcurrent` | 不匹配任何规则的下载（直链、磁力、BT 等）归入默认分类，默认并发 1 |
| `categories[].name` | 分类名（仅用于日志） |
| `categories[].match` | URL 解码后路径的子串匹配规则（写 Alist 挂载名即可，支持多条） |
| `categories[].maxConcurrent` | 该分类并发上限。**不限速的厂商可调大** |

**注意**：挂载名改了 / 新增挂载时，同步更新 `match` 规则即可（也可以为同一个厂商写多条匹配串）。

## 配置与密钥安全

- `config.json`、`state.json`、`.deploy/` 已写入 `.gitignore`，不会被提交；
- 推荐用环境变量 `ARIA2_RPC_SECRET` 传入密钥，配置文件里 `secret` 留空即可；
- 建议 aria2 的 RPC 端口只在内网暴露，不要直接映射到公网。

## 部署

推荐跑在 aria2 所在服务器上（docker-compose 或 systemd 二选一）：

**docker-compose 追加服务：**

```yaml
  vendor-queue:
    image: python:3.12-alpine
    restart: unless-stopped
    volumes:
      - ./aria2-vendor-queue:/app
    working_dir: /app
    command: python aria2_vendor_queue.py
    # 若 rpc 走内网地址，写 http://aria2:6800/jsonrpc 即可
```

**systemd 服务：**

```ini
# /etc/systemd/system/aria2-vendor-queue.service
[Unit]
Description=aria2 vendor queue scheduler
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/aria2-vendor-queue/aria2_vendor_queue.py
WorkingDirectory=/opt/aria2-vendor-queue
Restart=always
RestartSec=5
Environment=ARIA2_RPC_SECRET=你的密钥

[Install]
WantedBy=multi-user.target
```

## 行为与注意事项

- **队列顺序** = 每个分类内的添加顺序（FIFO）。整批粘贴多行链接时按粘贴行序排队。
- **AriaNg 里排队任务显示为"已暂停"**（在"已完成 / 已停止"列表中）：这是 aria2 状态机的限制——排队等待只有 active/waiting/paused 三态，调度器用 paused 表示"分类队列排队中"。列表顺序不代表实际下载顺序，实际顺序以调度器日志为准。
- **手动暂停的任务永远不会被自动恢复**（调度器只恢复自己暂停的任务）。
- **想插队**：在 AriaNg 暂停当前正在下载的该厂商任务即可——它会让位，同分类队列自动补位，之后可随时手动恢复它。
- **任务完成后的接续延迟** ≈ 一个轮询间隔（默认 3 秒）：调度器是"持续对齐状态"，不是等完成事件；任务完成后最多 3 秒内该分类的下一集就会开始。

## 离线仿真测试

`sim_test.py` 用一个模拟 aria2 RPC（复刻真实状态机：pause 释放槽位后自动启动下一个等待任务）跑完整场景，不连接真实 aria2：

```bash
python3 sim_test.py
```

覆盖 6 个场景：21 个同厂商任务一轮收敛为"1 跑 + 20 停"、稳态无抖动、完成后按顺序接续、跨厂商并行、同厂商新任务立即排队、完成互不干扰。
- **aria2 重启**：gid 会变、state.json 里的序号作废，调度器按当前队列里的排列顺序重新近似排队；正在排队的任务不受影响。
- **全局上限会被调大**：启动时自动把 aria2 `max-concurrent-downloads` 调为"各分类并发之和 + 默认分类并发"。总并发仍由各分类上限约束，不会被打爆。
- 请勿同时运行多个实例（它们会通过 state.json 各自为政）。

## 已知限制

- 分类完全依赖 URL 中的挂载名规则（这也是它的优点：全自动、无需手动标注）。aria2 1.36.0 无法通过 RPC 读回任务级 `header` 选项（已实测），所以"手动指定分类"暂无可靠的传递通道。
- 若把同一厂商挂载成多个挂载点（如"夸克-1""夸克-2"），需要为它们写指向同一分类名的多条 match 规则。

## 可选的后续增强（AriaNg 二开）

如果想在 AriaNg 界面上更直观，可以在本项目（AriaNg 源码）基础上做：

1. 新建任务页对粘贴的链接自动识别挂载名并显示"分类"徽章（纯展示，无需后端）；
2. 任务列表为每个任务按 URL 规则显示厂商徽章；
3. 若需要"手动分类"，给本调度器加一个 `POST /classify` 小接口，AriaNg 添加任务成功后把 `gid + 分类` 报送给它。

这三项都不影响核心功能，属于锦上添花。
