#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线仿真测试 —— 用一个模拟 aria2 RPC 跑完整调度场景，验证:
  1. 21 个同厂商任务涌入时，一轮内收敛为 "1 个在跑 + 20 个暂停排队"；
  2. 稳态下一轮不产生任何多余动作（无抖动）；
  3. 当前任务完成后，按添加顺序恢复下一个；
  4. 不同厂商的任务可以同时下载（互不阻塞）；
  5. 同厂商再加任务会立刻被压回队列，不会抢跑。

运行: python sim_test.py
"""

import sys

sys.path.insert(0, ".")

from urllib.parse import quote, unquote  # noqa: E402

from aria2_vendor_queue import Scheduler  # noqa: E402


def alist_uri(mount, name_template):
    """构造 aria2 实际会收到的 Alist 下载地址形态（中文路径按 URL 编码）"""
    return ("http://alist.local:5244/d/"
            + quote(mount, safe="") + "/" + quote("示例目录", safe="")
            + "/4K/" + name_template + "?sign=dummy")


QUARK = alist_uri("夸克", "{n}.mkv")
BAIDU = alist_uri("百度网盘", "EP{n}.mkv")


class MockAria2:
    """按 aria2 真实状态机行为模拟: pause 后槽位释放 → 自动启动下一个 waiting"""

    def __init__(self, limit=1):
        self.limit = limit
        self.tasks = {}
        self.n = 0
        self.pause_calls = 0
        self.unpause_calls = 0

    def _new_gid(self):
        self.n += 1
        return "%014x%02x" % (0xC0FFEE000000 + self.n, 0)

    def add(self, uri):
        gid = self._new_gid()
        self.tasks[gid] = {"uri": uri, "status": "waiting"}
        self.auto_activate()
        return gid

    def auto_activate(self):
        active = sum(1 for t in self.tasks.values() if t["status"] == "active")
        for t in self.tasks.values():
            if active >= self.limit:
                break
            if t["status"] == "waiting":
                t["status"] = "active"
                active += 1

    def complete(self, gid):
        self.tasks[gid]["status"] = "complete"
        self.auto_activate()

    def _view(self, gid):
        t = self.tasks[gid]
        return {"gid": gid, "status": t["status"],
                "files": [{"uris": [{"uri": t["uri"]}]}]}

    def call(self, method, *params):
        if method == "aria2.getGlobalOption":
            return {"max-concurrent-downloads": str(self.limit)}
        if method == "aria2.changeGlobalOption":
            self.limit = int(params[0]["max-concurrent-downloads"])
            self.auto_activate()
            return "OK"
        if method == "aria2.tellActive":
            return [self._view(g) for g, t in self.tasks.items() if t["status"] == "active"]
        if method == "aria2.tellWaiting":
            # 复刻真实 aria2(1.36) 行为: tellWaiting 返回 waiting + paused 的任务
            rows = [self._view(g) for g, t in self.tasks.items()
                    if t["status"] in ("waiting", "paused")]
            return rows[params[0]:params[0] + params[1]]
        if method == "aria2.tellStopped":
            # 复刻真实 aria2(1.36) 行为: tellStopped 也包含 paused 的任务
            rows = [self._view(g) for g, t in self.tasks.items()
                    if t["status"] in ("paused", "complete", "error", "removed")]
            return rows[params[0]:params[0] + params[1]]
        if method in ("aria2.pause", "aria2.forcePause"):
            gid = params[0]
            assert self.tasks[gid]["status"] in ("active", "waiting"), \
                "aria2.pause 只能作用于 active/waiting 任务"
            self.tasks[gid]["status"] = "paused"
            self.pause_calls += 1
            self.auto_activate()
            return gid
        if method == "aria2.unpause":
            gid = params[0]
            assert self.tasks[gid]["status"] == "paused", "aria2.unpause 只能作用于 paused 任务"
            self.tasks[gid]["status"] = "waiting"
            self.unpause_calls += 1
            self.auto_activate()
            return gid
        raise RuntimeError("未实现的方法: " + method)

    # ---- 断言辅助 ----
    def snapshot(self):
        res = {"active": [], "waiting": [], "paused": [], "complete": []}
        for g, t in self.tasks.items():
            res[t["status"]].append(t["uri"].rsplit("/", 1)[-1].split("?")[0])
        return res

    def state_of(self, name):
        for g, t in self.tasks.items():
            if t["uri"].rsplit("/", 1)[-1].split("?")[0] == name:
                return t["status"]
        return None


def make_scheduler(priority=None):
    cfg = {
        "rpc": "mock", "secret": "x", "pollIntervalSec": 3,
        "autoAdjustGlobalLimit": True, "defaultCategory": "默认", "defaultMaxConcurrent": 1,
        "stateFile": "sim_state.json",
        "categories": [
            {"name": "夸克", "match": ["/d/夸克/"], "maxConcurrent": 1},
            {"name": "百度网盘", "match": ["/d/百度网盘/"], "maxConcurrent": 1},
        ],
    }
    if priority:
        cfg["priorityMatch"] = priority
    return Scheduler(cfg, ".", dry_run=False, verbose=False)


def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        raise AssertionError(msg)


def quark_uri(sub, name):
    """构造带子目录的夸克直链形态（sim 用），用于测试插队路径匹配"""
    return ("http://alist.local:5244/d/" + quote("夸克", safe="") + "/"
            + quote("来自：分享", safe="") + "/" + quote(sub, safe="/") + "/"
            + name + "?sign=dummy")


def scenario_priority():
    """插队：priorityMatch 命中的一批任务必须整体紧跟"在下的任务"之后，
    且后续新入队的任务不能挤进插队组中间。"""
    import os
    if os.path.exists("sim_state.json"):
        os.remove("sim_state.json")

    aria = MockAria2(limit=1)
    sched = make_scheduler(["_pipe/谷雨27-30"])
    sched.ensure_global_limit(aria)

    print("场景 7: 03 在下时插入 _pipe/谷雨27-30（4 集）")
    for ep in ("03", "04", "05", "06"):
        aria.add(quark_uri("巨人S2", "S2 %s.mkv" % ep))
    sched.poll(aria)
    check(aria.state_of("S2 03.mkv") == "active", "03 正在下载")

    for ep in ("27", "28", "29", "30"):
        aria.add(quark_uri("_pipe/谷雨27-30", "%s.mkv" % ep))
    sched.poll(aria)
    check(aria.state_of("S2 03.mkv") == "active", "03 不被打断，继续下载")
    for ep in ("27", "28", "29", "30"):
        check(aria.state_of("%s.mkv" % ep) == "paused", "%s 已入队待下" % ep)

    print("  队列顺序:", " > ".join(aria.tasks[g]["uri"].rsplit("/", 1)[-1].split("?")[0]
                                   for g in sched.queue_order()))

    print("场景 8: 03 下完 → 应轮到最后插队的 27，而不是 04")
    aria.complete([g for g, t in aria.tasks.items() if "S2 03" in t["uri"]][0])
    sched.poll(aria)
    check(aria.state_of("27.mkv") == "active", "27.mkv 立刻开始下载")
    check(aria.state_of("S2 04.mkv") == "paused", "04 继续排队（被插队跳过）")

    print("场景 9: 插队期间又有新集入队（07）→ 只能排到队尾，不能插进插队组")
    aria.add(quark_uri("巨人S2", "S2 07.mkv"))
    sched.poll(aria)
    check(aria.state_of("S2 07.mkv") == "paused", "07 排队中")

    p0, u0 = aria.pause_calls, aria.unpause_calls
    sched.poll(aria)
    sched.poll(aria)
    check(aria.pause_calls == p0 and aria.unpause_calls == u0,
          "稳态连续两轮无 pause/unpause（重排不会造成抖动）")

    print("场景 10: 27 下完 → 28（插队组内部按顺序），07 仍不能抢先")
    aria.complete([g for g, t in aria.tasks.items() if "27.mkv" in t["uri"]][0])
    sched.poll(aria)
    check(aria.state_of("28.mkv") == "active", "28.mkv 接续")
    check(aria.state_of("S2 04.mkv") == "paused", "04 仍在排队")

    print("场景 11: 插队组下完 → 队列自动接回 04")
    for nm in ("28.mkv", "29.mkv", "30.mkv"):
        aria.complete([g for g, t in aria.tasks.items()
                       if unquote(t["uri"].split("?")[0]).endswith("/谷雨27-30/" + nm)][0])
        sched.poll(aria)
    snap = aria.snapshot()
    print("  当前在下载:", snap["active"])
    check(len(snap["active"]) == 1 and snap["active"][0] == "S2 04.mkv",
          "插队组下完后自动接回 04")

    if os.path.exists("sim_state.json"):
        os.remove("sim_state.json")


def scenario_legacy_state():
    """复刻线上那份已经跑歪的 state.json：04 的 seq 比在下的 03 还小（反转），
    谷雨组 seq 25~28 已经分出去但 nextSeq 还停在 27（会导致并列）。"""
    import json as _json
    import os
    if os.path.exists("sim_state.json"):
        os.remove("sim_state.json")

    aria = MockAria2(limit=1)
    aria.tasks = {
        "0e64dca525ae27a9": {"uri": quark_uri("巨人S2", "S2 03.mkv"), "status": "active"},
        "1f5de1e3006e912d": {"uri": quark_uri("巨人S2", "S2 04.mkv"), "status": "paused"},
    }
    for ep, gid in zip(("27", "28", "29", "30"),
                       ("4c920cea889fd48b", "4ff30f3c0c734e7e",
                        "c3590fb2d4a4fdf3", "7307e59ccc8de755")):
        aria.tasks[gid] = {"uri": quark_uri("_pipe/谷雨27-30", "%s.mkv" % ep),
                           "status": "paused"}

    state = {"tasks": {
        "0e64dca525ae27a9": {"seq": 24, "category": "夸克", "pausedByUs": False},
        "1f5de1e3006e912d": {"seq": 23, "category": "夸克", "pausedByUs": True},
        "4c920cea889fd48b": {"seq": 25, "category": "夸克", "pausedByUs": True},
        "4ff30f3c0c734e7e": {"seq": 26, "category": "夸克", "pausedByUs": True},
        "c3590fb2d4a4fdf3": {"seq": 27, "category": "夸克", "pausedByUs": True},
        "7307e59ccc8de755": {"seq": 28, "category": "夸克", "pausedByUs": True},
    }, "nextSeq": 27}
    with open("sim_state.json", "w", encoding="utf-8") as f:
        _json.dump(state, f, ensure_ascii=False)

    sched = make_scheduler(["_pipe/谷雨27-30"])
    sched.ensure_global_limit(aria)

    print("场景 12: 载入线上那份乱序 state（04 seq=23 在 03 seq=24 之前，nextSeq=27 落后）")
    sched.poll(aria)

    def order_names():
        return [unquote(aria.tasks[g]["uri"].split("?")[0]).rsplit("/", 1)[-1]
                for g in sched.queue_order()]

    print("  修复后队列顺序:", " > ".join(order_names()))
    check(order_names() == ["S2 03.mkv", "27.mkv", "28.mkv", "29.mkv",
                            "30.mkv", "S2 04.mkv"], "插队组整体紧跟 03，04 被压到队尾")
    check(sched.state["nextSeq"] == 7, "nextSeq 被修正为 7（不再与新号并列）")
    seqs = [e["seq"] for e in sched.state["tasks"].values()]
    check(len(seqs) == len(set(seqs)), "seq 不再重复")

    print("场景 13: 之后新入队的 05 只能排到队尾")
    aria.add(quark_uri("巨人S2", "S2 05.mkv"))
    sched.poll(aria)
    print("  队列顺序:", " > ".join(order_names()))
    check(order_names()[-1] == "S2 05.mkv", "05 排在最后")
    check(aria.state_of("S2 03.mkv") == "active", "03 未被任何操作打断")

    print("场景 14: 03 下完 → 轮到 27（而不是 04/05）")
    aria.complete("0e64dca525ae27a9")
    sched.poll(aria)
    check(aria.state_of("27.mkv") == "active", "27.mkv 开始下载")

    if os.path.exists("sim_state.json"):
        os.remove("sim_state.json")


def main():
    import os
    if os.path.exists("sim_state.json"):
        os.remove("sim_state.json")

    aria = MockAria2(limit=1)          # 复现你的现状: 全局最大同时下载数 = 1
    sched = make_scheduler()

    print("场景 1: 一次性加入 21 个夸克任务（06~26）")
    for i in range(6, 27):
        aria.add(QUARK.format(n="%02d" % i))
    print("  初始状态:", {k: len(v) for k, v in aria.snapshot().items()},
          "| 全局上限 =", aria.limit)

    sched.ensure_global_limit(aria)
    sched.poll(aria)
    snap = aria.snapshot()
    print("  第 1 轮后:", {k: len(v) for k, v in snap.items()},
          "| 全局上限 =", aria.limit)
    check(aria.limit == 3, "全局上限被自动调至 3（两厂商 + 默认分类）")
    check(len(snap["active"]) == 1 and snap["active"][0] == "06.mkv", "只有 06.mkv 在下载")
    check(len(snap["paused"]) == 20, "其余 20 个任务被暂停排队")
    check(len(snap["waiting"]) == 0, "没有遗留的 waiting 任务（不会反复启动-暂停）")

    print("场景 2: 稳态再跑一轮（应无任何动作）")
    p0, u0 = aria.pause_calls, aria.unpause_calls
    sched.poll(aria)
    check(aria.pause_calls == p0 and aria.unpause_calls == u0, "本轮 pause/unpause 调用为 0（无抖动）")

    print("场景 3: 06.mkv 下载完成 → 应自动恢复 07.mkv")
    gid06 = [g for g, t in aria.tasks.items() if "06.mkv" in t["uri"]][0]
    aria.complete(gid06)
    sched.poll(aria)
    check(aria.state_of("07.mkv") == "active", "07.mkv 自动开始下载")
    check(aria.state_of("08.mkv") == "paused", "08.mkv 继续排队等待")
    check(sum(1 for t in aria.tasks.values() if t["status"] == "active") == 1,
          "夸克分类仍只有 1 个任务在跑")

    print("场景 4: 加入百度网盘任务 EP01 → 应与夸克并行下载")
    aria.add(BAIDU.format(n="01"))
    sched.poll(aria)
    check(aria.state_of("EP01.mkv") == "active", "EP01.mkv 在下载")
    check(aria.state_of("07.mkv") == "active", "07.mkv 同时在下载（不同厂商互不阻塞）")
    check(sum(1 for t in aria.tasks.values() if t["status"] == "active") == 2,
          "全局并发 = 2，各厂商各 1 个")

    print("场景 5: 再加百度 EP02 → 应立即被压回百度排队，不与 EP01 抢带宽")
    aria.add(BAIDU.format(n="02"))
    sched.poll(aria)
    check(aria.state_of("EP02.mkv") == "paused", "EP02.mkv 被暂停排队")
    check(aria.state_of("EP01.mkv") == "active", "EP01.mkv 保持下载")
    check(sum(1 for t in aria.tasks.values() if t["status"] == "active") == 2,
          "全局并发仍为 2")

    print("场景 6: EP01 完成 → 应接续 EP02，且夸克 07 不受影响")
    gid_ep01 = [g for g, t in aria.tasks.items() if "EP01.mkv" in t["uri"]][0]
    aria.complete(gid_ep01)
    sched.poll(aria)
    check(aria.state_of("EP02.mkv") == "active", "EP02.mkv 接续下载")
    check(aria.state_of("07.mkv") == "active", "07.mkv 仍在下载（夸克队列未被打断）")

    snap = aria.snapshot()
    print("\n最终状态:", {k: len(v) for k, v in snap.items()})
    print("最终在下载:", sorted(snap["active"]))
    print("\n全部场景通过 ✓  （pause 调用 %d 次, unpause 调用 %d 次）"
          % (aria.pause_calls, aria.unpause_calls))

    import os as _os
    if _os.path.exists("sim_state.json"):
        _os.remove("sim_state.json")

    print()
    scenario_priority()
    print("\n插队场景通过 ✓")

    print()
    scenario_legacy_state()
    print("\n乱序状态自愈场景通过 ✓")


if __name__ == "__main__":
    main()
