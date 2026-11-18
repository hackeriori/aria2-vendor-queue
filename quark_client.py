#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quark_client.py —— 夸克网盘 API 的最小封装（仅标准库）

只覆盖本调度器需要的几个能力：
    - 分享链接：取 stoken、递归列目录、转存到指定目录（并等待任务完成）
    - 自己网盘：列目录、创建目录、删除文件、查容量

所有请求都带 pc 端 cookie 与常规浏览器 UA；夸克对非浏览器 UA 比较敏感。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://drive-pc.quark.cn/1/clouddrive"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 夸克任务状态：2 = 完成
TASK_DONE = 2

# 需要重新登录的错误码（cookie 过期/被踢下线）
AUTH_CODES = (401, 31001, 31002)


class QuarkError(Exception):
    pass


class QuarkAuthError(QuarkError):
    """cookie 失效。可被 cookie_provider 捕获并重新取 cookie 后重试。"""


class QuarkClient:
    def __init__(self, cookie="", timeout=30, logger=None, cookie_provider=None):
        self.cookie_provider = cookie_provider
        cookie = (cookie or "").strip()
        if not cookie and cookie_provider:
            cookie = (cookie_provider() or "").strip()
        if not cookie:
            raise QuarkError("夸克 cookie 为空")
        self.cookie = cookie
        self.timeout = timeout
        self.log = logger or (lambda msg: None)

    def _reload_cookie(self):
        """cookie 失效时回调 cookie_provider 重新取一份（如从 Alist 存储配置读）。"""
        if not self.cookie_provider:
            return False
        try:
            new = (self.cookie_provider() or "").strip()
        except Exception as e:
            self.log("重新读取夸克 cookie 失败: %s" % e)
            return False
        if not new or new == self.cookie:
            return False
        self.cookie = new
        self.log("已重新读取夸克 cookie（长度 %d），重试请求" % len(new))
        return True

    # ---------- 底层 ----------

    def _request(self, path, params=None, body=None, method=None, _retry=True):
        q = {"pr": "ucpro", "fr": "pc"}
        q.update(params or {})
        url = BASE + path + "?" + urllib.parse.urlencode(q, safe="")
        headers = {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Accept": "application/json, text/plain, */*",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code == 401 and _retry and self._reload_cookie():
                return self._request(path, params, body, method, _retry=False)
            try:
                payload = json.loads(raw)
            except Exception:
                raise QuarkError("HTTP %s %s: %s" % (e.code, path, raw[:200]))
            if payload.get("code") in AUTH_CODES:
                raise QuarkAuthError("%s 需要登录（cookie 失效）: %s %s"
                                     % (path, payload.get("code"), payload.get("message")))
            raise QuarkError("%s 失败: %s %s" % (path, payload.get("code"), payload.get("message")))
        except urllib.error.URLError as e:
            raise QuarkError("%s 网络错误: %s" % (path, e))
        try:
            payload = json.loads(raw)
        except Exception:
            raise QuarkError("%s 返回非 JSON: %s" % (path, raw[:200]))
        if isinstance(payload, dict) and payload.get("code") in AUTH_CODES:
            if _retry and self._reload_cookie():
                return self._request(path, params, body, method, _retry=False)
            raise QuarkAuthError("%s 需要登录（cookie 失效）: %s %s"
                                 % (path, payload.get("code"), payload.get("message")))
        return payload

    @staticmethod
    def _ok(resp, path):
        code = resp.get("code")
        if code not in (0, None) or resp.get("status") not in (200, None):
            raise QuarkError("%s 失败: %s %s" % (path, code, resp.get("message")))
        return resp.get("data") or {}

    # ---------- 容量 ----------

    def capacity(self):
        """返回 (总容量, 已用容量)，单位字节。"""
        d = self._ok(self._request("/member"), "/member")
        return int(d.get("total_capacity") or 0), int(d.get("use_capacity") or 0)

    # ---------- 我的网盘 ----------

    def list_dir(self, pdir_fid="0", page_size=200):
        """列出目录下的直接子项（自动翻页）。返回 [{fid,name,size,dir}]。"""
        items, page = [], 1
        while True:
            d = self._ok(self._request("/file/sort", {
                "pdir_fid": pdir_fid, "_page": page, "_size": page_size,
                "_fetch_total": 1, "_sort": "file_type:asc,file_name:asc",
            }), "/file/sort")
            batch = d.get("list") or []
            for x in batch:
                items.append({
                    "fid": x.get("fid"),
                    "name": x.get("file_name") or "",
                    "size": int(x.get("size") or 0),
                    "dir": bool(x.get("dir")),
                })
            if len(batch) < page_size:
                break
            page += 1
            if page > 20:
                break
        return items

    def ensure_dir(self, path, root_fid="0"):
        """按路径逐级创建目录，返回末级 fid。path 形如 "来自：分享/_pipe/巨人S2"。"""
        cur = root_fid
        for seg in [s for s in path.split("/") if s]:
            found = None
            for item in self.list_dir(cur):
                if item["dir"] and item["name"] == seg:
                    found = item["fid"]
                    break
            if found:
                cur = found
                continue
            d = self._ok(self._request("/file", body={
                "pdir_fid": cur, "file_name": seg, "dir_path": "", "dir_init_lock": False,
            }), "/file(创建目录)")
            fid = d.get("fid")
            if not fid:
                task_id = d.get("task_id")
                if task_id:
                    self.wait_task(task_id)
                    found = None
                    for item in self.list_dir(cur):
                        if item["dir"] and item["name"] == seg:
                            found = item["fid"]
                            break
                    fid = found
            if not fid:
                raise QuarkError("创建目录失败: %s" % seg)
            self.log("已创建网盘目录: %s" % seg)
            cur = fid
        return cur

    def delete(self, fid_list, action_type=2):
        """删除文件（进回收站，可恢复）。"""
        fid_list = [f for f in fid_list if f]
        if not fid_list:
            return {}
        return self._ok(self._request("/file/delete", body={
            "action_type": action_type, "filelist": fid_list, "exclude_fids": [],
        }), "/file/delete")

    # ---------- 分享链接 ----------

    def share_stoken(self, pwd_id, passcode=""):
        d = self._ok(self._request("/share/sharepage/token",
                                   body={"pwd_id": pwd_id, "passcode": passcode},
                                   method="POST"), "/share/sharepage/token")
        stoken = d.get("stoken")
        if not stoken:
            raise QuarkError("获取 stoken 失败（分享可能已失效或需要提取码）")
        return stoken

    def share_list(self, pwd_id, stoken, pdir_fid="0"):
        items, page = [], 1
        while True:
            d = self._ok(self._request("/share/sharepage/detail", {
                "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir_fid,
                "force": 0, "_page": page, "_size": 200, "_fetch_total": 1,
                "_sort": "file_type:asc,file_name:asc",
            }), "/share/sharepage/detail")
            batch = d.get("list") or []
            for x in batch:
                items.append({
                    "fid": x.get("fid"),
                    "name": x.get("file_name") or "",
                    "size": int(x.get("size") or 0),
                    "dir": bool(x.get("dir")),
                    "share_fid_token": x.get("share_fid_token") or "",
                })
            if len(batch) < 200:
                break
            page += 1
            if page > 20:
                break
        return items

    def share_save(self, pwd_id, stoken, fid, fid_token, to_pdir_fid, from_pdir_fid="0",
                   timeout=120):
        """转存单个文件到自己的目录，返回是否成功。"""
        d = self._ok(self._request("/share/sharepage/save", body={
            "fid_list": [fid],
            "fid_token_list": [fid_token],
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": from_pdir_fid,
            "scene": "link",
        }), "/share/sharepage/save")
        task_id = d.get("task_id")
        if task_id:
            self.wait_task(task_id, timeout=timeout)
        return True

    def wait_task(self, task_id, timeout=120, interval=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self._ok(self._request("/task", {
                "task_id": task_id, "retry_index": 0, "_page": 1, "_size": 50,
            }), "/task")
            status = d.get("status")
            if status == TASK_DONE:
                return d
            if status not in (None, 0, 1):
                raise QuarkError("任务 %s 异常结束，status=%s" % (task_id, status))
            time.sleep(interval)
        raise QuarkError("任务 %s 等待超时" % task_id)
