#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quark_window.py —— 夸克"转存窗口"调度器

解决的问题:
    夸克免费账号容量很小（本例 30G），4K 单集 8G 左右，一次只放得下两三集。
    手工流程（删网盘 → 网页转存 → Alist 复制链接 → AriaNg 添加 → 等 → 循环）
    每集要重复一遍，而免费号 490KB/s 下载一集要 5 小时，全程得人守着。

本模块把这条流水线闭环自动化:
    1. 算容量：只转存"放得下"的集，留出 reserve 余量；
    2. 转存：调夸克 API 把分享里的下一集转进 saveDir；
    3. 入队：让 Alist 刷新缓存 → 取 /d/ 直链 → aria2.addUri；
    4. 回收：本地文件大小 == 网盘文件大小（确认下完）→ 删除网盘文件释放配额；
    5. 补位：回到第 1 步，直到分享里的集全部下完。

安全边界（重要）:
    - 只删除"本任务管理目录内"的文件，绝不跨越 saveDir / quarkDir；
    - 只有 本地存在 且 大小完全相等 且 不在 aria2 活跃队列 时才删；
    - 删除进夸克回收站，可恢复；
    - dryRun=true 时只打印不动手。
"""

import json
import os
import re
import time
import urllib.parse

WINDOW_TAG = "[quark-window]"


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


class WindowScheduler:
    def __init__(self, quark, alist, rpc, cfg, cfg_dir, dry_run=False, verbose=False,
                 logger=None):
        self.quark = quark
        self.alist = alist
        self.rpc = rpc
        self.cfg = cfg
        self.dry_run = dry_run or bool(cfg.get("dryRun"))
        self.verbose = verbose
        self.log = logger or (lambda msg: None)

        self.interval = float(cfg.get("pollIntervalSec", 60))
        self.reserve_bytes = int(float(cfg.get("reserveGB", 4)) * (1024 ** 3))
        self.max_saved = int(cfg.get("maxSavedEpisodes", 2))
        self.delete_after_done = bool(cfg.get("deleteAfterDone", True))
        self.max_retries = int(cfg.get("maxRetries", 6))
        self.mount_path = (cfg.get("alistMountPath") or "/夸克").rstrip("/")
        self.jobs = [j for j in (cfg.get("jobs") or []) if j.get("enabled", True)]
        for j in self.jobs:  # 分享链接里直接解析 pwd_id，配置只写 shareUrl 即可
            if j.get("type", "share") == "share" and not j.get("pwdId"):
                m = re.search(r"/s/([0-9a-zA-Z]+)", j.get("shareUrl") or "")
                if m:
                    j["pwdId"] = m.group(1)

        state_file = cfg.get("stateFile") or os.path.join(cfg_dir, "state", "quark-window.json")
        self.state_file = os.path.abspath(
            state_file if os.path.isabs(state_file) else os.path.join(cfg_dir, state_file))
        self.state = {"jobs": {}}
        self._load_state()
        self._stoken_cache = {}
        self._items_cache = {}
        self.items_ttl = float(cfg.get("shareCacheSec", 300))

    # ---------- 状态 ----------

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
                self.state = data
                total = sum(len(v.get("done") or {}) for v in self.state["jobs"].values())
                self.log("已加载转存状态: %s（已完成 %d 个文件）" % (self.state_file, total))
        except Exception as e:
            self.log("转存状态文件损坏，将重建: %s" % e)

    def _save_state(self):
        tmp = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log("保存转存状态失败: %s" % e)

    def _job_state(self, job):
        return self.state["jobs"].setdefault(job["name"], {"done": {}, "retry": {}})

    # ---------- 通用工具 ----------

    def _gb(self, n):
        return "%.2fG" % (float(n) / (1024 ** 3))

    @staticmethod
    def _local_size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return -1

    def _local_done(self, path, expected):
        """判断本地是否真的下载完成。

        不能只看大小：aria2 开了 file-allocation=falloc 时，文件在开始下载的瞬间
        就被预分配到完整大小，此时"大小相等"但内容还没下完。必须同时满足：
          1. 没有 .aria2 控制文件（有它 = 任务还没下完/中断了）；
          2. 文件大小与网盘完全相等。
        """
        if os.path.exists(path + ".aria2"):
            return False
        size = self._local_size(path)
        return size >= 0 and size == expected

    def _inflight(self):
        """aria2 里处于 active / waiting / paused 的文件名集合。"""
        keys = ["gid", "status", "files"]
        names = {}
        for method, args in (("aria2.tellActive", (keys,)),
                             ("aria2.tellWaiting", (0, 1000, keys)),
                             ("aria2.tellStopped", (0, 1000, keys))):
            try:
                for t in self.rpc.call(method, *args):
                    if t.get("status") not in ("active", "waiting", "paused"):
                        continue
                    for f in t.get("files") or []:
                        p = f.get("path") or ""
                        if p:
                            names.setdefault(os.path.basename(p), []).append(t.get("gid"))
            except Exception as e:
                self.log("读取 aria2 任务列表失败: %s" % e)
        return names

    def _resolve_quark_dir(self, path):
        fid = "0"
        for seg in [s for s in path.split("/") if s]:
            found = None
            for item in self.quark.list_dir(fid):
                if item["dir"] and item["name"] == seg:
                    found = item["fid"]
                    break
            if not found:
                raise RuntimeError("夸克目录不存在: %s（在 %s 处中断）" % (path, seg))
            fid = found
        return fid

    def _alist_path(self, save_dir, name):
        return "%s/%s/%s" % (self.mount_path, save_dir.strip("/"), name)

    def _stoken(self, pwd_id, passcode=""):
        key = "%s|%s" % (pwd_id, passcode)
        cached = self._stoken_cache.get(key)
        if cached and time.time() - cached[1] < 1800:
            return cached[0]
        st = self.quark.share_stoken(pwd_id, passcode)
        self._stoken_cache[key] = (st, time.time())
        return st

    # ---------- 分享清单 ----------

    @staticmethod
    def _match(job, rel):
        inc = job.get("include") or []
        exc = job.get("exclude") or []
        if any(x in rel for x in exc):
            return False
        return not inc or any(x in rel for x in inc)

    def _share_items(self, job):
        """递归列出分享里匹配 include 规则的文件（含所在分享目录 fid）。"""
        cached = self._items_cache.get(job["name"])
        if cached and time.time() - cached[1] < self.items_ttl:
            return cached[0]
        pwd_id = job["pwdId"]
        stoken = self._stoken(pwd_id, job.get("passcode", ""))
        out = []

        def walk(fid, prefix, depth):
            if depth > 5:
                return
            for it in self.quark.share_list(pwd_id, stoken, fid):
                rel = "%s/%s" % (prefix, it["name"]) if prefix else it["name"]
                if it["dir"]:
                    walk(it["fid"], rel, depth + 1)
                elif self._match(job, rel):
                    out.append({
                        "name": it["name"], "rel": rel, "size": it["size"],
                        "fid": it["fid"], "token": it["share_fid_token"],
                        "pdir_fid": fid,
                    })

        walk("0", "", 0)
        out.sort(key=lambda x: _natural_key(x["rel"]))
        self._items_cache[job["name"]] = (out, time.time())
        return out

    # ---------- 删除（唯一的高风险动作） ----------

    def _try_delete(self, job, item, local_path, inflight):
        name = item["name"]
        st = self._job_state(job)
        if name in st["done"]:
            return False
        if name in inflight:
            if self.verbose:
                self.log("跳过删除 %s：aria2 里仍在队列/下载中" % name)
            return False
        if not self._local_done(local_path, item["size"]):
            return False
        if not self.delete_after_done:
            return False
        if self.dry_run:
            self.log("[dry-run] 将删除网盘文件 %s（%s，本地已下完）" % (name, self._gb(item["size"])))
            return False
        try:
            self.quark.delete([item["fid"]])
        except Exception as e:
            self.log("删除网盘文件失败 %s: %s" % (name, e))
            return False
        st["done"][name] = {"size": item["size"], "deletedAt": int(time.time()),
                            "job": job.get("name")}
        st["retry"].pop(name, None)
        self.log("已释放配额：删除网盘文件 %s（%s）" % (name, self._gb(item["size"])))
        return True

    # ---------- 入队 ----------

    @staticmethod
    def _normalize_url(raw):
        """把 Alist 的 /p/（代理）直链改写成 /d/。

        两者签名都是按"文件路径"生成的，/p/ 改成 /d/ 后签名依然有效；
        而厂商队列调度器是按 URL 里 "/d/<挂载名>/" 分类的，统一成 /d/ 才能被正确归类。
        """
        try:
            p = urllib.parse.urlsplit(raw)
            if p.path.startswith("/p/"):
                return urllib.parse.urlunsplit(p._replace(path="/d/" + p.path[3:]))
        except Exception:
            pass
        return raw

    def _enqueue(self, job, name, local_path):
        st = self._job_state(job)
        retry = st["retry"].get(name, {"count": 0, "ts": 0})
        if retry["count"] >= self.max_retries:
            if time.time() - retry["ts"] > 3600:
                self.log("警告：%s 已重试 %d 次仍未完成，暂停自动重试（1 小时后再试）"
                         % (name, retry["count"]))
                retry["ts"] = time.time()
            return False
        path = self._alist_path(job["saveDir"], name)
        try:
            raw = self._normalize_url(self.alist.get_raw_url(path))
        except Exception as e:
            self.log("取直链失败（下轮重试）%s: %s" % (name, e))
            return False
        if self.dry_run:
            self.log("[dry-run] 将添加 aria2 任务 %s -> %s" % (name, job["localDir"]))
            retry["count"] += 1
            retry["ts"] = time.time()
            st["retry"][name] = retry
            return False
        try:
            os.makedirs(job["localDir"], exist_ok=True)
        except OSError:
            pass
        try:
            self.rpc.call("aria2.addUri", [raw],
                          {"dir": job["localDir"], "out": name, "continue": "true"})
        except Exception as e:
            self.log("添加 aria2 任务失败 %s: %s" % (name, e))
            return False
        retry["count"] += 1
        retry["ts"] = time.time()
        st["retry"][name] = retry
        self.log("已入队：%s（第 %d 次）" % (name, retry["count"]))
        return True

    # ---------- 两类任务 ----------

    def _handle_share(self, job, inflight):
        save_fid = self.quark.ensure_dir(job["saveDir"])
        saved = [x for x in self.quark.list_dir(save_fid) if not x["dir"]]
        st = self._job_state(job)
        changed = False
        # 窗口大小、预留空间都允许按任务覆盖（单集补漏可以只占 1 格、少留余量）
        max_saved = int(job.get("maxSavedEpisodes", self.max_saved))
        reserve = int(float(job.get("reserveGB", self.cfg.get("reserveGB", 4))) * (1024 ** 3))

        remain = []
        for item in saved:
            local = os.path.join(job["localDir"], item["name"])
            if self._try_delete(job, item, local, inflight):
                changed = True
                continue
            if item["name"] in st["done"]:
                continue
            remain.append(item)

        for item in remain:
            if item["name"] in inflight:
                continue
            local = os.path.join(job["localDir"], item["name"])
            if self._local_done(local, item["size"]):
                continue
            self._enqueue(job, item["name"], local)

        # 补位：只转存"放得下"的集
        items = self._share_items(job)
        if not items:
            self.log("分享里没有匹配到任何文件（检查 include 规则）")
            return changed
        done = st["done"]
        have = {x["name"] for x in remain}
        todo = [i for i in items if i["name"] not in done and i["name"] not in have]
        total, used = self.quark.capacity()
        budget = total - used - reserve
        slots = max_saved - len(remain)
        self.log("%s｜分享 %d 集，已完成 %d，网盘在盘 %d 集，剩余空间 %s，本轮可再转存 %d 集"
                 % (job["name"], len(items), len(done), len(remain), self._gb(total - used),
                    max(0, min(slots, len(todo)))))
        for item in todo:
            if slots <= 0:
                break
            if item["size"] > budget:
                self.log("空间不足，暂停转存：下一集 %s 需要 %s，可用 %s（预留 %s）"
                         % (item["name"], self._gb(item["size"]), self._gb(budget),
                            self._gb(reserve)))
                break
            if self.dry_run:
                self.log("[dry-run] 将转存 %s（%s）" % (item["name"], self._gb(item["size"])))
            else:
                try:
                    self.quark.share_save(job["pwdId"], self._stoken(job["pwdId"], job.get("passcode", "")),
                                          item["fid"], item["token"], save_fid,
                                          from_pdir_fid=item["pdir_fid"])
                    self.log("已转存：%s（%s）" % (item["name"], self._gb(item["size"])))
                    changed = True
                except Exception as e:
                    self.log("转存失败 %s: %s" % (item["name"], e))
                    break
            budget -= item["size"]
            slots -= 1
        return changed

    def _handle_reclaim(self, job, inflight):
        fid = self._resolve_quark_dir(job["quarkDir"])
        files = [x for x in self.quark.list_dir(fid) if not x["dir"]]
        changed = False
        freed = 0
        for item in files:
            local = os.path.join(job["localDir"], item["name"])
            if self._try_delete(job, item, local, inflight):
                changed = True
                freed += item["size"]
        if freed:
            self.log("%s｜本轮共释放 %s" % (job["name"], self._gb(freed)))
        elif self.verbose:
            self.log("%s｜%d 个文件均未满足删除条件" % (job["name"], len(files)))
        return changed

    # ---------- 主循环 ----------

    def tick(self):
        inflight = self._inflight()
        changed = False
        for job in self.jobs:
            try:
                if job.get("type", "share") == "reclaim":
                    changed |= self._handle_reclaim(job, inflight)
                else:
                    changed |= self._handle_share(job, inflight)
            except Exception as e:
                self.log("任务[%s]本轮出错（下轮继续）: %s" % (job.get("name"), e))
        if changed:
            self._save_state()

    def run(self, once=False):
        names = ", ".join(j.get("name", "?") for j in self.jobs)
        self.log("转存窗口调度器启动：%d 个任务（%s）｜窗口 %d 集｜预留 %s｜轮询 %ss｜dry_run=%s"
                 % (len(self.jobs), names, self.max_saved, self._gb(self.reserve_bytes),
                    self.interval, self.dry_run))
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                self._save_state()
                return
            except Exception as e:
                self.log("本轮异常（下轮继续）: %s" % e)
            if once:
                return
            try:
                time.sleep(self.interval)
            except KeyboardInterrupt:
                self._save_state()
                return
