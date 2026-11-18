#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aria2-vendor-queue —— 按网盘厂商（Alist 挂载点）为 aria2 维护相互独立的下载队列

背景:
    aria2 只有全局的 max-concurrent-downloads，没有"按分类限制并发"的能力。
    当 Alist 挂载了多个网盘（夸克、百度网盘……）时，不同厂商各有各的限速，
    同一厂商同时下载多个任务只会互相瓜分限速带宽，而不同厂商并行下载速度可以叠加。

原理:
    AriaNg 添加任务时，aria2 收到的 URL 形如:
        http://<你的 alist 地址>/d/<挂载点名>/<文件路径>?sign=...
    `/d/` 后的第一段就是 Alist 的挂载点名（夸克 / 百度网盘 / 迅雷云盘 / 阿里云……），
    天然标识了网盘厂商。本调度器轮询 aria2 RPC:
      1. 按 URL 中的挂载名把每个任务归入一个"分类"；
      2. 每个分类有独立的并发上限（默认 1）和独立的 FIFO 队列（按添加顺序）；
      3. 超出分类并发上限的任务（含尚未开始的等待任务）用 aria2.pause 暂停排队，保留进度；
         分类有空位时（上一个任务完成或被你手动暂停）按队列顺序 aria2.unpause 恢复最早的一个；
         不超不欠时不做任何动作；
      4. 分类之间互不干扰 —— 夸克下载第一集的同时，百度网盘也可以下载自己的第一集。

注意:
    - 需要把 aria2 全局的 max-concurrent-downloads 调大到 >= 各分类并发上限之和，
      脚本启动时会在 autoAdjustGlobalLimit=true 时自动完成。
    - 调度器管理的排队任务在 AriaNg 中会显示为"已暂停"（位于 已完成/已停止 列表），
      这是 aria2 的状态机制决定的，属于正常现象。
    - 手动暂停（非本脚本暂停）的任务永远不会被本脚本自动恢复。
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TAG = "[vendor-queue]"
_logger = logging.getLogger("vendor-queue")

# 默认日志策略：单文件最大 10M，备份最多保留 30 天
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_RETENTION_DAYS = 30


class SizedDatedRotatingHandler(logging.handlers.BaseRotatingHandler):
    """按大小轮转、按保留期清理的日志文件处理器。

    - 当前日志文件达到 maxBytes 后轮转，备份文件名带时间戳（不覆盖旧备份）；
    - 每次轮转及程序启动时，删除 mtime 早于 retentionDays 天的备份文件。
    """

    def __init__(self, filename, max_bytes=DEFAULT_LOG_MAX_BYTES,
                 retention_days=DEFAULT_LOG_RETENTION_DAYS, encoding="utf-8"):
        super().__init__(filename, "a", encoding=encoding, delay=True)
        self.max_bytes = max(1024, int(max_bytes))
        self.retention_days = max(0, int(retention_days))

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        msg = "%s\n" % self.format(record)
        try:
            size = len(msg.encode(self.encoding or "utf-8"))
        except LookupError:
            size = len(msg.encode("utf-8"))
        try:  # 文本流 tell() 返回不透明值，用 fstat 取文件真实字节数
            cur = os.fstat(self.stream.fileno()).st_size
        except (OSError, ValueError, AttributeError):
            cur = self.stream.tell()
        return cur + size >= self.max_bytes

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        if os.path.exists(self.baseFilename):
            ts = time.strftime("%Y%m%d-%H%M%S")
            dfn = "%s.%s" % (self.baseFilename, ts)
            n = 1
            while os.path.exists(dfn):  # 同一秒内多次轮转避免覆盖
                dfn = "%s.%s.%d" % (self.baseFilename, ts, n)
                n += 1
            try:
                os.replace(self.baseFilename, dfn)
            except OSError:
                pass
        self.purge_old_backups()
        if not self.delay:
            self.stream = self._open()

    def purge_old_backups(self):
        """删除超过保留期的历史日志（按文件修改时间）。"""
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        d = os.path.dirname(self.baseFilename) or "."
        prefix = os.path.basename(self.baseFilename) + "."
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(d, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def setup_logging(cfg, cfg_dir):
    """按配置初始化日志：同时输出到 stdout（docker logs）和轮转日志文件。

    两个子模块（vendor-queue / quark-window）共用同一组 handler，
    日志前缀用 logger 名区分，避免同一文件被两个文件句柄各写一份、轮转时打架。
    """
    raw = cfg.get("logFile") or os.path.join("logs", "vendor-queue.log")
    log_file = raw if os.path.isabs(raw) else os.path.join(cfg_dir, raw)
    log_file = os.path.abspath(log_file)
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
    except OSError:
        pass

    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = SizedDatedRotatingHandler(
        log_file,
        max_bytes=cfg.get("logMaxBytes", DEFAULT_LOG_MAX_BYTES),
        retention_days=cfg.get("logRetentionDays", DEFAULT_LOG_RETENTION_DAYS),
    )
    fh.setFormatter(fmt)
    fh.purge_old_backups()  # 启动时也清理一次过期备份

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    for name in ("vendor-queue", "quark-window"):
        lgr = logging.getLogger(name)
        lgr.setLevel(logging.INFO)
        lgr.handlers.clear()
        lgr.propagate = False
        lgr.addHandler(fh)
        lgr.addHandler(sh)
    return log_file


def log(msg, verbose=False):
    _logger.info(msg)


def _task_name(task):
    """任务对应的文件名（日志用），取不到就退回 gid 前缀。"""
    for f in (task.get("files") or []):
        p = f.get("path") or ""
        if p:
            return os.path.basename(p)
    return (task.get("gid") or "")[:8]


class Aria2Rpc:
    def __init__(self, url, secret, timeout=15):
        self.url = url.rstrip("/")
        self.secret = (secret or "").strip()
        self.timeout = timeout
        self._id = 0
        # 部分反代/WAF 会拦截非浏览器 UA 的请求，这里模拟常规客户端
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) aria2-vendor-queue/1.0",
        }
        origin = urllib.parse.urlsplit(self.url)
        if origin.scheme in ("http", "https"):
            self.headers["Origin"] = "%s://%s" % (origin.scheme, origin.netloc)

    def call(self, method, *params):
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": ["token:" + self.secret] + list(params),
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # aria2 对部分 RPC 错误返回 4xx 状态码（如对已暂停任务 pause 报
            # 400 "cannot be paused now"），body 内仍是标准 JSON-RPC error
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise
        if "error" in body:
            raise RuntimeError("%s 失败: %s %s" % (method, body["error"].get("code"),
                                                  body["error"].get("message")))
        return body["result"]


class Scheduler:
    def __init__(self, cfg, cfg_dir, dry_run, verbose):
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        self.rpc = None
        self.interval = float(cfg.get("pollIntervalSec", 3))

        # 分类匹配规则: [(name, [substring, ...])]
        self.matchers = []
        for cat in cfg.get("categories", []):
            pats = cat.get("match", [])
            if isinstance(pats, str):
                pats = [pats]
            self.matchers.append((cat["name"], [urllib.parse.unquote(p) for p in pats]))
        self.quotas = {cat["name"]: int(cat.get("maxConcurrent", 1))
                       for cat in cfg.get("categories", [])}
        self.default_cat = cfg.get("defaultCategory", "默认")
        self.default_quota = int(cfg.get("defaultMaxConcurrent", 1))

        # 插队规则：URL 路径命中这些子串的任务，排队号排到"正在下载的任务之后、
        # 其余排队任务之前"。用于让某一批任务跳过同厂商的 FIFO 队列先下。
        prio = cfg.get("priorityMatch") or []
        if isinstance(prio, str):
            prio = [prio]
        self.priority_pats = [urllib.parse.unquote(p) for p in prio]

        state_file = cfg.get("stateFile") or os.path.join(cfg_dir, "state.json")
        self.state_file = os.path.abspath(state_file)
        self.state = {"tasks": {}, "nextSeq": 1}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("tasks"), dict):
                    self.state = loaded
                log("已加载状态文件: %s (%d 个任务记录)" % (self.state_file, len(self.state["tasks"])))
            except Exception as e:
                log("状态文件损坏，将重建: %s" % e)

    # ---------- 基础工具 ----------

    def quota_of(self, cat):
        return self.quotas.get(cat, self.default_quota)

    def needed_global_limit(self):
        return sum(self.quotas.values()) + self.default_quota

    @staticmethod
    def _first_uri(task):
        for f in task.get("files") or []:
            for u in f.get("uris") or []:
                uri = u.get("uri", "")
                if uri:
                    return uri
        return ""

    def _decoded_path(self, task):
        """取任务 URL 里解码后的路径（中文目录在 URL 里是百分号编码的）。"""
        uri = self._first_uri(task)
        if not uri:
            return ""
        try:
            return urllib.parse.unquote(urllib.parse.urlparse(uri).path)
        except Exception:
            return uri

    def category_of(self, task):
        path = self._decoded_path(task)
        if not path:
            return self.default_cat
        for name, pats in self.matchers:
            for p in pats:
                if p in path:
                    return name
        return self.default_cat

    def is_priority(self, task):
        """是否命中 priorityMatch（插队规则）。"""
        if not self.priority_pats:
            return False
        path = self._decoded_path(task)
        return bool(path) and any(p in path for p in self.priority_pats)

    def _save_state(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except Exception as e:
            log("保存状态文件失败: %s" % e)

    # ---------- 启动检查 ----------

    def ensure_global_limit(self, rpc):
        need = max(1, self.needed_global_limit())
        cur = int(rpc.call("aria2.getGlobalOption").get("max-concurrent-downloads", "1"))
        auto = bool(self.cfg.get("autoAdjustGlobalLimit", True))
        if cur >= need:
            log("aria2 全局最大同时下载数=%d（需 >= %d），无需调整" % (cur, need))
            return
        if not auto:
            log("警告: aria2 全局最大同时下载数=%d，需 >= %d，但 autoAdjustGlobalLimit=false 未自动调整；"
                "请在 AriaNg 中手动调大，否则各分类无法真正并行下载" % (cur, need))
            return
        if self.dry_run:
            log("[dry-run] aria2 全局最大同时下载数=%d，需要 >= %d，实际运行时将自动调整" % (cur, need))
        else:
            rpc.call("aria2.changeGlobalOption", {"max-concurrent-downloads": str(need)})
            log("已将 aria2 全局最大同时下载数 %d -> %d" % (cur, need))

    # ---------- 核心：每轮调度 ----------

    def poll(self, rpc):
        keys = ["gid", "status", "files"]
        active = rpc.call("aria2.tellActive", keys)
        # 注意: aria2(1.36) 的 tellWaiting 会把 paused 状态的任务一并返回，
        # 必须按 status 拆分，否则已暂停任务会被误计入"在跑/待启动"，
        # 导致每轮都对其重复调用 aria2.pause（报 cannot be paused now）。
        waiting_raw = rpc.call("aria2.tellWaiting", 0, 1000, keys)
        stopped = rpc.call("aria2.tellStopped", 0, 1000, keys)

        waiting = [t for t in waiting_raw if t.get("status") == "waiting"]
        paused_map = {}  # paused 任务可能出现在 tellWaiting 和 tellStopped 两处，按 gid 去重
        for t in waiting_raw + stopped:
            if t.get("status") == "paused":
                paused_map.setdefault(t["gid"], t)
        paused = list(paused_map.values())
        finished = {t["gid"] for t in stopped
                    if t.get("status") in ("complete", "error", "removed")}
        state_changed = False

        tasks = self.state["tasks"]

        # 1. 为新出现的任务按发现顺序编号（active -> waiting -> paused 由旧到新）
        new_prio = []  # 本轮新发现的插队任务，稍后重排 seq
        for t in active + waiting + paused[::-1]:
            g = t["gid"]
            if g not in tasks:
                cat = self.category_of(t)
                tasks[g] = {"seq": self.state["nextSeq"], "category": cat,
                            "pausedByUs": False}
                self.state["nextSeq"] += 1
                state_changed = True
                if self.is_priority(t):
                    new_prio.append(g)
                if self.verbose:
                    log("发现新任务 %s -> 分类[%s] seq=%d%s"
                        % (g, cat, tasks[g]["seq"], "（插队）" if g in new_prio else ""))

        # 2. 任务已被启动（active）→ 清除"由我们暂停"的标记（例如用户手动恢复过它）
        for t in active:
            e = tasks.get(t["gid"])
            if e and e["pausedByUs"]:
                e["pausedByUs"] = False
                state_changed = True

        # 3. 清理已结束或已消失的任务
        live = ({t["gid"] for t in active} | {t["gid"] for t in waiting}
                | {t["gid"] for t in paused})
        for g in [g for g in list(tasks.keys()) if g not in live or g in finished]:
            del tasks[g]
            state_changed = True

        # 3.5 插队：把新发现的优先任务排到"正在下载的任务之后、其余排队任务之前"
        #     不去动正在下载的任务（它已经下了半天，打断它更浪费），只插到它后面。
        if new_prio:
            new_prio = [g for g in new_prio if g in tasks]
            active_gids = {t["gid"] for t in active}
            # 只参考"与插队无关的"正在下载任务：aria2 全局并发若 > 分类并发，
            # 插队任务在 addUri 后会被 aria2 立刻启动，它们自己也在 active 里，
            # 若一并计入就会把基准算到自己头上，反而排到最后。
            act_seqs = [tasks[g]["seq"] for g in active_gids
                        if g in tasks and g not in new_prio]
            if new_prio and act_seqs:
                lo = max(act_seqs)
                for g, e in tasks.items():  # 其余排队任务整体后移，腾出位置
                    if g not in new_prio and g not in active_gids and e["seq"] > lo:
                        e["seq"] += len(new_prio) + 1
                for i, g in enumerate(sorted(new_prio, key=lambda x: tasks[x]["seq"])):
                    tasks[g]["seq"] = lo + 1 + i
            elif new_prio:  # 没有"别人的"任务在下：直接排到队首
                others = [e["seq"] for g, e in tasks.items() if g not in new_prio]
                base = min(others) if others else 0
                for i, g in enumerate(sorted(new_prio, key=lambda x: tasks[x]["seq"])):
                    tasks[g]["seq"] = base - len(new_prio) + i
            if new_prio:
                state_changed = True
                names = []
                for t in active + waiting + paused:
                    if t["gid"] in new_prio:
                        names.append(_task_name(t))
                log("插队任务已排到队首（当前在下的任务之后）：%s" % ", ".join(names))

        # 4. 按分类汇总
        #    running = 正在下载 + 正被 aria2 排进队列（waiting，随时会开跑）的任务
        #    这两类都必须计入分类并发占用，否则 aria2 会先把 waiting 全启动，
        #    调度器再一轮轮把它们暂停，产生"启动-暂停"抖动。
        by_cat = {}  # name -> {"running": [(seq, gid)], "ours": [(seq, gid)]}
        for t in active + waiting:
            e = tasks.get(t["gid"])
            if not e:
                continue
            cat = self.category_of(t)
            if e.get("category") != cat:
                e["category"] = cat
                state_changed = True
            by_cat.setdefault(cat, {"running": [], "ours": []})["running"].append((e["seq"], t["gid"]))
        for t in paused:
            e = tasks.get(t["gid"])
            if e and e["pausedByUs"]:
                cat = e.get("category") or self.category_of(t)
                by_cat.setdefault(cat, {"running": [], "ours": []})["ours"].append((e["seq"], t["gid"]))

        if self.verbose:
            for name in sorted(by_cat):
                b = by_cat[name]
                log("分类[%s] 并发上限=%d 在跑/待启动=%d 我们暂停排队=%d"
                    % (name, self.quota_of(name), len(b["running"]), len(b["ours"])))

        # 5. 执行调度：让每个分类"在跑/待启动"的任务数恰好等于并发上限
        for name, b in by_cat.items():
            quota = self.quota_of(name)
            b["running"].sort()
            b["ours"].sort()

            if len(b["running"]) > quota:
                # 超发：暂停"最新"的那些（保留按添加顺序最早的任务继续跑）
                victims = [g for _, g in sorted(b["running"], reverse=True)[:len(b["running"]) - quota]]
                for g in victims:
                    self._do_pause(rpc, g)
            elif len(b["running"]) < quota and b["ours"]:
                # 有空位：按队列顺序（seq 从小到大）恢复我们暂停的任务
                for _, g in b["ours"][:quota - len(b["running"])]:
                    self._do_unpause(rpc, g)

        if state_changed:
            self._save_state()

    def _do_pause(self, rpc, gid):
        e = self.state["tasks"].get(gid)
        if self.dry_run:
            log("[dry-run] 将暂停任务 %s（超出分类并发上限）" % gid)
            return
        try:
            rpc.call("aria2.pause", gid)
            if e:
                e["pausedByUs"] = True
                e["starting"] = False
            log("已暂停任务 %s（超出分类并发上限）" % gid)
        except Exception as ex:
            try:
                rpc.call("aria2.forcePause", gid)
                if e:
                    e["pausedByUs"] = True
                    e["starting"] = False
                log("已强制暂停任务 %s" % gid)
            except Exception as ex2:
                log("暂停任务 %s 失败: %s / %s" % (gid, ex, ex2))

    def _do_unpause(self, rpc, gid):
        e = self.state["tasks"].get(gid)
        if self.dry_run:
            log("[dry-run] 将恢复任务 %s（分类队列轮到它了）" % gid)
            return
        try:
            rpc.call("aria2.unpause", gid)
            if e:
                e["pausedByUs"] = False
            log("已恢复任务 %s（分类队列轮到它了）" % gid)
        except Exception as ex:
            if e:
                e["pausedByUs"] = False
            log("恢复任务 %s 失败: %s" % (gid, ex))

    # ---------- 主循环 ----------

    def run(self, rpc, once=False):
        self.rpc = rpc
        self.ensure_global_limit(rpc)
        while True:
            try:
                self.poll(rpc)
            except KeyboardInterrupt:
                log("收到退出信号，保存状态后退出")
                self._save_state()
                return
            except Exception as e:
                log("调度轮询出错（下一轮继续）: %s" % e)
            if once:
                return
            try:
                time.sleep(self.interval)
            except KeyboardInterrupt:
                log("收到退出信号，保存状态后退出")
                self._save_state()
                return


def build_window(cfg, cfg_dir, rpc, dry_run, verbose):
    """按 config.quark 构造转存窗口调度器；未启用或未配置 cookie 时返回 None。"""
    qcfg = cfg.get("quark") or {}
    if not qcfg.get("enabled"):
        return None
    wlog = logging.getLogger("quark-window")
    cookie = (qcfg.get("cookie") or "").strip()
    if not cookie:
        env_name = qcfg.get("cookieEnv", "QUARK_COOKIE")
        cookie = (os.environ.get(env_name) or "").strip()
    if not cookie:
        wlog.info("未配置夸克 cookie（config.quark.cookie 或环境变量），跳过转存窗口调度")
        return None
    try:
        from alist_client import AlistClient
        from quark_client import QuarkClient
        from quark_window import WindowScheduler

        acfg = qcfg.get("alist") or {}
        if not acfg.get("baseUrl") or not acfg.get("username"):
            wlog.info("config.quark.alist 未配置 baseUrl/username，跳过转存窗口调度")
            return None
        quark = QuarkClient(cookie, logger=wlog.info)
        alist = AlistClient(acfg.get("baseUrl"), acfg.get("username"),
                            acfg.get("password", ""), logger=wlog.info)
        alist.login()
        return WindowScheduler(quark, alist, rpc, qcfg, cfg_dir,
                               dry_run=dry_run, verbose=verbose, logger=wlog.info)
    except Exception as e:
        wlog.info("转存窗口调度器初始化失败，本次只跑队列调度: %s" % e)
        return None


def main():
    ap = argparse.ArgumentParser(description="aria2 按网盘厂商分队列调度器 + 夸克转存窗口")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径 (默认: 脚本同目录 config.json)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="只打印将要执行的动作，不真正暂停/恢复/转存/删除")
    ap.add_argument("-1", "--once", action="store_true", help="只执行一轮调度（用于验证/测试）")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出每轮的明细日志")
    ap.add_argument("--pipeline-only", action="store_true", help="只跑夸克转存窗口，不跑厂商队列调度")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cfg_path = args.config or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(cfg_path):
        print("找不到配置文件: %s（可参考 config.example.json）" % cfg_path, file=sys.stderr)
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    log_file = setup_logging(cfg, os.path.dirname(os.path.abspath(cfg_path)))

    secret = cfg.get("secret", "") or os.environ.get(cfg.get("secretEnv", "ARIA2_RPC_SECRET"), "")
    if not secret:
        print("未配置 RPC 密钥（config.json 的 secret 字段或环境变量）", file=sys.stderr)
        sys.exit(1)

    rpc = Aria2Rpc(cfg["rpc"], secret)
    sched = Scheduler(cfg, os.path.dirname(os.path.abspath(cfg_path)),
                      dry_run=args.dry_run, verbose=args.verbose)
    log("调度器启动: rpc=%s dry_run=%s 轮询间隔=%ss 日志=%s(单文件最大%dM, 保留%d天)"
        % (cfg["rpc"], args.dry_run, cfg.get("pollIntervalSec", 3), log_file,
           int(cfg.get("logMaxBytes", DEFAULT_LOG_MAX_BYTES)) // (1024 * 1024),
           int(cfg.get("logRetentionDays", DEFAULT_LOG_RETENTION_DAYS))))

    window = build_window(cfg, os.path.dirname(os.path.abspath(cfg_path)), rpc,
                          args.dry_run, args.verbose)

    if args.pipeline_only:
        if window is None:
            print("--pipeline-only 需要 config.quark 启用，且 cookie / alist 配置完整",
                  file=sys.stderr)
            sys.exit(1)
        window.run(once=args.once)
        return

    if window is not None:
        if args.once:
            window.tick()
        else:
            threading.Thread(target=window.run, name="quark-window", daemon=True).start()
            log("转存窗口调度器已在后台线程启动")

    sched.run(rpc, once=args.once)


if __name__ == "__main__":
    main()
